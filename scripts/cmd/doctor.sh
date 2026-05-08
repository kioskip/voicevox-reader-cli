#!/bin/bash
# scripts/cmd_doctor.sh - vvread doctor subcommand (R-009)
#
# scripts/doctor.py を Python で起動するだけの薄いラッパー。
# venv の python を優先して使い、無ければ system python3 にフォールバック。
# 引数はそのまま透過(--offline / --scope / --json / --strict)。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

exec "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/doctor.py" "$@"
