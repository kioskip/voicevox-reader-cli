#!/bin/bash
# scripts/cmd/speakers.sh - vvread speakers subcommand (B-017)
#
# scripts/speakers.py を Python で起動するだけの薄いラッパー。
# venv の python を優先して使い、無ければ system python3 にフォールバック。
# 引数はそのまま透過 (--engine-url)。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

export VVREAD_PROJECT_DIR

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

exec "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/speakers.py" "$@"
