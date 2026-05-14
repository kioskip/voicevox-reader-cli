#!/bin/bash
# lib/session.sh - セッショントークン管理 (S-011)
#
# source して使う。set は呼ばない（caller の strict mode を尊重）。
# 前提: lib/log.sh を source 済みであること（_now_ms 依存）。
# 前提: session_file の親ディレクトリは caller が作成済みであること。
#       書き込み失敗は caller の strict mode（set -e）に委ねる。
#
# 提供する関数:
#   vvread_session_start <session_file>
#     新しいセッション ID を生成して session_file に書き込み、stdout にエコー。
#   vvread_session_is_current <session_file> <session_id>
#     session_file の内容が session_id と一致すれば 0、不一致なら 1 を返す。
#     ファイル不在・読み取りエラーは「不一致」として扱う（返り値 1）。

# shellcheck source=./log.sh
# （_now_ms を log.sh から参照。lint.sh の --external-sources で解決される）

vvread_session_start() {
  local session_file="$1"
  local ms session_id
  ms=$(_now_ms)
  session_id="${ms}_$$"
  printf '%s\n' "${session_id}" > "${session_file}"
  printf '%s\n' "${session_id}"
}

vvread_session_is_current() {
  local session_file="$1" session_id="$2"
  local current
  current=$(cat "${session_file}" 2>/dev/null || echo "")
  [ "${current}" = "${session_id}" ]
}
