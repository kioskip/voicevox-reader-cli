#!/bin/bash
# scripts/cmd/queue.sh - vvread queue subcommand (B-015)
#
# キュー再生モードの制御プレーン。
#   vvread queue on        キュー再生モードを有効化（永続フラグ）
#   vvread queue off        無効化（pending/playing が残っていれば拒否）
#   vvread queue status     mode / pending / playing / failed 件数を表示
#   vvread queue clear      pending のみ削除（再生中は継続）
#
# 全停止（再生停止 + pending 削除）は `vvread stop`、現エントリのみ停止は
# `vvread queue skip`（B-144）が担当する。本コマンドは状態フラグと clear のみ。
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

# 共通ロガー
# shellcheck disable=SC2034
LOG_NAME="queue"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"

# Playback 抽象層（skip で player kill に使用）
# shellcheck source=../lib/playback.sh
source "${VVREAD_SCRIPTS_DIR}/lib/playback.sh"

# duration parser（failed cleanup --ttl 用）
# shellcheck source=../lib/duration.sh
source "${VVREAD_SCRIPTS_DIR}/lib/duration.sh"

# キュー lib
# shellcheck source=../lib/queue.sh
source "${VVREAD_SCRIPTS_DIR}/lib/queue.sh"

vvread_queue_dirs_init

usage() {
  cat >&2 <<'EOF'
Usage: vvread queue <command>

  on        キュー再生モードを有効化（割り込まず順番に再生）
  off       キュー再生モードを無効化（pending/playing が空のときのみ）
  status    mode / pending / playing / failed の件数を表示
  clear     pending を削除（再生中のエントリは継続）
  skip      再生中の現エントリのみ停止し次の pending へ進む
  reset     【破壊的】wedge した drainer を強制停止し queue 状態を backup へ退避
            （lock/pending/playing/failed を削除でなく timestamp backup へ mv）
  failed <list|rm|clear|cleanup>
            retry 上限超で退避した failed entry を管理（cleanup は手動実行のみ）

関連: `vvread stop`（全停止 + pending 削除）
EOF
}

cmd_on() {
  touch "${STATE_DIR}/queue_mode"
  log_info "queue mode on"
  echo "queue mode: on"
}

cmd_off() {
  local p pl
  p=$(_queue_count "${QDIR}/pending")
  pl=$(_queue_count "${QDIR}/playing")
  if [ "${p}" -ne 0 ] || [ "${pl}" -ne 0 ]; then
    printf 'ERROR: queue is not empty (pending=%s playing=%s)\n' "${p}" "${pl}" >&2
    printf 'Run `vvread stop` before `vvread queue off`.\n' >&2
    exit 1
  fi
  rm -f "${STATE_DIR}/queue_mode"
  log_info "queue mode off"
  echo "queue mode: off"
}

cmd_clear() {
  vvread_queue_clear
  log_info "queue clear (pending)"
  echo "queue cleared (pending)"
}

cmd_skip() {
  # 再生中エントリが無ければ no-op
  if [ "$(_queue_count "${QDIR}/playing")" -eq 0 ]; then
    echo "nothing playing"
    exit 0
  fi
  if vvread_queue_skip_request "${QDIR}"; then
    # live drainer へ skip signal 済み。現 player を kill して即座に次へ送る。
    vvread_kill_play "${STATE_DIR}/playing.pid"
    log_info "queue skip"
    echo "skip: current entry stopped"
  else
    # playing はあるが live drainer 不在（orphan）
    echo "nothing playing"
  fi
}

# 【破壊的】wedge 復旧用の強制リセット。drainer kill + 全 queue 状態の backup 退避。
cmd_reset() {
  printf 'WARNING: `queue reset` is destructive — it kills the queue drainer and\n' >&2
  printf '         backs up all queue state (lock/pending/playing/failed) to a\n' >&2
  printf '         timestamped backup dir under the state directory.\n' >&2
  local backup
  backup=$(vvread_queue_reset "${QDIR}")
  vvread_kill_play "${STATE_DIR}/playing.pid"
  log_warn "queue reset (drainer stopped, state backed up to ${backup:-<none>})"
  if [ -n "${backup}" ]; then
    printf 'queue reset: state backed up to %s\n' "${backup}"
  else
    printf 'queue reset: done\n'
  fi
}

cmd_failed() {
  local sub="${2:-}"
  case "${sub}" in
    list)
      local out
      out=$(vvread_queue_failed_list)
      if [ -z "${out}" ]; then
        echo "no failed entries"
        return 0
      fi
      printf '%-14s %-14s %-7s %-6s %s\n' "FAILED_MS" "CREATED_MS" "SPEAKER" "SOURCE" "RETRY"
      printf '%s\n' "${out}"
      ;;
    rm)
      local name="${3:-}"
      if [ -z "${name}" ]; then
        echo "Usage: vvread queue failed rm <entry>" >&2
        exit 2
      fi
      local rc=0
      vvread_queue_failed_rm "${QDIR}" "${name}" || rc=$?
      case "${rc}" in
        0) echo "removed: ${name}" ;;
        2) printf 'invalid entry name: %s\n' "${name}" >&2; exit 2 ;;
        *) printf 'not found: %s\n' "${name}" >&2; exit 1 ;;
      esac
      ;;
    clear)
      vvread_queue_failed_clear
      echo "failed cleared"
      ;;
    cleanup)
      local ttl="7d"
      if [ "${3:-}" = "--ttl" ]; then
        ttl="${4:-}"
        if [ -z "${ttl}" ]; then
          echo "Usage: vvread queue failed cleanup [--ttl <duration>]" >&2
          exit 2
        fi
      elif [ -n "${3:-}" ]; then
        echo "Usage: vvread queue failed cleanup [--ttl <duration>]" >&2
        exit 2
      fi
      local sec
      if ! sec=$(vvread_parse_duration "${ttl}"); then
        printf 'invalid duration: %s (例: 30s, 10m, 2h, 7d)\n' "${ttl}" >&2
        exit 2
      fi
      local removed
      removed=$(vvread_queue_failed_cleanup "${QDIR}" "$((sec * 1000))")
      echo "removed ${removed} failed entries (TTL ${ttl})"
      ;;
    "")
      echo "Usage: vvread queue failed <list|rm|clear|cleanup>" >&2
      exit 2
      ;;
    *)
      printf 'vvread queue failed: unknown subcommand: %s\n' "${sub}" >&2
      echo "Usage: vvread queue failed <list|rm|clear|cleanup>" >&2
      exit 2
      ;;
  esac
}

case "${1:-}" in
  on)     cmd_on ;;
  off)    cmd_off ;;
  status) vvread_queue_status ;;
  clear)  cmd_clear ;;
  skip)   cmd_skip ;;
  reset)  cmd_reset ;;
  failed) cmd_failed "$@" ;;
  -h|--help|"") usage; [ -z "${1:-}" ] && exit 2 || exit 0 ;;
  *)
    printf 'vvread queue: unknown subcommand: %s\n' "$1" >&2
    usage
    exit 2
    ;;
esac
