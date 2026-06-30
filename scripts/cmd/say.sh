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

# キュー再生モード (B-015)
# shellcheck source=../lib/queue.sh
source "${VVREAD_SCRIPTS_DIR}/lib/queue.sh"

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

# ===== キュー再生モード分岐（preempt パスより前段・B-015）=====
#
# 【最重要】queue 分岐は下段の vvread_kill_play / vvread_session_start より
# 前に置く。前段を通すと queue モードが自分の drainer を kill し、session
# 上書きで自殺してサイレント no-op になる。queue パスは preempt パス
# (QUEUE_MODE=0) を丸ごとバイパスする別モード。preempt パスは一切変更しない。

# queue モード解決（優先順位: per-call > env > STATE_DIR flag > 既定 preempt）
_vvread_resolve_queue_mode() {
  case "${QUEUE_OVERRIDE:-}" in
    on)  echo 1; return 0 ;;
    off) echo 0; return 0 ;;
  esac
  case "${VVREAD_SAY_QUEUE:-}" in
    1) echo 1; return 0 ;;
    0) echo 0; return 0 ;;
    "") ;;
    *) log_warn "say invalid VVREAD_SAY_QUEUE=${VVREAD_SAY_QUEUE} — falling back to flag" ;;
  esac
  if [ -f "${STATE_DIR}/queue_mode" ]; then echo 1; else echo 0; fi
}

# drain: playing/<entry> を sanitize→chunk→synth→play。
# 戻り値: 0=完走 / 1=synth 失敗（playing に残し orphan retry へ委ねる）/ 2=halt(stop)
#         3=skip / 4=lost-lock（queue.lock を回収された。再生せず playing に残し退場）
_vvread_drain_one_entry() {
  local entry="$1"
  # Reset per entry to the drainer baseline speed (local prevents cross-entry leak).
  local VOICEVOX_SPEED="${VOICEVOX_SPEED:-1.5}"
  export VOICEVOX_SPEED
  local espk _normalized="" etext _first_line _entry_speed="" chunked total idx wav srraw prc
  espk=$(vvread_queue_entry_field "${entry}" speaker)
  _queue_is_uint "${espk}" || espk="${SPEAKER}"
  _first_line=$(head -n 1 "${QDIR}/playing/${entry}" 2>/dev/null || true)
  case "${_first_line}" in
    '#vvread speed='*)
      # vvread-controlled header with speed. Strip line 1 in all cases so body
      # content is never lost. Invalid value falls back to baseline speed.
      _entry_speed="${_first_line#'#vvread speed='}"
      if _normalized=$(_vvread_speed_normalize "${_entry_speed}"); then
        VOICEVOX_SPEED="${_normalized}"
      else
        log_warn "queue: invalid speed in entry metadata (${_entry_speed}) — using baseline"
      fi
      etext=$(tail -n +2 "${QDIR}/playing/${entry}" 2>/dev/null || echo "")
      ;;
    '#vvread')
      # vvread-controlled header without speed. Strip line 1; use baseline speed.
      etext=$(tail -n +2 "${QDIR}/playing/${entry}" 2>/dev/null || echo "")
      ;;
    *)
      # Legacy entries (pre-v0.4.3, no header): treat whole file as body.
      etext=$(cat "${QDIR}/playing/${entry}" 2>/dev/null || echo "")
      ;;
  esac
  [ -n "${etext}" ] || return 0

  chunked=$(vvread_chunk_split "${etext}" "${espk}" "${PYTHON}" "${VVREAD_SCRIPTS_DIR}")
  [ -n "${chunked}" ] || return 0
  local chunks=()
  while IFS= read -r line; do
    [ -n "${line}" ] && chunks+=("${line}")
  done <<< "${chunked}"
  total=${#chunks[@]}
  [ "${total}" -gt 0 ] || return 0

  idx=0
  while [ "${idx}" -lt "${total}" ]; do
    # 各 chunk 再生前に halt/skip 判定（自分の token と一致する signal のみ）。
    # halt(stop) を skip より先に評価（concurrent stop 優先）。
    if vvread_queue_should_halt "${QDIR}" "${DRAINER_TOKEN}"; then
      return 2
    fi
    if vvread_queue_should_skip "${QDIR}" "${DRAINER_TOKEN}"; then
      return 3
    fi
    # ownership: synth 開始前。lock を失っていたら synth せず退場（二重再生回避）。
    vvread_queue_owns_lock "${QDIR}" "${DRAINER_TOKEN}" || return 4
    wav="${QWAV}_${idx}.wav"
    srraw=0
    vvread_say_synth_chunk "${idx}" "${chunks[${idx}]}" "${wav}" "${espk}" \
      "${total}" "${ENGINES[0]}" || srraw=$?
    if [ "${srraw}" -ne 0 ]; then
      rm -f "${wav}"
      log_info "say drain synth_failed entry_chunk=$((idx + 1))/${total}"
      return 1
    fi
    # ownership: synth 完了直後。lost なら再生せず退場。
    if ! vvread_queue_owns_lock "${QDIR}" "${DRAINER_TOKEN}"; then
      rm -f "${wav}"
      return 4
    fi
    vvread_queue_progress "${QDIR}" "${DRAINER_TOKEN}"  # 実処理進捗（synth 完了）
    # 注: drain は現状 ENGINES[0] 固定。multi-engine drain は別タスク。
    _engine="${ENGINES[0]:-unknown}"
    log_info "say drain play chunk=$((idx + 1))/${total} speaker=${espk} engine=${_engine}"
    prc=0
    vvread_say_play_chunk "${idx}" "${wav}" "${PID_FILE}" "${total}" || prc=$?
    rm -f "${wav}"
    if [ "${prc}" -eq 4 ]; then
      # 再生中に lock を失った（helper が player 停止）: playing に残し退場
      return 4
    elif [ "${prc}" -ne 0 ]; then
      return 1
    fi
    vvread_queue_progress "${QDIR}" "${DRAINER_TOKEN}"  # 実処理進捗（chunk 再生完了）
    idx=$((idx + 1))
  done
  # 最終 chunk 再生中/後に届いた halt を取りこぼさない（単一/最終 chunk の
  # stop.request 残留対策。Phase 2 の skip consume-and-clear の前段にもなる）
  if vvread_queue_should_halt "${QDIR}" "${DRAINER_TOKEN}"; then
    return 2
  fi
  return 0
}

