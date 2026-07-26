#!/bin/bash
# voice.sh - voiceClaude の発話制御 CLI
#
# サブコマンド:
#   vvread stop              現在再生中の音を即停止(将来の発話は維持)
#   vvread mute <duration>   一定時間ミュート(例: 30s, 5m, 2h)。期限後は自動復帰
#   vvread unmute            ミュートだけを解除(off 状態は維持)
#   vvread off               永続オフ(`vvread on` まで)
#   vvread on                復帰
#   vvread status [--json]   現状表示
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
# L-4: 共有ホストで他ユーザーに読まれないよう umask 077 で新規作成する
# (lib/queue.sh::vvread_queue_dirs_init と統一)。
( umask 077; mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${CACHE_DIR}" )
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

  # 空 or 数字以外 → 何もせず pid_file を消す(lib/playback.sh::vvread_kill_play と同じガード)
  case "${pid}" in
    ""|*[!0-9]*)
      rm -f "${PLAY_PID_FILE}"
      return 0
      ;;
  esac

  # PID 0 は POSIX で「呼出側プロセスグループ全体に signal 送信」を意味し危険。
  # playing.pid が "0" に汚染されているケースを拒否する。
  if [ "${pid}" -eq 0 ] 2>/dev/null; then
    rm -f "${PLAY_PID_FILE}"
    return 0
  fi

  if kill -0 "${pid}" 2>/dev/null; then
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

# queue モード時は全停止（pending 削除 + drainer へ token 付き halt signal）も担う。
# 順序: stop.request → pending 削除 → lock 解放（ここまで queue_stop_request 内の
# mutation lock）。queue mode flag（${STATE_DIR}/queue_mode）は維持し、ここでは
# 消さない（`vvread queue off` でのみ削除する既存の責務分担）。
# 判定基準は「queue_mode フラグ（永続 ON、cmd/queue.sh の on/off と同一基準）」
# OR「queue ディレクトリ存在」の OR 条件にする。`vvread say --queue` /
# `VVREAD_SAY_QUEUE=1` による per-call（1回限り）queueing は queue_mode フラグ
# を一切触らずに queue ディレクトリと drainer を作るため（cmd/say.sh の
# `_vvread_resolve_queue_mode()` 参照）、queue_mode フラグのみの判定だと
# per-call queue 使用中の stop/mute/off がそのdrainerへ届かなくなる
# （F-128 で判定基準を `-d` → `-f` に変えた際の回帰）。
_queue_stop_if_active() {
  if [ -f "${STATE_DIR}/queue_mode" ] || [ -d "${STATE_DIR}/queue" ]; then
    vvread_queue_dirs_init
    # wedge した drainer は stop signal を読まない。自動 reset はせず WARN のみ
    # （破壊操作は明示 `vvread queue reset` に限定）。pending 削除前に判定する。
    if [ "$(vvread_queue_lock_class "${QDIR}")" = "wedge" ]; then
      printf 'WARN: queue drainer appears wedged. Run `vvread queue reset` to force recovery.\n' >&2
    fi
    vvread_queue_stop_request "${QDIR}" || true
  fi
}

_load_mute_until() {
  [ -f "${MUTE_UNTIL_FILE}" ] || { echo ""; return; }
  cat "${MUTE_UNTIL_FILE}" 2>/dev/null || echo ""
}

_format_until() {
  date -r "$1" +%H:%M:%S
}

_is_alive_pid() {
  local pid="${1:-}"
  # 空 or 数字以外 → alive とはみなさない
  case "${pid}" in
    ""|*[!0-9]*) return 1 ;;
  esac
  # PID 0 は「呼出側プロセスグループ全体」を指すため alive 判定から除外する
  [ "${pid}" -eq 0 ] 2>/dev/null && return 1
  kill -0 "${pid}" 2>/dev/null
}

