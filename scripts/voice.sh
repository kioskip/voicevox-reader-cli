#!/bin/bash
# voice.sh - voiceClaude の発話制御 CLI
#
# サブコマンド:
#   vvread stop              現在再生中の音を即停止(将来の発話は維持)
#   vvread mute <duration>   一定時間ミュート(例: 30s, 5m, 2h)。期限後は自動復帰
#   vvread off               永続オフ(`vvread on` まで)
#   vvread on                復帰
#   vvread status            現状表示
#   vvread clean             ${STATE_DIR} 内の orphan と ${CACHE_DIR} の wav を一括削除。具体的には以下:
#                             - 別セッションの voice_*.wav / .wav.query.json / .wav.query.json.tuned
#                             - 旧 QUERY_PREFIX 形式の query_*.json / .tuned(S-001 以前の遺物)
#                             - ${CACHE_DIR}/*.wav（定型フレーズ wav キャッシュ）
#                           現セッション(${STATE_DIR}/session.id)の voice_${current}_* は保護する。
#                           状態ファイル(session.id / playing.pid / disabled /
#                           mute_until / last_notify)、prefix 無しファイル(test.wav 等)、
#                           ${LOG_DIR}/ には触れない。

set -e

PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# OS 別パス(R-001)+ 旧 tmp/ からの移行(R-003)
# shellcheck source=./lib/paths.sh
source "${PROJECT_DIR}/scripts/lib/paths.sh"
STATE_DIR="$(vvread_state_dir)"
LOG_DIR="$(vvread_log_dir)"
CACHE_DIR="$(vvread_cache_dir)"
mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${CACHE_DIR}"
vvread_migrate_legacy_tmp "${PROJECT_DIR}/tmp"

PLAY_PID_FILE="${STATE_DIR}/playing.pid"
SESSION_FILE="${STATE_DIR}/session.id"
DISABLED_FILE="${STATE_DIR}/disabled"
MUTE_UNTIL_FILE="${STATE_DIR}/mute_until"

# 共通ロガー(log_info / log_debug / _now_ms を提供)。タグは "voice"
# LOG_NAME は source 後の lib_log.sh が ${LOG_NAME:-speak} で読む。
# shellcheck disable=SC2034
LOG_NAME="voice"
# shellcheck source=./lib/log.sh
source "${VVREAD_SCRIPTS_DIR:-$(dirname "$0")}/lib/log.sh"

# キュー再生モード (B-015): stop が queue 全停止も担うため source する
# shellcheck source=./lib/queue.sh
source "${VVREAD_SCRIPTS_DIR:-$(dirname "$0")}/lib/queue.sh"

# duration parser (FB-4): mute の duration 解析。cmd/queue.sh と共有
# shellcheck source=./lib/duration.sh
source "${VVREAD_SCRIPTS_DIR:-$(dirname "$0")}/lib/duration.sh"


# ===== ヘルパー(状態の読み書き) =====

_kill_playing_afplay() {
  [ -f "${PLAY_PID_FILE}" ] || return 0
  local pid
  pid=$(cat "${PLAY_PID_FILE}" 2>/dev/null || echo "")
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
  fi
  rm -f "${PLAY_PID_FILE}"
}

_invalidate_session() {
  echo "stopped_$(date +%s)" > "${SESSION_FILE}"
}

_stop_current() {
  _kill_playing_afplay
  _invalidate_session
}

_load_mute_until() {
  [ -f "${MUTE_UNTIL_FILE}" ] || { echo ""; return; }
  cat "${MUTE_UNTIL_FILE}" 2>/dev/null || echo ""
}

_format_until() {
  date -r "$1" +%H:%M:%S
}

_is_alive_pid() {
  [ -n "$1" ] && kill -0 "$1" 2>/dev/null
}

# ===== ヘルパー(状態を行で出力) =====

_print_state_disabled() {
  echo "state: disabled"
}

_print_state_muted() {
  local until="$1"
  local remain=$(( until - $(date +%s) ))
  echo "state: muted (残り ${remain}s, until $(_format_until "${until}"))"
}

_print_state_playing() {
  echo "state: playing (pid=$1)"
}

_print_state_idle() {
  echo "state: idle"
}


# ===== サブコマンド =====

cmd_stop() {
  # queue モード時は全停止（pending 削除 + drainer へ token 付き halt signal）も担う。
  # 順序: stop.request → pending 削除 → lock 解放（ここまで queue_stop_request 内の
  # mutation lock）→ player kill（_stop_current）。queue mode flag は維持。
  if [ -d "${STATE_DIR}/queue" ]; then
    vvread_queue_dirs_init
    # wedge した drainer は stop signal を読まない。自動 reset はせず WARN のみ
    # （破壊操作は明示 `vvread queue reset` に限定）。pending 削除前に判定する。
    if [ "$(vvread_queue_lock_class "${QDIR}")" = "wedge" ]; then
      printf 'WARN: queue drainer appears wedged. Run `vvread queue reset` to force recovery.\n' >&2
    fi
    vvread_queue_stop_request "${QDIR}" || true
  fi
  _stop_current
  log_info "stop"
  echo "stopped"
}