# drainer のメインループ。queue.lock 保持前提（DRAINER_TOKEN 設定済み）。
_vvread_drain_loop() {
  local entry drc tok _empty_pop_streak=0
  while true; do
    # 前 drainer の crash 残骸を retry+1 で回収（mutation lock 内）
    if tok=$(_queue_mutate_lock); then
      # set -e 下で recover が非ゼロ復帰しても mutation lock を leak させない
      vvread_queue_recover_orphans || true
      _queue_mutate_unlock "${tok}"
    fi

    if ! entry=$(vvread_queue_pop); then
      # 空 → release（abort 前に必ず自分の token で release）。
      # lost-wakeup 対策で release 後に pending 再スキャン。
      vvread_queue_release "${QDIR}/queue.lock" "${DRAINER_TOKEN}"
      if [ "$(_queue_count "${QDIR}/pending")" -gt 0 ]; then
        # pending>0 なのに pop が空 = wedge シグネチャ（mutate 取得失敗等）。
        # bounded retry で打ち切り、queue.lock は release 済みのまま次の say へ
        # 引き継がせる（無限 spin 防止の安全網。self-reclaim をすり抜けた残余対策）。
        _empty_pop_streak=$((_empty_pop_streak + 1))
        if [ "${_empty_pop_streak}" -ge "${_QUEUE_EMPTY_POP_MAX:-20}" ]; then
          log_warn "say drain abort: pending>0 but pop empty x${_empty_pop_streak} (wedge) — yielding to next say"
          DRAINER_TOKEN=""
          return 0
        fi
        if DRAINER_TOKEN=$(vvread_queue_acquire "${QDIR}/queue.lock"); then
          continue
        fi
      fi
      DRAINER_TOKEN=""
      return 0
    fi
    _empty_pop_streak=0   # pop 成功でリセット（spin 中は progress も更新しない）

    # pop 成功直後: ownership 検証。回収されていたら entry を playing に残し退場
    # （二重再生回避。release しない＝もう自分の token ではない）。
    if ! vvread_queue_owns_lock "${QDIR}" "${DRAINER_TOKEN}"; then
      log_warn "say drain lost queue.lock after pop entry=${entry}"
      DRAINER_TOKEN=""
      return 0
    fi
    vvread_queue_progress "${QDIR}" "${DRAINER_TOKEN}"  # 実処理進捗（pop 成功）

    drc=0
    _vvread_drain_one_entry "${entry}" || drc=$?
    case "${drc}" in
      1)
        # synth/play 失敗: playing に残し orphan recovery の retry に委ねる
        log_info "say drain entry_failed_retry_pending entry=${entry}"
        ;;
      2)
        # halt(stop): 現エントリを discard。skip/stop signal を消し release して exit。
        _queue_safe_rm "${QDIR}/playing/${entry}"
        vvread_queue_clear_skip "${QDIR}" "${DRAINER_TOKEN}"
        vvread_queue_clear_stop "${QDIR}"
        vvread_queue_release "${QDIR}/queue.lock" "${DRAINER_TOKEN}"
        DRAINER_TOKEN=""
        log_info "say drain halted entry=${entry}"
        return 0
        ;;
      3)
        # skip: 現エントリを discard。token 一致 skip signal を consume し次 entry へ。
        _queue_safe_rm "${QDIR}/playing/${entry}"
        vvread_queue_clear_skip "${QDIR}" "${DRAINER_TOKEN}"
        log_info "say drain skipped entry=${entry}"
        ;;
      4)
        # lost-lock: queue.lock を回収された。playing entry は残す（failed 移動・
        # 削除をしない）。release もしない（自分の token ではない）。退場。
        log_warn "say drain lost queue.lock during entry=${entry}"
        DRAINER_TOKEN=""
        return 0
        ;;
      *)
        # 完走: 単一/最終 chunk の skip 残留を consume-and-clear（token 一致のみ）
        _queue_safe_rm "${QDIR}/playing/${entry}"
        vvread_queue_clear_skip "${QDIR}" "${DRAINER_TOKEN}"
        ;;
    esac
  done
}

