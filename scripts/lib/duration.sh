#!/bin/bash
# lib/duration.sh - duration 文字列を秒に変換 (FB-4 / B-145)
#
# source して使う。set は呼ばない（caller の strict mode を尊重）。
# lib 依存なし。
#
# 提供する関数:
#   vvread_parse_duration <value>
#     受理: <正整数><s|m|h|d>（例: 30s / 10m / 2h / 7d）。秒を stdout に出力し 0。
#     拒否: 空 / 非数値 / 小数 / 負数 / 未知 suffix → return 1（stdout なし）。
#
# 注: voice.sh の旧 _parse_duration（s/m/h のみ）を一般化したもの。`d`（日）を
#     追加サポート。voice.sh / cmd/queue.sh の双方が source して使う。

vvread_parse_duration() {
  local raw="${1:-}"
  if [[ "${raw}" =~ ^([0-9]+)([smhd])$ ]]; then
    local n="${BASH_REMATCH[1]}"
    case "${BASH_REMATCH[2]}" in
      s) printf '%s\n' "${n}" ;;
      m) printf '%s\n' "$((n * 60))" ;;
      h) printf '%s\n' "$((n * 3600))" ;;
      d) printf '%s\n' "$((n * 86400))" ;;
    esac
    return 0
  fi
  return 1
}
