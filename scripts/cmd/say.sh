#!/bin/bash
# scripts/cmd_say.sh - vvread say subcommand (R-005)
#
# Usage: cmd_say.sh <text> [--speaker N]
#
# テキストを VOICEVOX で合成し、再生する。長文は sanitize.py + chunk_split.py
# で chunk 分割し、各 chunk を逐次に「synth → play」する薄い orchestrator。
#
# 設計方針(R-005 スコープ):
#   - prefetch / 並列合成は本コマンドでは行わない(逐次合成 + 同期再生)
#   - 新しい say 起動時に古い playback を kill する(vvread_kill_play)
#   - session token 方式で preemption: 各 chunk の前後で session.id を確認し、
#     新しい session が来ていれば silent に exit 0(古い発話を残さない)
#   - synth は lib_voicevox.sh::voicevox_synthesize、play は lib_playback.sh::
#     vvread_play_async に委譲(R-028 の cmd_synth/cmd_play と同じ層)
#
# 速度改善(prefetch / 並列合成 / キャンセル制御の高度化)は別タスクで扱う。
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# OS 別 path 解決
# shellcheck source=../lib/paths.sh
source "${VVREAD_SCRIPTS_DIR}/lib/paths.sh"
STATE_DIR="$(vvread_state_dir)"
LOG_DIR="$(vvread_log_dir)"
mkdir -p "${STATE_DIR}" "${LOG_DIR}"

# settings.py で設定を一括解決(env > project > user > default)
# log.sh source より前に eval することで log.level も反映される
PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
[ -x "${PYTHON}" ] || PYTHON="python3"
eval "$("${PYTHON}" "${VVREAD_SCRIPTS_DIR}/settings.py" env 2>/dev/null || true)"

# 共通ロガー
# shellcheck disable=SC2034
LOG_NAME="say"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"

# VOICEVOX HTTP API
# shellcheck source=../lib/voicevox.sh
source "${VVREAD_SCRIPTS_DIR}/lib/voicevox.sh"

# Playback 抽象層 (R-002)
# shellcheck source=../lib/playback.sh
source "${VVREAD_SCRIPTS_DIR}/lib/playback.sh"

# ===== usage =====

usage() {
  cat >&2 <<'EOF'
Usage: vvread say <text> [--speaker N]

  <text>          発話するテキスト(必須)
  --speaker N     話者 ID (default: VOICEVOX_SPEAKER 環境変数 or 3)

設定可能な環境変数:
  VOICEVOX_ENGINE_URL   VOICEVOX Engine URL
  VOICEVOX_SPEAKER      話者 ID
  VOICEVOX_SPEED ほか   発話パラメータ(cmd_synth と同じ)
  VVREAD_PLAYER         player バイナリの明示指定
EOF
  exit 1
}

# ===== argparse =====

TEXT=""
SPEAKER_OVERRIDE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --speaker)
      if [ $# -lt 2 ]; then
        printf 'vvread say: --speaker requires an argument\n' >&2
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
      shift
      while [ $# -gt 0 ]; do
        if [ -z "${TEXT}" ]; then
          TEXT="$1"
        else
          printf 'vvread say: too many positional arguments\n' >&2
          exit 1
        fi
        shift
      done
      break
      ;;
    -*)
      printf 'vvread say: unknown option: %s\n' "$1" >&2
      exit 1
      ;;
    *)
      if [ -z "${TEXT}" ]; then
        TEXT="$1"
      else
        printf 'vvread say: too many positional arguments\n' >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [ -z "${TEXT}" ]; then
  printf 'vvread say: <text> is required\n' >&2
  usage
fi

# ===== 発話パラメータ =====
# settings.py env の eval で VOICEVOX_* は解決済み(env > project > user > default)。
# 以下の :- はsettings.py 失敗時のバックストップ。--speaker は最優先。

SPEAKER="${SPEAKER_OVERRIDE:-${VOICEVOX_SPEAKER:-3}}"

# lib_voicevox.sh が dynamic scoping で参照する変数群(R-028 cmd_synth と同じ)
# S-008: ENGINE_URL (settings.py 解決) → ENGINE (legacy) の順で参照
# shellcheck disable=SC2034
ENGINE="${VOICEVOX_ENGINE_URL:-${VOICEVOX_ENGINE:-http://127.0.0.1:50021}}"
ENGINE="${ENGINE%/}"
# shellcheck disable=SC2034
SPEED_SCALE="${VOICEVOX_SPEED:-1.5}"
# shellcheck disable=SC2034
PRE_PHONEME="${VOICEVOX_PRE_PHONEME:-0}"
# shellcheck disable=SC2034
POST_PHONEME="${VOICEVOX_POST_PHONEME:-0}"
# shellcheck disable=SC2034
PITCH_SCALE="${VOICEVOX_PITCH:-0}"
# shellcheck disable=SC2034
INTONATION_SCALE="${VOICEVOX_INTONATION:-1.0}"
# shellcheck disable=SC2034
VOLUME_SCALE="${VOICEVOX_VOLUME:-1.0}"
# shellcheck disable=SC2034
PAUSE_LENGTH_SCALE="${VOICEVOX_PAUSE_SCALE:-1.0}"

# ===== sanitize + chunk split =====

CHUNKED=$(printf '%s' "${TEXT}" \
  | "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/sanitize.py" \
  | "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/chunk_split.py" --speaker "${SPEAKER}" \
  || true)

if [ -z "${CHUNKED}" ]; then
  log_info "say empty after sanitize text_chars=${#TEXT}"
  exit 0
fi

