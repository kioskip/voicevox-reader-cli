#!/bin/bash
# lib/chunk.sh - sanitize + chunk_split パイプライン (S-011)
#
# source して使う。set は呼ばない（caller の strict mode を尊重）。
# lib 依存なし。
#
# 提供する関数:
#   vvread_chunk_split <text> <speaker> <python> <scripts_dir>
#     sanitize.py → chunk_split.py に通し、チャンク一覧を改行区切りでstdoutに出力。
#     caller が「空の場合の exit」を判断する。

vvread_chunk_split() {
  local text="$1" speaker="$2" python="$3" scripts_dir="$4"
  printf '%s' "${text}" \
    | "${python}" "${scripts_dir}/sanitize.py" \
    | "${python}" "${scripts_dir}/chunk_split.py" --speaker "${speaker}" \
    || true  # 既存 say.sh の挙動維持: sanitize / chunk_split の失敗は空出力に変換し、
             # caller 側の既存の空チェック（[ -z "${CHUNKED}" ]）に委ねる
}
