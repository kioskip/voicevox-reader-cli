#!/bin/bash
# lib/os.sh - OS 判定ヘルパー (S-010)
#
# source して使う。set は呼ばない(caller の strict mode を尊重、
# doc/08-bash-rules.md §2 参照)。

# OS 判定。"Darwin" のみ macOS、それ以外は Linux 同等扱い
_vvread_is_macos() {
  [ "$(uname -s)" = "Darwin" ]
}
