#!/bin/bash
# scripts/lib_playback.sh - 音声再生プレイヤーの抽象層 (R-002)
#
# source して使う。set は呼ばない(caller の strict mode を尊重、
# doc/08-bash-rules.md §2 参照)。
#
# 提供する関数:
#   vvread_detect_player         - 利用可能な player 名を stdout に出力
#                                  (見つからなければ exit 1 + 空 stdout)
#   vvread_play_async <wav> <pid_file>
#                                - 非同期再生開始、PID を pid_file に書く
#                                - 戻り値: 0=起動 / 1=player 不在 / 2=wav 不在
#                                - 「exec 直後に終了」の検出はしない(bash 3.2 で
#                                  zombie と alive を区別する移植性ある手段が無いため)。
#                                  caller は `wait $pid` で異常終了を検知できる
#   vvread_kill_play <pid_file>  - pid_file の PID を kill。常に 0 を返す(noop 安全)
#
# 優先順位(VVREAD_PLAYER > OS 自動検出):
#   - VVREAD_PLAYER 環境変数: 明示指定。不在の場合は 1 を返す(fallback しない、
#                              ユーザの意図を尊重)
#   - macOS (Darwin): afplay
#   - Linux/WSL: paplay > pw-play > aplay > play(sox) > ffplay
#   - Git Bash 等: 該当なし。空を返し caller/doctor 側で WSL 推奨を案内
#
# エラー時の warning 出力は lib 側では行わず caller (vvread say / doctor)
# に任せる。

# S-010: _vvread_is_macos は lib/os.sh に集約
# shellcheck source=./os.sh
source "$(dirname "${BASH_SOURCE[0]}")/os.sh"

# Linux 系の player 優先順を空白区切りで返す(bash 3.2 互換、配列ではなく文字列)
_vvread_linux_player_priority() {
  echo "paplay pw-play aplay play ffplay"
}

# 検出ロジック本体。stdout に player 名、見つからなければ exit 1。
vvread_detect_player() {
  # VVREAD_PLAYER 明示指定が最優先。不在なら fallback しない
  if [ -n "${VVREAD_PLAYER:-}" ]; then
    if command -v "${VVREAD_PLAYER}" >/dev/null 2>&1; then
      printf '%s\n' "${VVREAD_PLAYER}"
      return 0
    fi
    return 1
  fi

  local candidates p
  if _vvread_is_macos; then
    candidates="afplay"
  else
    candidates="$(_vvread_linux_player_priority)"
  fi

  for p in ${candidates}; do
    if command -v "${p}" >/dev/null 2>&1; then
      printf '%s\n' "${p}"
      return 0
    fi
  done
  return 1
}

# Player ごとの引数を組み立て、グローバル配列 _vvread_play_cmd に格納する。
# bash 3.2 では関数から配列を返せないため、global で受け渡す方式を採用。
# 内部関数(`_` prefix)。caller は通常 vvread_play_async 経由で利用する。
_vvread_build_play_command() {
  local player="$1"
  local wav="$2"
  case "${player}" in
    afplay)
      _vvread_play_cmd=(afplay "${wav}")
      ;;
    paplay)
      _vvread_play_cmd=(paplay "${wav}")
      ;;
    pw-play)
      _vvread_play_cmd=(pw-play "${wav}")
      ;;
    aplay)
      # -q: ヘッダ等のデバッグ出力を抑止
      _vvread_play_cmd=(aplay -q "${wav}")
      ;;
    play)
      # sox の play。-q で stderr の冗長出力を抑止
      _vvread_play_cmd=(play -q "${wav}")
      ;;
    ffplay)
      # -nodisp(GUI 抑止) / -autoexit(再生後終了) / -loglevel quiet(ログ抑止)
      _vvread_play_cmd=(ffplay -nodisp -autoexit -loglevel quiet "${wav}")
      ;;
    *)
      # 想定外の player(VVREAD_PLAYER で任意指定された場合)。引数なしで素直に呼ぶ
      _vvread_play_cmd=("${player}" "${wav}")
      ;;
  esac
}

# 非同期で再生開始。pid_file に PID を書く。
# 戻り値:
#   0 = 起動成功(pid_file 書き込み済)
#   1 = player 不在(VVREAD_PLAYER override + 自動検出 両方ヒットなし)
#   2 = wav ファイル不在 or 空
#
# 起動後の player が即終了するケース(command not found / 引数不正 / device
# busy)は本関数では検出しない。bash 3.2 では zombie process と alive process
# を `kill -0` で区別する移植性ある手段が無く、`wait` は alive 時にブロック
# するため。caller は `wait $pid` で完了を待つ際に exit code を見ることで
# 異常終了を検知できる(speak.sh の既存パターンと整合)。
vvread_play_async() {
  local wav="$1"
  local pid_file="$2"

  if [ ! -s "${wav}" ]; then
    return 2
  fi

  local player
  if ! player=$(vvread_detect_player); then
    return 1
  fi

  _vvread_build_play_command "${player}" "${wav}"

  # bg 起動。stderr/stdout は捨てる(player によって冗長出力が違うため統一)
  "${_vvread_play_cmd[@]}" >/dev/null 2>&1 &
  local pid=$!

  printf '%s\n' "${pid}" > "${pid_file}"
  return 0
}

# pid_file の PID を kill。ファイル/PID が不正でも noop 安全。
# 常に 0 を返す。pid_file が存在すれば最後に削除する。
vvread_kill_play() {
  local pid_file="$1"
  [ -f "${pid_file}" ] || return 0

  local pid
  pid=$(cat "${pid_file}" 2>/dev/null || echo "")

  # 空 or 数字以外 → 何もせず pid_file を消す
  case "${pid}" in
    ""|*[!0-9]*)
      rm -f "${pid_file}"
      return 0
      ;;
  esac

  # PID 0 は POSIX で「呼出側プロセスグループ全体に signal 送信」の意味で危険。
  # 0 や 0 のみで構成された値("00" 等)はすべて拒否する。
  if [ "${pid}" -eq 0 ] 2>/dev/null; then
    rm -f "${pid_file}"
    return 0
  fi

  if kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
  fi
  rm -f "${pid_file}"
  return 0
}
