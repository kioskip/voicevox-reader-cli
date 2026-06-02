#!/bin/bash
# scripts/cmd_say.sh - vvread say subcommand (R-005)
#
# Usage: cmd_say.sh <text> [--speaker N]
#
# テキストを VOICEVOX で合成し、再生する。長文は sanitize.py + chunk_split.py
# で chunk 分割し、各 chunk を逐次に「synth → play」する薄い orchestrator。
#
# 設計方針(R-005 スコープ):
#   - prefetch / 並列合成は本コマンドでは行わない(逐次合成 + 同期再生)
#   - 新しい say 起動時に古い playback を kill する(vvread_kill_play)
#   - session token 方式で preemption: 各 chunk の前後で session.id を確認し、
#     新しい session が来ていれば silent に exit 0(古い発話を残さない)
#   - synth は lib/voicevox.sh::voicevox_synthesize、play は lib/playback.sh::
#     vvread_play_async に委譲(R-028 の cmd_synth/cmd_play と同じ層)
#   - 引数パース: lib/say_args.sh (R-103)
#   - synth/play チャンクヘルパー: lib/say_pipeline.sh (R-103)
#
# 速度改善(prefetch / 並列合成 / キャンセル制御の高度化)は別タスクで扱う。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# OS 別 path 解決
# shellcheck source=../lib/paths.sh
source "${VVREAD_SCRIPTS_DIR}/lib/paths.sh"
STATE_DIR="$(vvread_state_dir)"
LOG_DIR="$(vvread_log_dir)"
CACHE_DIR="$(vvread_cache_dir)"
mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${CACHE_DIR}"

# settings.py で設定を一括解決(env > project > user > default)
# log.sh source より前に eval することで log.level も反映される
PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
[ -x "${PYTHON}" ] || PYTHON="python3"
eval "$("${PYTHON}" "${VVREAD_SCRIPTS_DIR}/settings.py" env 2>/dev/null || true)"

# 共通ロガー
# shellcheck disable=SC2034
LOG_NAME="say"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"

# VOICEVOX HTTP API
# shellcheck source=../lib/voicevox.sh
source "${VVREAD_SCRIPTS_DIR}/lib/voicevox.sh"

# Playback 抽象層 (R-002)
# shellcheck source=../lib/playback.sh
source "${VVREAD_SCRIPTS_DIR}/lib/playback.sh"

# セッショントークン管理 (S-011)
# shellcheck source=../lib/session.sh
source "${VVREAD_SCRIPTS_DIR}/lib/session.sh"

# sanitize + chunk_split パイプライン (S-011)
# shellcheck source=../lib/chunk.sh
source "${VVREAD_SCRIPTS_DIR}/lib/chunk.sh"

# 引数パース (R-103)
# shellcheck source=../lib/say_args.sh
source "${VVREAD_SCRIPTS_DIR}/lib/say_args.sh"

# synth/play チャンクヘルパー (R-103)
# shellcheck source=../lib/say_pipeline.sh
source "${VVREAD_SCRIPTS_DIR}/lib/say_pipeline.sh"

# キャッシュ TTL 自動削除 (T-013)
# shellcheck source=../lib/cache_cleanup.sh
source "${VVREAD_SCRIPTS_DIR}/lib/cache_cleanup.sh"

# ===== 引数パース =====

vvread_say_parse_args "$@"

# ===== キャッシュ TTL クリーンアップ（バックグラウンド）=====
# 引数パース成功後のみここに到達する（不正引数時は say_args.sh が exit する）
_vvread_cache_cleanup_if_due

# ===== エンジン配列 =====
# VOICEVOX_ENGINES は settings.py env で ';' 区切りで解決済み。
# 未設定なら VOICEVOX_ENGINE_URL の単一要素にフォールバック。
ENGINES=()
if [ -n "${VOICEVOX_ENGINES:-}" ]; then
  IFS=';' read -ra ENGINES <<< "${VOICEVOX_ENGINES}"
