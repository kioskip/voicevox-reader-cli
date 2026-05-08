#!/bin/bash
# scripts/cmd_uninstall.sh - vvread uninstall subcommand (R-008)
#
# scripts/hook_install.py を Python で起動するだけの薄いラッパー。
# venv の python を優先して使い、無ければ system python3 にフォールバック。
# 引数はそのまま透過(--scope / --dry-run)。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# hook_install.py が repo_root を絶対パスで埋め込めるよう export
export VVREAD_PROJECT_DIR

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

exec "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/hook_install.py" uninstall "$@"