cmd_mute() {
  local arg="${1:-}"
  if [ -z "${arg}" ]; then
    echo "Usage: vvread mute <duration>  (例: 30s, 5m, 2h)" >&2
    exit 1
  fi
  local sec
  if ! sec=$(vvread_parse_duration "${arg}"); then
    echo "duration の形式が不正: ${arg} (例: 30s, 5m, 2h)" >&2
    exit 1
  fi
  local until=$(( $(date +%s) + sec ))
  echo "${until}" > "${MUTE_UNTIL_FILE}"
  _stop_current
  log_info "mute duration=${arg} until=${until}"
  echo "muted for ${arg} (until $(_format_until "${until}"))"
}

cmd_off() {
  touch "${DISABLED_FILE}"
  _stop_current
  log_info "off"
  echo "読み上げを無効にしました（再開するには \`vvread on\` を実行してください）"
}

cmd_on() {
  rm -f "${DISABLED_FILE}" "${MUTE_UNTIL_FILE}"
  log_info "on"
  echo "読み上げを有効にしました"
}

cmd_clean() {
  # 現セッションの prefix を除外して voice_* を削除する。
  # session.id が無い場合は __none__ をダミー prefix とすることで全件削除になる。
  # (旧 tmp/speak.log → tmp/logs/speak.log の intra-tmp 移行は R-003 で
  #  vvread_migrate_legacy_tmp に置き換わったため、ここでは扱わない)
  local current
  current=$(cat "${SESSION_FILE}" 2>/dev/null || echo "__none__")
  [ -z "${current}" ] && current="__none__"

  # 削除候補:
  #   1. 現セッション以外の voice_* (wav / .wav.query.json / .wav.query.json.tuned)
  #   2. 旧 QUERY_PREFIX 形式の query_*.json / query_*.tuned(S-001 以前の遺物。
  #      現状は voice_* prefix に統一されているため、これらは全件 orphan として安全に消せる)
  local matches
  matches=$(find "${STATE_DIR}" -maxdepth 1 \( \
              \( -name "voice_*" ! -name "voice_${current}_*" \) \
              -o -name "query_*.json" \
              -o -name "query_*.tuned" \
            \) 2>/dev/null || true)

  if [ -z "${matches}" ]; then
    echo "nothing to clean."
    log_info "clean files=0 session=${current}"
    return 0
  fi

  local count
  count=$(printf '%s\n' "${matches}" | wc -l | tr -d ' ')
  printf '%s\n' "${matches}" | xargs rm -f
  log_info "clean files=${count} session=${current}"
  echo "removed ${count} file(s)."

  # CACHE_DIR の wav を全削除（macOS 互換）
  if [ -d "${CACHE_DIR}" ]; then
    find "${CACHE_DIR}" -type f -name "*.wav" -exec rm -f {} \; 2>/dev/null || true
    log_info "clean: removed cached wav from ${CACHE_DIR}"
  fi
}

cmd_status() {
  if [ -f "${DISABLED_FILE}" ]; then
    _print_state_disabled
    return
  fi

  local until
  until=$(_load_mute_until)
  if [ -n "${until}" ]; then
    if [ "$(date +%s)" -lt "${until}" ]; then
      _print_state_muted "${until}"
      return
    fi
    rm -f "${MUTE_UNTIL_FILE}"
  fi

  if [ -f "${PLAY_PID_FILE}" ]; then
    local pid
    pid=$(cat "${PLAY_PID_FILE}" 2>/dev/null || echo "")
    if _is_alive_pid "${pid}"; then
      _print_state_playing "${pid}"
      return
    fi
  fi

  _print_state_idle
}


# ===== entrypoint =====

usage() {
  cat >&2 <<EOF
Usage: vvread <command>

Commands:
  stop              現在再生中の音を即停止(将来の発話は維持)
  mute <duration>   一定時間ミュート (例: 30s, 5m, 2h)
  off               永続オフ(\`vvread on\` まで)
  on                復帰
  status            現状表示
  clean             state ディレクトリの orphan(別セッションの voice_*)を掃除
EOF
  exit 1
}

case "${1:-}" in
  stop)   shift; cmd_stop "$@" ;;
  mute)   shift; cmd_mute "$@" ;;
  off)    shift; cmd_off "$@" ;;
  on)     shift; cmd_on "$@" ;;
  status) shift; cmd_status "$@" ;;
  clean)  shift; cmd_clean "$@" ;;
  ""|-h|--help) usage ;;
  *) echo "unknown command: $1" >&2; usage ;;
esac
