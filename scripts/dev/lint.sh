#!/bin/bash
# scripts/dev/lint.sh - bash スクリプトの shellcheck + Bash 3.2 互換チェック
#
# 警告ゼロ運用 (R-023 + R-026)。新規 bash ファイルが追加されたら自動で対象になる。
# 詳細ルール: doc/08-bash-rules.md

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "${PROJECT_DIR}"

# ----- 0. shellcheck の存在確認 -----

if ! command -v shellcheck >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[ERROR] shellcheck not installed.

Install:
  macOS:  brew install shellcheck
  Ubuntu: sudo apt install shellcheck
  Arch:   sudo pacman -S shellcheck

Reference: https://github.com/koalaman/shellcheck#installing
EOF
  exit 2
fi

# ----- 1. 対象ファイル収集 -----
# scripts/ + bin/ + publish/ 配下の .sh、または shebang が bash の実行可能ファイル

TARGETS=()
while IFS= read -r f; do
  TARGETS+=("$f")
done < <(
  {
    find scripts -type f -name "*.sh" 2>/dev/null
    find bin -type f 2>/dev/null
    find publish -type f -name "*.sh" 2>/dev/null
  } | sort -u
)

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "no bash files found."
  exit 0
fi

# ----- 2. shellcheck 実行 -----

echo "==> shellcheck (--shell=bash --severity=warning --external-sources) on ${#TARGETS[@]} file(s)"
shellcheck --shell=bash --severity=warning --external-sources "${TARGETS[@]}"

# ----- 3. Bash 4+ 機能の grep 検出 (R-023) -----
# 3.2 互換チェック専用フラグが shellcheck に無いため自前 grep で補う。
# パターンを追加するときは doc/08-bash-rules.md §1 表も同期する。

echo "==> Bash 3.2 compat check (grep)"

declare -a BAD_PATTERNS=(
  'declare[[:space:]]+-A'                # 連想配列 (4.0+)
  '\bmapfile\b'                          # mapfile (4.0+)
  '\breadarray\b'                        # readarray (4.0+)
  '\$\{[A-Za-z_][A-Za-z0-9_]*\^\^'       # ${var^^} 大文字変換 (4.0+)
  '\$\{[A-Za-z_][A-Za-z0-9_]*,,'         # ${var,,} 小文字変換 (4.0+)
  '\$\{[A-Za-z_][A-Za-z0-9_]*@[ULu]\}'   # ${var@U/L/u} (4.4+)
  '\bcoproc\b'                           # coproc (4.0+)
  '\[\[[[:space:]]+-v[[:space:]]'        # [[ -v var ]] (4.2+)
)

# grep 対象から lint.sh 自身を除外(自身がパターン定義の registry なので
# self-match する)。BASH_SOURCE で「実行中のこのスクリプト」の相対パスを
# 取り、TARGETS から落とす。
SELF_REL="scripts/dev/lint.sh"
GREP_TARGETS=()
for f in "${TARGETS[@]}"; do
  if [ "${f}" != "${SELF_REL}" ]; then
    GREP_TARGETS+=("${f}")
  fi
done

violations=0
if [ "${#GREP_TARGETS[@]}" -gt 0 ]; then
  for pat in "${BAD_PATTERNS[@]}"; do
    # 誤爆を避けるため grep の出力は変数に取り、後段でまとめて表示
    matches=$(grep -EnH "${pat}" "${GREP_TARGETS[@]}" 2>/dev/null || true)
    if [ -n "${matches}" ]; then
      echo "[Bash 3.2 violation: ${pat}]"
      echo "${matches}"
      violations=$((violations + 1))
    fi
  done
fi

if [ "${violations}" -gt 0 ]; then
  echo "==> ${violations} compat violation(s) found. See doc/08-bash-rules.md §1." >&2
  exit 1
fi

echo "==> all checks passed (${#TARGETS[@]} file(s))"