fi
[ "${#ENGINES[@]}" -eq 0 ] && ENGINES=("${VOICEVOX_ENGINE_URL:-http://127.0.0.1:50021}")
ENGINE_COUNT="${#ENGINES[@]}"

# ===== 発話パラメータ =====
# settings.py env の eval で VOICEVOX_* は解決済み(env > project > user > default)。
# voicevox_resolve_speaker は settings.py 失敗時のバックストップとして機能する。
# --speaker は最優先(SPEAKER_OVERRIDE が設定されていればそちらを使う)。

SPEAKER=$(voicevox_resolve_speaker "${SPEAKER_OVERRIDE}")

# ===== sanitize + chunk split =====

CHUNKED=$(vvread_chunk_split "${TEXT}" "${SPEAKER}" "${PYTHON}" "${VVREAD_SCRIPTS_DIR}")

if [ -z "${CHUNKED}" ]; then
  log_info "say empty after sanitize text_chars=${#TEXT}"
  exit 0
fi

# 配列に読み込む(Bash 3.2 互換のため while + read のみ使用、bash 4+ 専用の
# 配列読込 builtin は doc/08-bash-rules.md §1 により禁止)
CHUNKS=()
while IFS= read -r line; do
  if [ -n "${line}" ]; then
    CHUNKS+=("${line}")
  fi
done <<< "${CHUNKED}"

