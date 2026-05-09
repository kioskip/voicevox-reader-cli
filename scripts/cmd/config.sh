#!/bin/bash
# scripts/cmd/config.sh - vvread config / edit subcommand (B-014)
#
# scripts/config.py を Python で起動するだけの薄いラッパー。
# `vvread config` と `vvread edit` の両方がこのスクリプトに dispatch される。
# venv の python を優先して使い、無ければ system python3 にフォールバック。
# 引数はそのまま透過 (--dry-run)。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

export VVREAD_PROJECT_DIR

PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

exec "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/config.py" "$@"
