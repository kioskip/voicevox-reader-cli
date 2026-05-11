#!/bin/bash
# scripts/cmd_synth.sh - vvread synth subcommand (R-028)
#
# Usage: cmd_synth.sh <text> --output FILE [--speaker N]
#
# テキストを VOICEVOX で合成し wav として FILE に書き出す。再生はしない。
# vvread say (R-005) は本コマンド + cmd_play.sh を順に呼ぶ orchestrator になる予定。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

# bin/vvread から呼ばれる場合は VVREAD_*_DIR が export 済み。直接実行(scripts/
# から)でも動くよう自前で path を解決する。
VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# OS 別 path 解決
# shellcheck source=../lib/paths.sh
source "${VVREAD_SCRIPTS_DIR}/lib/paths.sh"
LOG_DIR="$(vvread_log_dir)"
mkdir -p "${LOG_DIR}"

# settings.py で設定を一括解決(env > project > user > default)
# log.sh source より前に eval することで log.level も反映される
PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
[ -x "${PYTHON}" ] || PYTHON="python3"
eval "$("${PYTHON}" "${VVREAD_SCRIPTS_DIR}/settings.py" env 2>/dev/null || true)"

# 共通ロガー
# LOG_NAME は source 後の lib_log.sh が ${LOG_NAME:-speak} で読む。
# shellcheck disable=SC2034
LOG_NAME="synth"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"

# VOICEVOX HTTP API ヘルパー
# shellcheck source=../lib/voicevox.sh
source "${VVREAD_SCRIPTS_DIR}/lib/voicevox.sh"

# ===== usage =====

usage() {
  cat >&2 <<'EOF'
Usage: vvread synth <text> --output FILE [--speaker N]

  <text>          合成するテキスト(必須)
  --output FILE   出力 wav ファイルパス(必須)。--output=FILE 形式も可
  --speaker N     話者 ID (default: VOICEVOX_SPEAKER 環境変数 or 3)

設定可能な環境変数:
  VOICEVOX_ENGINE_URL   VOICEVOX Engine URL (default: http://127.0.0.1:50021)
  VOICEVOX_SPEAKER      話者 ID (default: 3)
  VOICEVOX_SPEED        速度倍率 (default: 1.5)
  VOICEVOX_PITCH        ピッチ (default: 0)
  VOICEVOX_INTONATION   イントネーション (default: 1.0)
  VOICEVOX_VOLUME       音量 (default: 1.0)
  VOICEVOX_PAUSE_SCALE  ポーズ長倍率 (default: 1.0)
  VOICEVOX_PRE_PHONEME  発話前無音(秒) (default: 0)
  VOICEVOX_POST_PHONEME 発話後無音(秒) (default: 0)
EOF
  exit 1
}

# ===== 引数 parse =====

TEXT=""
OUTPUT=""
SPEAKER_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --output)
      if [ $# -lt 2 ]; then
        printf 'vvread synth: --output requires an argument\n' >&2
        exit 1
      fi
      OUTPUT="$2"
      shift 2
      ;;
    --output=*)
      OUTPUT="${1#--output=}"
      shift
      ;;
    --speaker)
      if [ $# -lt 2 ]; then
        printf 'vvread synth: --speaker requires an argument\n' >&2
        exit 1
      fi
      SPEAKER_OVERRIDE="$2"
      shift 2
      ;;
    --speaker=*)
      SPEAKER_OVERRIDE="${1#--speaker=}"
      shift
      ;;
    -h|--help)
      usage
      ;;
    --)
      # 以降は位置引数として扱う(`-` で始まる text を渡したい場合)
      shift
      while [ $# -gt 0 ]; do
        if [ -z "${TEXT}" ]; then
          TEXT="$1"
        else
          printf 'vvread synth: too many positional arguments\n' >&2
          exit 1
        fi
        shift
      done
      break
      ;;
    -*)
      printf 'vvread synth: unknown option: %s\n' "$1" >&2
      exit 1
      ;;
    *)
      if [ -z "${TEXT}" ]; then
        TEXT="$1"
      else
        printf 'vvread synth: too many positional arguments\n' >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [ -z "${TEXT}" ]; then
  printf 'vvread synth: <text> is required\n' >&2
  usage
fi
if [ -z "${OUTPUT}" ]; then
  printf 'vvread synth: --output FILE is required\n' >&2
  usage
fi

# ===== 発話パラメータ =====
# settings.py env の eval で VOICEVOX_* は解決済み(env > project > user > default)。
# voicevox_resolve_speaker は settings.py 失敗時のバックストップとして機能する。
# ENGINE / SPEED_SCALE 等は lib/voicevox.sh が VOICEVOX_* を直読みするため不要(S-006/S-007)。

SPEAKER=$(voicevox_resolve_speaker "${SPEAKER_OVERRIDE}")

# ===== 出力先ディレクトリ作成 =====

OUTPUT_DIR=$(dirname "${OUTPUT}")
mkdir -p "${OUTPUT_DIR}"

# ===== 合成 =====

log_info "synth start text_chars=${#TEXT} speaker=${SPEAKER} output=${OUTPUT}"

synth_rc=0
voicevox_synthesize "${OUTPUT}" "${TEXT}" "${SPEAKER}" "1/1" || synth_rc=$?

if [ "${synth_rc}" -ne 0 ]; then
  log_info "synth failed rc=${synth_rc}"
  printf 'vvread synth: synthesis failed (rc=%s)\n' "${synth_rc}" >&2
  exit 1
fi

log_info "synth done output=${OUTPUT}"
exit 0
