#!/bin/bash
# scripts/cmd/url.sh - URL読み上げ (B-003)
#
# Usage: url.sh <url> [--speaker N]
#
# URLからWebページ本文を取得し、cmd/say.sh に委譲する。
# 本文抽出は fetch_url.py、chunk/synth/play は say.sh に任せる。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# ===== 引数パース =====

if [ $# -eq 0 ]; then
  printf 'vvread url: <url> is required\n' >&2
  exit 1
fi

case "$1" in
  -h|--help)
    printf 'Usage: vvread url <url> [--speaker N]\n' >&2
    exit 1
    ;;
  -*)
    printf 'vvread url: expected <url>, got option: %s\n' "$1" >&2
    exit 1
    ;;
esac

url="$1"
shift  # 残り $@ は --speaker 等の say options

# ===== URL 検証（shell 層; scheme prefix のみ） =====
# scheme/userinfo/redirect の詳細検証は fetch_url.py 側で行う

case "${url}" in
  http://*|https://*)
    ;;
  *)
    printf 'vvread url: invalid URL (http:// or https:// required): %s\n' "${url}" >&2
    exit 1
    ;;
esac

# ===== Python 解決 =====

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
[ -x "${PYTHON}" ] || PYTHON="python3"

# ===== Webコンテンツ取得 =====

text=$("${PYTHON}" "${VVREAD_SCRIPTS_DIR}/fetch_url.py" "${url}")

if [ -z "${text}" ]; then
  printf 'vvread url: no content fetched from: %s\n' "${url}" >&2
  exit 1
fi

# ===== say.sh に委譲 =====

exec "${VVREAD_SCRIPTS_DIR}/cmd/say.sh" "${text}" "$@"
