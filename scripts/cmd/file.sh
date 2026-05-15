#!/bin/bash
# scripts/cmd/file.sh - ファイル読み上げ (B-002)
#
# Usage: file.sh <path> [--speaker N]
#
# 指定ファイルの内容を読み込み、検証したうえで cmd/say.sh に委譲する。
# chunk / synth / play の処理は say.sh に任せる。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# ===== 引数パース =====
# <path> が先頭の位置引数。それ以降は say.sh にそのまま渡す say options。

if [ $# -eq 0 ]; then
  printf 'vvread file: <file> is required\n' >&2
  exit 1
fi

case "$1" in
  -h|--help)
    printf 'Usage: vvread file <path> [--speaker N]\n' >&2
    exit 1
    ;;
  -*)
    printf 'vvread file: expected <path>, got option: %s\n' "$1" >&2
    exit 1
    ;;
esac

file_path="$1"
shift  # 残り $@ は --speaker 等の say options

# ===== ファイル検証 =====

if [ ! -f "${file_path}" ]; then
  printf 'vvread file: file not found: %s\n' "${file_path}" >&2
  exit 1
fi

if [ ! -r "${file_path}" ]; then
  printf 'vvread file: file not readable: %s\n' "${file_path}" >&2
  exit 1
fi

# ===== ファイル読み込み =====

text=$(cat "${file_path}")

if [ -z "${text}" ]; then
  printf 'vvread file: file is empty: %s\n' "${file_path}" >&2
  exit 1
fi

# NOTE: 大容量ファイルは後段 VOICEVOX_MAX_CHARS で truncation 制御される

# ===== say.sh に委譲 =====
exec "${VVREAD_SCRIPTS_DIR}/cmd/say.sh" "${text}" "$@"
