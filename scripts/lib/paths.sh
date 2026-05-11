#!/bin/bash
# scripts/lib_paths.sh - OS 別パス resolver (R-001)
#
# source して使う。set は呼ばない(caller の strict mode を尊重、
# doc/08-bash-rules.md §2 参照)。副作用なし(mkdir は呼ばない、
# 呼び出し側責務)。
#
# 提供する関数:
#   vvread_state_dir   - state ディレクトリを stdout に出力
#   vvread_log_dir     - log ディレクトリを stdout に出力
#   vvread_cache_dir   - cache ディレクトリを stdout に出力
#
# 優先順位: VVREAD_*_DIR 環境変数 > OS 既定値
# OS 別既定値は scripts/paths.py と完全一致(tests/test_paths.py で固定)。
#
# WSL / Git Bash は Linux と同じ XDG パスにフォールバック(uname -s
# で Darwin 以外なら全部 Linux 同等扱い、Backlog 確定事項通り)。

# S-010: _vvread_is_macos は lib/os.sh に集約
# shellcheck source=./os.sh
source "$(dirname "${BASH_SOURCE[0]}")/os.sh"

# `~` のみ展開する単純な expanduser($VAR は扱わない、Python 側 _expand と整合)。
# 主に override(VVREAD_*_DIR の値)経由で渡される `~/foo` を展開するためのもの。
# OS 既定値の文字列は最初から ${HOME} を使うので tilde を通らない。
# 末尾スラッシュもここで剥がす。
_vvread_expand() {
  local p="$1"
  # tilde 展開: `~` 単独、または `~/...` の場合のみ HOME に置換。
  # case の quoted "~" パターンが SC2088 で誤検出されるため if/else で書く。
  if [ "$p" = "~" ]; then
    p="${HOME}"
  elif [ "${p#"~/"}" != "$p" ]; then
    p="${HOME}/${p#"~/"}"
  fi
  # 末尾スラッシュ正規化(Python の Path() と挙動を揃える)
  while [ "${p}" != "/" ] && [ "${p%/}" != "${p}" ]; do
    p="${p%/}"
  done
  printf '%s\n' "$p"
}

vvread_state_dir() {
  local override="${VVREAD_STATE_DIR:-}"
  if [ -n "${override}" ]; then
    _vvread_expand "${override}"
    return
  fi
  if _vvread_is_macos; then
    _vvread_expand "${HOME}/Library/Application Support/vvread"
  else
    local base="${XDG_STATE_HOME:-${HOME}/.local/state}"
    _vvread_expand "${base}/vvread"
  fi
}

vvread_log_dir() {
  local override="${VVREAD_LOG_DIR:-}"
  if [ -n "${override}" ]; then
    _vvread_expand "${override}"
    return
  fi
  if _vvread_is_macos; then
    _vvread_expand "${HOME}/Library/Logs/vvread"
  else
    local base="${XDG_STATE_HOME:-${HOME}/.local/state}"
    _vvread_expand "${base}/vvread/logs"
  fi
}

vvread_cache_dir() {
  local override="${VVREAD_CACHE_DIR:-}"
  if [ -n "${override}" ]; then
    _vvread_expand "${override}"
    return
  fi
  if _vvread_is_macos; then
    _vvread_expand "${HOME}/Library/Caches/vvread"
  else
    local base="${XDG_CACHE_HOME:-${HOME}/.cache}"
    _vvread_expand "${base}/vvread"
  fi
}

# 旧 ${PROJECT_DIR}/tmp/ から新しい OS 別ディレクトリへの初回移行。
# 旧ディレクトリは削除しない(ロールバック余地)。新側に既存があれば上書き
# しない(idempotent、複数 entry script から呼ばれても安全)。
#
# 移行対象: ユーザ操作の状態(disabled / mute_until)、通知 cooldown
# (last_notify)、wav キャッシュ(cache/*.wav)、ログ(logs/speak.log)。
# session.id / playing.pid / voice_*.wav は per-session ephemeral なので
# 移行対象外(次回起動で再生成される)。
#
# 第 1 引数: 旧 tmp ディレクトリ (例: "${PROJECT_DIR}/tmp")
vvread_migrate_legacy_tmp() {
  local legacy_dir="$1"
  [ -d "${legacy_dir}" ] || return 0

  local state_dir cache_dir log_dir
  state_dir=$(vvread_state_dir)
  cache_dir=$(vvread_cache_dir)
  log_dir=$(vvread_log_dir)

  # 単一ファイルの copy: 新側に存在しない時のみ
  local name
  for name in disabled mute_until last_notify; do
    if [ -e "${legacy_dir}/${name}" ] && [ ! -e "${state_dir}/${name}" ]; then
      mkdir -p "${state_dir}"
      cp -p "${legacy_dir}/${name}" "${state_dir}/${name}" 2>/dev/null || true
    fi
  done

  # wav キャッシュ: ファイルごとに新側を確認(部分的な移行も対応)
  if [ -d "${legacy_dir}/cache" ]; then
    local wav base
    for wav in "${legacy_dir}/cache"/*.wav; do
      [ -f "${wav}" ] || continue   # glob unmatched
      base=$(basename "${wav}")
      if [ ! -e "${cache_dir}/${base}" ]; then
        mkdir -p "${cache_dir}"
        cp -p "${wav}" "${cache_dir}/${base}" 2>/dev/null || true
      fi
    done
  fi

  # ログ: 新側に既存があればスキップ(append 状態を壊さない)
  if [ -f "${legacy_dir}/logs/speak.log" ] && [ ! -f "${log_dir}/speak.log" ]; then
    mkdir -p "${log_dir}"
    cp -p "${legacy_dir}/logs/speak.log" "${log_dir}/speak.log" 2>/dev/null || true
  fi
}
