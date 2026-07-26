#!/bin/bash
# scripts/cmd/menubar.sh - bin/vvread menubar サブコマンド (B-151)
#
# rumps 製 macOS メニューバー常駐 UI を起動する。
# 非 darwin では案内付きで exit 1(macOS 専用機能)。
#
# Python 解決順:
#   1. VVREAD_MENUBAR_PYTHON 環境変数(明示指定)
#   2. repo/.venv/bin/python(import rumps が成功する場合)
#   3. 全候補で失敗 → 案内付き exit 1(`uv sync` を促す。rumps は pyproject.toml の
#      sys_platform=='darwin' marker でコア依存として自動解決される)
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

# ----- 0. 非 darwin は案内付き exit 1 -----

if [ "$(uname -s)" != "Darwin" ]; then
  echo "vvread menubar: macOS 専用機能です(現在の OS では利用できません)。" >&2
  echo "  対象 OS: macOS (Darwin)" >&2
  exit 1
fi

# ----- 1. rumps が import 可能な Python を探す -----

# import rumps が成功する Python を探して echo する。失敗時は return 1。
_try_python() {
  local py="$1"
  [ -x "${py}" ] || return 1
  "${py}" -c "import rumps" 2>/dev/null || return 1
  echo "${py}"
}

PYTHON=""

# 1. VVREAD_MENUBAR_PYTHON 環境変数(明示指定)
if [ -n "${VVREAD_MENUBAR_PYTHON:-}" ]; then
  PYTHON="$(_try_python "${VVREAD_MENUBAR_PYTHON}" || true)"
fi

# 2. repo/.venv/bin/python
if [ -z "${PYTHON}" ]; then
  PYTHON="$(_try_python "${VVREAD_PROJECT_DIR}/.venv/bin/python" || true)"
fi

# 全候補で失敗 → 案内付き exit 1
if [ -z "${PYTHON}" ]; then
  echo "vvread menubar: rumps package not found." >&2
  echo "  Install: uv sync  # pyproject.toml の sys_platform=='darwin' marker で" >&2
  echo "           rumps がコア依存として自動解決されます(macOS のみ、追加 --extra 不要)" >&2
  exit 1
fi

exec "${PYTHON}" "${VVREAD_PROJECT_DIR}/scripts/menubar.py" "$@"
