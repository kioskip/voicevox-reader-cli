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
mkdir -p "${STATE_DIR}" "${LOG_DIR}"

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

# ===== 引数パース =====

vvread_say_parse_args "$@"

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

# 各 chunk の wav を入れる prefix
WAV_PREFIX="${STATE_DIR}/voice_${SESSION_ID}"

# 終了時(正常 / 失敗 / preempted いずれも)に自プロセスの wav を全部消す
trap '
  rm -f "'"${WAV_PREFIX}"'"_* 2>/dev/null
' EXIT

log_info "say start chunks=${CHUNK_TOTAL} text_chars=${#TEXT} speaker=${SPEAKER} session=${SESSION_ID}"

# ===== orchestration loop =====

i=0
while [ "${i}" -lt "${CHUNK_TOTAL}" ]; do
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=pre_synth"
    exit 0
  fi

  WAV="${WAV_PREFIX}_${i}.wav"

  if ! vvread_say_synth_chunk "${i}" "${CHUNKS[${i}]}" "${WAV}" "${SPEAKER}" "${CHUNK_TOTAL}"; then
    log_info "say synth_failed chunk=$((i + 1))/${CHUNK_TOTAL}"
    printf 'vvread say: synthesis failed for chunk %s\n' "$((i + 1))" >&2
    exit 1
  fi

  # synth と play の境界。新しい say が来ていれば再生せず終了
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=pre_play"
    rm -f "${WAV}"
    exit 0
  fi

  log_info "say play chunk=$((i + 1))/${CHUNK_TOTAL}"

  if ! vvread_say_play_chunk "${i}" "${WAV}" "${PID_FILE}" "${CHUNK_TOTAL}"; then
    exit 1
  fi

  rm -f "${WAV}"

  # 再生完了直後。preempt されていれば残り chunk をスキップ
  if ! vvread_session_is_current "${SESSION_FILE}" "${SESSION_ID}"; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=post_play"
    exit 0
  fi

  i=$((i + 1))
done

log_info "say done chunks=${CHUNK_TOTAL} session=${SESSION_ID}"
exit 0