CHUNK_TOTAL=${#CHUNKS[@]}
if [ "${CHUNK_TOTAL}" -eq 0 ]; then
  log_info "say no chunks after split text_chars=${#TEXT}"
  exit 0
fi

# ===== Session token + 旧 playback 停止 =====

SESSION_FILE="${STATE_DIR}/session.id"
PID_FILE="${STATE_DIR}/playing.pid"

# 旧 playback を kill(idempotent / 不在でも安全)。これにより同時に
# 動いている古い vvread say の wait が解け、次の session check で exit 0 する
vvread_kill_play "${PID_FILE}"

# 新しい session token を発行(lib/session.sh)
SESSION_ID=$(vvread_session_start "${SESSION_FILE}")

CACHE_HIT_FILE="${STATE_DIR}/cache_hits_${SESSION_ID}_$$.tmp"
: > "${CACHE_HIT_FILE}"
export VVREAD_CACHE_HIT_FILE="${CACHE_HIT_FILE}"

# 各 chunk の wav を入れる prefix
WAV_PREFIX="${STATE_DIR}/voice_${SESSION_ID}"

# synth background PID を管理する配列(vvread_say_launch_synth_bg が書き込む)
SYNTH_PIDS=()

# 終了時に全 synth worker を kill → wait → wav 削除(正常 / 失敗 / preempted 共通)
_vvread_say_cleanup() {
  local _idx
  if [ "${#SYNTH_PIDS[@]}" -gt 0 ]; then
    for _idx in "${!SYNTH_PIDS[@]}"; do
      kill "${SYNTH_PIDS[$_idx]}" 2>/dev/null || true
    done
    for _idx in "${!SYNTH_PIDS[@]}"; do
      wait "${SYNTH_PIDS[$_idx]}" 2>/dev/null || true
      unset "SYNTH_PIDS[$_idx]"
    done
  fi
  rm -f "${WAV_PREFIX}"_* 2>/dev/null || true
  rm -f "${CACHE_HIT_FILE:-}" 2>/dev/null || true
}
trap _vvread_say_cleanup EXIT

log_info "say start chunks=${CHUNK_TOTAL} text_chars=${#TEXT} speaker=${SPEAKER} engines=${ENGINE_COUNT} session=${SESSION_ID}"

# ===== orchestration loop (Producer/Consumer, B-124) =====
#
# 設計: 固定ウィンドウ方式。各エンジンは最大 1 合成を担当。
#   初期バッチ: chunk 0..M-1 を並列 synth 起動
#   play loop i:
#     pre-wait preempt check
#     wait SYNTH_PIDS[i] → unset → post-wait check
#     synth 失敗 → rm partial wav → 同一 engine retry（next 起動より前）
#     post-retry check → i+M を background synth → play → rm wav → post-play check

# 初期バッチ: chunk 0..M-1 を並列 synth 起動
j=0
while [ "${j}" -lt "${ENGINE_COUNT}" ] && [ "${j}" -lt "${CHUNK_TOTAL}" ]; do
  vvread_say_launch_synth_bg "${j}" "${CHUNKS[${j}]}" "${WAV_PREFIX}_${j}.wav" \
    "${SPEAKER}" "${CHUNK_TOTAL}" "${ENGINES[$((j % ENGINE_COUNT))]}"
  j=$((j + 1))
done

i=0
while [ "${i}" -lt "${CHUNK_TOTAL}" ]; do

  # 1. pre-wait preempt check
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=pre_wait"
    exit 0
  fi

  WAV="${WAV_PREFIX}_${i}.wav"

  # 2. synth 完了を wait(set -e 対応: || で RC 捕捉)
  synth_rc=0
  wait "${SYNTH_PIDS[${i}]}" || synth_rc=$?
  unset "SYNTH_PIDS[${i}]"

  # 3. post-wait session check
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=post_wait"
    rm -f "${WAV}"
    exit 0
  fi

  # 4. synth 失敗 → partial wav 削除 → 同一 engine retry(next 起動より先に実施)
  if [ "${synth_rc}" -ne 0 ]; then
    rm -f "${WAV}"
    retry_engine="${ENGINES[$((i % ENGINE_COUNT))]}"
    log_info "say synth_failed_retry chunk=$((i + 1))/${CHUNK_TOTAL} engine=${retry_engine}"
    if ! vvread_say_synth_chunk "${i}" "${CHUNKS[${i}]}" "${WAV}" "${SPEAKER}" \
        "${CHUNK_TOTAL}" "${retry_engine}"; then
      log_info "say synth_failed chunk=$((i + 1))/${CHUNK_TOTAL}"
      printf 'vvread say: synthesis failed for chunk %s\n' "$((i + 1))" >&2
      exit 1
    fi
  fi

  # 5. post-retry session check
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=post_retry"
    rm -f "${WAV}"
    exit 0
  fi

  # 6. look-ahead: fallback 完了後に next worker を起動(窓を維持)
  next=$((i + ENGINE_COUNT))
  if [ "${next}" -lt "${CHUNK_TOTAL}" ]; then
    vvread_say_launch_synth_bg "${next}" "${CHUNKS[${next}]}" "${WAV_PREFIX}_${next}.wav" \
      "${SPEAKER}" "${CHUNK_TOTAL}" "${ENGINES[$((next % ENGINE_COUNT))]}"
  fi

  # 7. play
  log_info "say play chunk=$((i + 1))/${CHUNK_TOTAL} engine=${ENGINES[$((i % ENGINE_COUNT))]}"

  if ! vvread_say_play_chunk "${i}" "${WAV}" "${PID_FILE}" "${CHUNK_TOTAL}"; then
    exit 1
  fi

  rm -f "${WAV}"

  # 8. post-play preempt check
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=post_play"
    exit 0
  fi

  i=$((i + 1))
done

_cache_hits=0
if [ -s "${CACHE_HIT_FILE:-}" ]; then
  _cache_hits=$(
    sort -u "${CACHE_HIT_FILE}" 2>/dev/null |
      wc -l |
      tr -d ' '
  )
fi
log_info "say cache_summary hits=${_cache_hits}/${CHUNK_TOTAL} session=${SESSION_ID}"

log_info "say done chunks=${CHUNK_TOTAL} session=${SESSION_ID}"
exit 0