# status --json 用に mute_until を検証する。
# 有効な未来 epoch は数値を、不在・空・期限切れ・不正値は null を stdout へ返す。
# 不正値だけは stderr に警告し、期限切れファイルは人間向け status と同様に削除する。
_json_mute_until() {
  local now="$1"
  local until
  until=$(_load_mute_until)

  if [ -z "${until}" ]; then
    echo "null"
    return
  fi

  case "${until}" in
    *[!0-9]*)
      printf '警告: mute_until の値が不正です: %s\n' "${until}" >&2
      echo "null"
      return
      ;;
  esac

  # JSON 数値は先頭ゼロを許さないため、比較・出力前に 1 桁になるまで除去する。
  while [ "${#until}" -gt 1 ] && [ "${until#0}" != "${until}" ]; do
    until="${until#0}"
  done

  if [ "${until}" -gt "${now}" ]; then
    echo "${until}"
    return
  fi

  rm -f "${MUTE_UNTIL_FILE}"
  echo "null"
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
  # 順序: queue stop request → player kill（_stop_current）。
  _queue_stop_if_active
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
  # フラグ書込みを先に行い、drainer 側が再開しうる競合窓を狭めてから停止要求を送る。
  _queue_stop_if_active
  _stop_current
  log_info "mute duration=${arg} until=${until}"
  echo "muted for ${arg} (until $(_format_until "${until}"))"
}

cmd_unmute() {
  rm -f "${MUTE_UNTIL_FILE}"
  if [ -f "${DISABLED_FILE}" ]; then
    echo "ミュートを解除しました（読み上げはオフのままです）"
  else
    echo "ミュートを解除しました"
  fi
}

cmd_off() {
  touch "${DISABLED_FILE}"
  # フラグ書込みを先に行い、drainer 側が再開しうる競合窓を狭めてから停止要求を送る。
  _queue_stop_if_active
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
  # -print0 | while read -d '' でスペース/改行含みパスを word-split せず処理する
  # (L-1: 従来の `xargs rm -f` は macOS の
  #  `~/Library/Application Support/vvread` のようなスペース含みパスで
  #  word-split して実際には何も削除しないのに「removed N」と表示していた)
  local count=0 f
  while IFS= read -r -d '' f; do
    rm -f -- "${f}" && count=$((count + 1))
  done < <(find "${STATE_DIR}" -maxdepth 1 \( \
              \( -name "voice_*" ! -name "voice_${current}_*" \) \
              -o -name "query_*.json" \
              -o -name "query_*.tuned" \
            \) -print0 2>/dev/null)

  if [ "${count}" -eq 0 ]; then
    echo "nothing to clean."
    log_info "clean files=0 session=${current}"
    return 0
  fi

  log_info "clean files=${count} session=${current}"
  echo "removed ${count} file(s)."

  # CACHE_DIR の wav を全削除（macOS 互換）
  if [ -d "${CACHE_DIR}" ]; then
    find "${CACHE_DIR}" -type f -name "*.wav" -exec rm -f {} \; 2>/dev/null || true
    log_info "clean: removed cached wav from ${CACHE_DIR}"
  fi
}

cmd_status() {
  if [ "${1:-}" = "--json" ]; then
    local now until state pid mode pending playing failed
    now=$(date +%s)
    until=$(_json_mute_until "${now}")

    if [ -f "${DISABLED_FILE}" ]; then
      state="disabled"
    elif [ "${until}" != "null" ]; then
      state="muted"
    else
      pid=$(cat "${PLAY_PID_FILE}" 2>/dev/null || echo "")
      if _is_alive_pid "${pid}"; then
        state="playing"
      else
        state="idle"
      fi
    fi

    mode="off"
    [ -f "${STATE_DIR}/queue_mode" ] && mode="on"
    pending=$(_queue_count "${STATE_DIR}/queue/pending")
    playing=$(_queue_count "${STATE_DIR}/queue/playing")
    failed=$(_queue_count "${STATE_DIR}/queue/failed")

    printf '{"state": "%s", "mute_until": %s, "queue": {"mode": "%s", "pending": %s, "playing": %s, "failed": %s}}\n' \
      "${state}" "${until}" "${mode}" "${pending}" "${playing}" "${failed}"
    return
  fi

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
  unmute            ミュートだけを解除(off 状態は維持)
  off               永続オフ(\`vvread on\` まで)
  on                復帰
  status [--json]   現状表示
  clean             state ディレクトリの orphan(別セッションの voice_*)を掃除
EOF
  exit 1
}

case "${1:-}" in
  stop)    shift; cmd_stop "$@" ;;
  mute)    shift; cmd_mute "$@" ;;
  unmute)  shift; cmd_unmute "$@" ;;
  off)     shift; cmd_off "$@" ;;
  on)      shift; cmd_on "$@" ;;
  status)  shift; cmd_status "$@" ;;
  clean)   shift; cmd_clean "$@" ;;
  ""|-h|--help) usage ;;
  *) echo "unknown command: $1" >&2; usage ;;
esac
