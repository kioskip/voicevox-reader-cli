#!/bin/bash
# scripts/cmd/mcp.sh - bin/vvread mcp サブコマンド (B-110)
#
# vvread の主要コマンドを MCP サーバとして公開する。
# FastMCP (mcp>=1,<2) を使用した stdio transport 実装。
#
# Python 解決順:
#   1. VVREAD_MCP_PYTHON 環境変数（明示指定）
#   2. repo/.venv/bin/python（import mcp が成功する場合）
#   3. python3（import mcp が成功する場合）
#   4. 全候補で失敗 → 案内付き exit 1
#
# 必要パッケージ: uv sync --extra mcp  (Python >=3.10)
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

# import mcp が成功する Python を探して echo する。失敗時は return 1。
_try_python() {
  local py="$1"
  [ -x "${py}" ] || return 1
  "${py}" -c "import mcp" 2>/dev/null || return 1
  echo "${py}"
}

PYTHON=""

# 1. VVREAD_MCP_PYTHON 環境変数（明示指定）
if [ -n "${VVREAD_MCP_PYTHON:-}" ]; then
  PYTHON="$(_try_python "${VVREAD_MCP_PYTHON}" || true)"
fi

# 2. repo/.venv/bin/python
if [ -z "${PYTHON}" ]; then
  PYTHON="$(_try_python "${VVREAD_PROJECT_DIR}/.venv/bin/python" || true)"
fi

# 3. python3 (PATH 上)
if [ -z "${PYTHON}" ]; then
  _py3="$(command -v python3 2>/dev/null || true)"
  if [ -n "${_py3}" ]; then
    PYTHON="$(_try_python "${_py3}" || true)"
  fi
fi

# 全候補で失敗 → 案内付き exit 1
if [ -z "${PYTHON}" ]; then
  echo "vvread mcp: mcp package not found." >&2
  echo "  Install: uv sync --extra mcp  (Python >=3.10 required)" >&2
  exit 1
fi

exec "${PYTHON}" "${VVREAD_PROJECT_DIR}/scripts/mcp_server.py" "$@"