QUEUE_MODE=$(_vvread_resolve_queue_mode)
if [ "${QUEUE_MODE}" = "1" ]; then
  vvread_queue_dirs_init
  PID_FILE="${STATE_DIR}/playing.pid"
  QWAV="${STATE_DIR}/qvoice_$$"
  DRAINER_TOKEN=""
  SAY_SOURCE="${VVREAD_SAY_SOURCE:-cli}"

  # drain 中の wav と queue.lock を必ず後始末（trap は requeue しない・
  # playing entry は残し次 drainer の回収に委ねる）
  _vvread_queue_cleanup() {
    rm -f "${QWAV}"_*.wav 2>/dev/null || true
    if [ -n "${DRAINER_TOKEN:-}" ]; then
      vvread_queue_release "${QDIR}/queue.lock" "${DRAINER_TOKEN}" 2>/dev/null || true
    fi
  }
  trap _vvread_queue_cleanup EXIT

  _submit_rc=0
  vvread_queue_submit "${SAY_SOURCE}" "${SPEAKER}" "${TEXT}" "${SPEED_OVERRIDE:-}" || _submit_rc=$?
  _text_preview="${TEXT:0:10}"
  _text_preview="${_text_preview//$'\n'/ }"
  _text_preview="${_text_preview//$'\r'/}"
  _text_preview="${_text_preview//$'\t'/ }"
  log_info "say enqueue source=${SAY_SOURCE} speaker=${SPEAKER} rc=${_submit_rc} text_chars=${#TEXT} text_from=${_text_preview}"

  # drainer になれれば drain、なれなければ既存 drainer に委ねて即終了。
  # acquire 失敗時は heartbeat-stale な drainer（生存だが進捗なし）を回収して再挑戦。
  if DRAINER_TOKEN=$(vvread_queue_acquire "${QDIR}/queue.lock"); then
    log_info "say drainer_start session_pid=$$"
    _vvread_drain_loop
  else
    vvread_queue_reclaim_drain_stale "${QDIR}"
    if DRAINER_TOKEN=$(vvread_queue_acquire "${QDIR}/queue.lock"); then
      log_info "say drainer_start session_pid=$$ (reclaimed stale drainer)"
      _vvread_drain_loop
    else
      log_info "say queued (drainer active)"
    fi
  fi
  exit 0
fi

# preempt モードのみ: --speed を env export で子プロセス(cache_key.py)に伝播。
# queue モードでは speed は entry metadata (#vvread speed=N) で per-entry 管理するため
# ここで export すると _vvread_drain_one_entry の baseline が汚染される（Codex P2 #2）。
if [ -n "${SPEED_OVERRIDE:-}" ]; then
  export VOICEVOX_SPEED="${SPEED_OVERRIDE}"
fi

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
