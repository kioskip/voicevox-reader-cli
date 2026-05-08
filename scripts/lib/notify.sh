#!/bin/bash
# lib_notify.sh - macOS 通知センターへの通知ヘルパー
#
# source して使う。呼び出し側で STATE_DIR が定義されている前提
# (lib_paths.sh の vvread_state_dir で取得した値)。
# 連続発火時の抑制(クールダウン)も内蔵する。
#
# 使い方:
#   notify_error "title" "message"
#
# 通知バックエンド:
#   1. terminal-notifier(brew install terminal-notifier)があれば優先。
#      macOS Sequoia 以降の osascript display notification は通知許可周りで
#      サイレント失敗することがあるため、自前バンドルを持つ terminal-notifier
#      の方が信頼できる。
#   2. 無ければ osascript にフォールバック。それも無ければ何もせず exit 0。
#
# 環境変数:
#   VOICEVOX_NOTIFY_COOLDOWN  通知抑制の秒数 (default 60)。
#                             直前の通知から N 秒以内なら次の通知をスキップ。

NOTIFY_COOLDOWN_SEC="${VOICEVOX_NOTIFY_COOLDOWN:-60}"
NOTIFY_LAST_FILE="${STATE_DIR}/last_notify"

# osascript 用のエスケープ。ダブルクオートとバックスラッシュをエスケープし、
# 改行は空白に置換する(複数行通知は読みにくいため)。
# terminal-notifier は -title/-message 引数経由なのでエスケープ不要。
_notify_escape() {
  printf '%s' "$1" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

_notify_within_cooldown() {
  [ -f "${NOTIFY_LAST_FILE}" ] || return 1
  local last now
  last=$(cat "${NOTIFY_LAST_FILE}" 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $((now - last)) -lt "${NOTIFY_COOLDOWN_SEC}" ]
}

notify_error() {
  local title="$1"
  local message="$2"

  if _notify_within_cooldown; then
    return 0
  fi
  date +%s > "${NOTIFY_LAST_FILE}"

  # terminal-notifier 優先(自前の通知バンドルを持つため許可状態が安定)
  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "${title}" -message "${message}" -group "vvread" \
      >/dev/null 2>&1 || true
    return 0
  fi

  # フォールバック: osascript display notification
  command -v osascript >/dev/null 2>&1 || return 0
  local t m
  t=$(_notify_escape "${title}")
  m=$(_notify_escape "${message}")
  osascript -e "display notification \"${m}\" with title \"${t}\"" 2>/dev/null || true
}