# 配列に読み込む(Bash 3.2 互換のため while + read のみ使用、bash 4+ 専用の
# 配列読込 builtin は doc/08-bash-rules.md §1 により禁止)
CHUNKS=()
while IFS= read -r line; do
  if [ -n "${line}" ]; then
    CHUNKS+=("${line}")
  fi
done <<< "${CHUNKED}"

CHUNK_TOTAL=${#CHUNKS[@]}
if [ "${CHUNK_TOTAL}" -eq 0 ]; then
  log_info "say no chunks after split text_chars=${#TEXT}"
  exit 0
fi

# ===== Session token + 旧 playback 停止 =====

SESSION_FILE="${STATE_DIR}/session.id"
PID_FILE="${STATE_DIR}/playing.pid"

# 旧 playback を kill(idempotent / 不在でも安全)。これにより同時に
# 動いている古い vvread say の wait が解け、次の session check で exit 0 する
vvread_kill_play "${PID_FILE}"

# 新しい session token を発行
SESSION_ID="$(_now_ms)_$$"
echo "${SESSION_ID}" > "${SESSION_FILE}"

is_current_session() {
  local current
  current=$(cat "${SESSION_FILE}" 2>/dev/null || echo "")
  [ "${current}" = "${SESSION_ID}" ]
}

# 各 chunk の wav を入れる prefix
WAV_PREFIX="${STATE_DIR}/voice_${SESSION_ID}"

# 終了時(正常 / 失敗 / preempted いずれも)に自プロセスの wav を全部消す
trap '
  rm -f "'"${WAV_PREFIX}"'"_* 2>/dev/null
' EXIT

log_info "say start chunks=${CHUNK_TOTAL} text_chars=${#TEXT} speaker=${SPEAKER} session=${SESSION_ID}"

# ===== 内部 helper(synth と play を分離、R-005 スコープ通り) =====

# 1 chunk を合成する。失敗時 1 を返す。caller が exit 判断。
_say_synth_chunk() {
  local idx="$1" text="$2" wav="$3"
  voicevox_synthesize "${wav}" "${text}" "${SPEAKER}" "$((idx + 1))/${CHUNK_TOTAL}"
}

# 1 chunk を再生する(同期、再生完了まで wait)。
# 戻り値: 0=正常 / 1=player 不在等で再生開始失敗
# preempt 検知(別 say からの kill)による wait 非ゼロは「中断」として silent
# に return 0 する(post-play の is_current_session で exit 判断するため)。
_say_play_chunk() {
  local idx="$1" wav="$2"
  local play_rc=0
  vvread_play_async "${wav}" "${PID_FILE}" || play_rc=$?
  case "${play_rc}" in
    0) ;;
    1)
      printf 'vvread say: no audio player available.\n' >&2
      if [ "$(uname -s)" != "Darwin" ]; then
        printf 'Hint: install paplay/aplay/play/ffplay or set VVREAD_PLAYER.\n' >&2
      fi
      return 1
      ;;
    2)
      printf 'vvread say: missing wav for chunk %s\n' "$((idx + 1))" >&2
      return 1
      ;;
    *)
      printf 'vvread say: player launch error (rc=%s)\n' "${play_rc}" >&2
      return 1
      ;;
  esac

  local pid
  pid=$(cat "${PID_FILE}" 2>/dev/null || echo "")
  if [ -z "${pid}" ]; then
    printf 'vvread say: failed to start player (no pid recorded)\n' >&2
    return 1
  fi

  # 同期再生。preempt 時の kill で wait は非ゼロ復帰するが、それ自体は異常では
  # なく next chunk の session check で exit 0 する経路に乗る。
  wait "${pid}" 2>/dev/null || true

  # T-013: PID_FILE が自分の PID のままなら消す。Say-B に上書き済みなら触らない
  # (旧 race: 自分が rm すると後続 Say-C が vvread_kill_play の早期 return で
  # B を kill できず preempt 失敗していた)。
  # 残 race: A の player が自然終了 → A の cat 直後に B が kill+rm+write を完遂
  # → A の rm で B の file まで消す、という極小窓は残る(v0.1 許容、必要なら flock 化)
  local current_pid
  current_pid=$(cat "${PID_FILE}" 2>/dev/null || echo "")
  if [ "${current_pid}" = "${pid}" ]; then
    rm -f "${PID_FILE}"
  fi
  return 0
}

# ===== orchestration loop =====

i=0
while [ "${i}" -lt "${CHUNK_TOTAL}" ]; do
  if ! is_current_session; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=pre_synth"
    exit 0
  fi

  WAV="${WAV_PREFIX}_${i}.wav"

  if ! _say_synth_chunk "${i}" "${CHUNKS[${i}]}" "${WAV}"; then
    log_info "say synth_failed chunk=$((i + 1))/${CHUNK_TOTAL}"
    printf 'vvread say: synthesis failed for chunk %s\n' "$((i + 1))" >&2
    exit 1
  fi

  # synth と play の境界。新しい say が来ていれば再生せず終了
  if ! is_current_session; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=pre_play"
    rm -f "${WAV}"
    exit 0
  fi

  log_info "say play chunk=$((i + 1))/${CHUNK_TOTAL}"

  if ! _say_play_chunk "${i}" "${WAV}"; then
    exit 1
  fi

  rm -f "${WAV}"

  # 再生完了直後。preempt されていれば残り chunk をスキップ
  if ! is_current_session; then
    log_info "say superseded chunk=$((i + 1))/${CHUNK_TOTAL} phase=post_play"
    exit 0
  fi

  i=$((i + 1))
done

log_info "say done chunks=${CHUNK_TOTAL} session=${SESSION_ID}"
exit 0
