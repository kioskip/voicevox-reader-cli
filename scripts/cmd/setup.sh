#!/bin/bash
# scripts/cmd_setup.sh - vvread setup subcommand (R-010 / R-011)
#
# scripts/setup.py を Python で起動するだけの薄いラッパー。
# venv の python を優先して使い、無ければ system python3 にフォールバック。
# 引数はそのまま透過(--engine / --engine-url / --scope / --yes / --dry-run /
# --skip-engine / --skip-e2k / --skip-hook / --install-e2k / --no-install-e2k
# / --json)。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# setup.py が hook 登録の絶対パス埋め込み等で repo_root を絶対パスで使うため
export VVREAD_PROJECT_DIR

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

exec "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/setup.py" "$@"
