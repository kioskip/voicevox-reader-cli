#!/bin/bash
# scripts/cmd_play.sh - vvread play subcommand (R-028)
#
# Usage: cmd_play.sh <wav>
#
# 既存 wav ファイルを再生する。合成はしない。
# lib_playback.sh の vvread_play_async / vvread_kill_play を使用。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

# bin/vvread から呼ばれる場合は VVREAD_*_DIR が export 済み。直接実行(scripts/
# から)でも動くよう自前で path を解決する。
VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# OS 別 path 解決
# shellcheck source=../lib/paths.sh
source "${VVREAD_SCRIPTS_DIR}/lib/paths.sh"
STATE_DIR="$(vvread_state_dir)"
LOG_DIR="$(vvread_log_dir)"
# L-4: 共有ホストで他ユーザーに読まれないよう umask 077 で新規作成する
# (lib/queue.sh::vvread_queue_dirs_init と統一)。
( umask 077; mkdir -p "${STATE_DIR}" "${LOG_DIR}" )

# 共通ロガー
# shellcheck disable=SC2034
LOG_NAME="play"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"

# 再生抽象層 (R-002)
# shellcheck source=../lib/playback.sh
source "${VVREAD_SCRIPTS_DIR}/lib/playback.sh"

# ===== usage =====

usage() {
  cat >&2 <<'EOF'
Usage: vvread play <wav>

  <wav>   再生する wav ファイル(必須)

設定可能な環境変数:
  VVREAD_PLAYER   player バイナリの明示指定(自動検出を上書き)
                  例: paplay / pw-play / aplay / play / ffplay / 絶対パス
EOF
  exit 1
}

# ===== 引数 parse =====

if [ $# -lt 1 ]; then
  printf 'vvread play: <wav> is required\n' >&2
  usage
fi

case "$1" in
  -h|--help)
    usage
    ;;
esac

WAV="$1"
shift

if [ $# -gt 0 ]; then
  printf 'vvread play: too many positional arguments\n' >&2
  exit 1
fi

# ===== wav 存在確認 =====

if [ ! -e "${WAV}" ]; then
  printf 'vvread play: wav file not found: %s\n' "${WAV}" >&2
  exit 1
fi
if [ ! -s "${WAV}" ]; then
  printf 'vvread play: wav file is empty: %s\n' "${WAV}" >&2
  exit 1
fi

# ===== 再生 =====

PID_FILE="${STATE_DIR}/playing.pid"

log_info "play start wav=${WAV}"

play_rc=0
vvread_play_async "${WAV}" "${PID_FILE}" || play_rc=$?

case "${play_rc}" in
  0)
    : # 起動成功
    ;;
  1)
    log_info "play failed reason=no_player"
    printf 'vvread play: no audio player available.\n' >&2
    if [ "$(uname -s)" != "Darwin" ]; then
      printf 'Hint: install one of paplay (PulseAudio), pw-play (PipeWire), aplay (ALSA), play (sox), ffplay (FFmpeg).\n' >&2
      printf '      Or set VVREAD_PLAYER to a player binary path.\n' >&2
    fi
    exit 1
    ;;
  2)
    # 上で -e / -s チェック済みなのでここには来ないはずだが念のため
    log_info "play failed reason=wav_missing"
    printf 'vvread play: wav file not found or empty: %s\n' "${WAV}" >&2
    exit 1
    ;;
  *)
    log_info "play failed reason=launch_error rc=${play_rc}"
    printf 'vvread play: unexpected error from player launch (rc=%s)\n' "${play_rc}" >&2
    exit 1
    ;;
esac

# pid_file の PID を読んで wait
PID="$(cat "${PID_FILE}" 2>/dev/null || echo "")"
if [ -z "${PID}" ]; then
  log_info "play failed reason=pid_file_empty"
  printf 'vvread play: failed to start player (no pid recorded)\n' >&2
  exit 1
fi

log_info "play running pid=${PID}"

# wait の exit code が player の exit code を反映する
wait_rc=0
wait "${PID}" 2>/dev/null || wait_rc=$?

# T-013: PID_FILE が自分の PID のままなら消す。別 say に上書き済みなら触らない
# (cmd_say と同じ防御。残 race は v0.1 許容、必要なら flock 化)
CURRENT_PID="$(cat "${PID_FILE}" 2>/dev/null || echo "")"
if [ "${CURRENT_PID}" = "${PID}" ]; then
  rm -f "${PID_FILE}"
fi

if [ "${wait_rc}" -ne 0 ]; then
  log_info "play exited rc=${wait_rc}"
  printf 'vvread play: player exited with code %s\n' "${wait_rc}" >&2
  exit "${wait_rc}"
fi

log_info "play done"
exit 0
