#!/bin/bash
# lib/say_pipeline.sh - vvread say の synth/play チャンクヘルパー (R-103)
#
# source して使う。set は呼ばない（caller の strict mode を尊重）。
#
# 前提（caller で source 済みであること）:
#   - lib/log.sh      (log_debug)
#   - lib/voicevox.sh (voicevox_synthesize)
#   - lib/playback.sh (vvread_play_async)
#
# 提供する関数:
#   vvread_say_synth_chunk <idx> <text> <wav> <speaker> <chunk_total>
#     1 chunk を合成する。失敗時 1 を返す。caller が exit 判断。
#   vvread_say_play_chunk <idx> <wav> <pid_file> <chunk_total>
#     1 chunk を再生する（同期）。0=正常 / 1=再生開始失敗。
#     preempt 検知による wait 非ゼロは silent に return 0
#     （post-play の is_current_session で exit 判断するため）。

# lib/os.sh を明示 source する。
# caller（say.sh）が lib/playback.sh 経由で既に source 済みの場合、二重 source になるが
# os.sh は `_vvread_is_macos()` の関数定義のみ（top-level 実行コードなし）で
# 副作用がないため許容する。
# shellcheck source=./os.sh
source "$(dirname "${BASH_SOURCE[0]}")/os.sh"

vvread_say_synth_chunk() {
  local idx="$1" text="$2" wav="$3" speaker="$4" chunk_total="$5" engine_url="${6:-}"

  local cache_key=""
  if [ -n "${CACHE_DIR:-}" ]; then
    if [ "${idx}" -eq 0 ] && [ "${VVREAD_CACHE_FIRST_CHUNK_RAW:-true}" = "true" ]; then
      local _max="${VVREAD_CACHE_FIRST_CHUNK_RAW_MAX_CHARS:-100}"
      cache_key=$( printf '%s' "${text}" | \
        "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/cache_key.py" --speaker "${speaker}" \
          --cache-raw --cache-raw-max-chars "${_max}" \
        2>/dev/null || true )
    else
      cache_key=$( printf '%s' "${text}" | \
        "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/cache_key.py" --speaker "${speaker}" \
        2>/dev/null || true )
    fi
  fi

  if [ -n "${cache_key}" ]; then
    local cache_wav="${CACHE_DIR}/${cache_key}.wav"
    if [ -f "${cache_wav}" ]; then
      if cp "${cache_wav}" "${wav}" 2>/dev/null; then
        log_debug "say cache_hit chunk=$((idx + 1))/${chunk_total} key=${cache_key}"
        return 0
      fi
      log_debug "say cache_copy_fail chunk=$((idx + 1))/${chunk_total} key=${cache_key} fallback to synth"
    fi
  fi

  # 合成失敗時は即返す。後続 if 文の終了コードに上書きされないよう明示的に伝播
  voicevox_synthesize "${wav}" "${text}" "${speaker}" "$((idx + 1))/${chunk_total}" "${engine_url}" || return $?

  if [ -n "${cache_key}" ] && [ -f "${wav}" ]; then
    local tmp_wav="${CACHE_DIR}/${cache_key}.${$}.tmp"
    if cp "${wav}" "${tmp_wav}" 2>/dev/null; then
      mv "${tmp_wav}" "${CACHE_DIR}/${cache_key}.wav" 2>/dev/null || rm -f "${tmp_wav}" 2>/dev/null || true
      log_debug "say cache_write chunk=$((idx + 1))/${chunk_total} key=${cache_key}"
    fi
  fi
}

# 非同期 synth を起動し SYNTH_PIDS[$idx] に PID を保存する。
# command substitution を使わず現在 shell で直接 & 起動する（subshell 即時復帰保証のため）。
# 呼び出し元の SYNTH_PIDS 配列を直接変更する。
vvread_say_launch_synth_bg() {
  local idx="$1" text="$2" wav="$3" speaker="$4" chunk_total="$5" engine_url="${6:-}"

  vvread_say_synth_chunk \
    "${idx}" "${text}" "${wav}" "${speaker}" "${chunk_total}" "${engine_url}" &

  # shellcheck disable=SC2034
  SYNTH_PIDS[$idx]="$!"
}

# 1 chunk を再生する（同期、再生完了まで wait）。
# 戻り値: 0=正常 / 1=player 不在等で再生開始失敗
# preempt 検知（別 say からの kill）による wait 非ゼロは「中断」として silent に
# return 0 する（post-play の is_current_session で exit 判断するため）。
vvread_say_play_chunk() {
  local idx="$1" wav="$2" pid_file="$3" chunk_total="$4"
  local play_rc=0
  vvread_play_async "${wav}" "${pid_file}" || play_rc=$?
  case "${play_rc}" in
    0) ;;
    1)
      printf 'vvread say: no audio player available.\n' >&2
      if ! _vvread_is_macos; then
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
  pid=$(cat "${pid_file}" 2>/dev/null || echo "")
  if [ -z "${pid}" ]; then
    printf 'vvread say: failed to start player (no pid recorded)\n' >&2
    return 1
  fi

  # 同期再生。preempt 時の kill で wait は非ゼロ復帰するが異常ではない。
  # 前提: vvread_play_async が同一シェルの子プロセスとして player を起動すること。
  #       サブシェル越しに起動された PID を wait した場合、ゾンビ化や wait 失敗の
  #       リスクがあるが、既存挙動の移植であり今回スコープ外。
  # T-014: wait_rc をデバッグログに残す（preempt vs 異常終了の区別用）
  wait "${pid}" 2>/dev/null
  local wait_rc=$?
  if [ "${wait_rc}" -ne 0 ]; then
    log_debug "say player_wait_nonzero chunk=$((idx + 1))/${chunk_total} wait_rc=${wait_rc}"
  fi

  # T-013: PID_FILE が自分の PID のままなら消す。Say-B に上書き済みなら触らない
  local current_pid
  current_pid=$(cat "${pid_file}" 2>/dev/null || echo "")
  if [ "${current_pid}" = "${pid}" ]; then
    rm -f "${pid_file}"
  fi
  return 0
}
