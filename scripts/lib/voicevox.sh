#!/bin/bash
# lib_voicevox.sh - VOICEVOX Engine HTTP API ラッパー
#
# 提供関数:
#   voicevox_synthesize <wav_path> <text> <speaker> [<chunk_label>]
#     audio_query → jq でチューニング値注入 → synthesis の 3 ステップで wav を生成。
#     0 = 成功(wav 出力済み)、1 = curl/jq のいずれかが失敗。
#     <chunk_label> はログ用("1/3" 等)。省略時は "?"。
#
# 呼び出し前提(同じ shell 内に存在していること):
#   - lib_log.sh が source 済み(_now_ms / log_debug を使う)
#
# VOICEVOX_* 環境変数を関数内で直解決する。settings.py が canonical
# (env > project > user > default)、ここは settings.py 失敗時の safety net。
# 旧設計では呼び出し側 local 変数(ENGINE / SPEED_SCALE 等)に依存する
# dynamic scoping を採用していたが、S-006/S-007 で廃止し一元化した。

voicevox_synthesize() {
  local wav="$1" text="$2" speaker="$3" chunk_label="${4:-?}" engine_url_arg="${5:-}"
  local query_file="${wav}.query.json"
  local tuned_file="${query_file}.tuned"
  local encoded phase_ms rc=0
  # S-009: engine 応答停止時の永久ブロック防止
  local _vox_timeout="${VOICEVOX_TIMEOUT:-30}"
  # S-006/S-007: VOICEVOX_* を直解決(settings.py が canonical、ここは fallback)
  # ENGINE: 第5引数 > VOICEVOX_ENGINE_URL > VOICEVOX_ENGINE(legacy, S-008) > default
  local _engine="${engine_url_arg:-${VOICEVOX_ENGINE_URL:-${VOICEVOX_ENGINE:-http://127.0.0.1:50021}}}"
  _engine="${_engine%/}"
  local _speed="${VOICEVOX_SPEED:-1.5}"
  local _pre="${VOICEVOX_PRE_PHONEME:-0}"
  local _post="${VOICEVOX_POST_PHONEME:-0}"
  local _pitch="${VOICEVOX_PITCH:-0}"
  local _intonation="${VOICEVOX_INTONATION:-1.0}"
  local _volume="${VOICEVOX_VOLUME:-1.0}"
  local _pause="${VOICEVOX_PAUSE_SCALE:-1.0}"

  encoded=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "${text}") || {
    rm -f "${query_file}" "${tuned_file}"
    return 1
  }

  phase_ms=$(_now_ms)
  if ! curl -fsS -m "${_vox_timeout}" -X POST \
    "${_engine}/audio_query?speaker=${speaker}&text=${encoded}" \
    -o "${query_file}"; then
    rc=1
  else
    log_debug "audio_query chunk=${chunk_label} elapsed_ms=$(( $(_now_ms) - phase_ms ))"
  fi

  if [ $rc -eq 0 ]; then
    if ! jq \
      --argjson speed "${_speed}" \
      --argjson pre "${_pre}" \
      --argjson post "${_post}" \
      --argjson pitch "${_pitch}" \
      --argjson intonation "${_intonation}" \
      --argjson volume "${_volume}" \
      --argjson pause "${_pause}" \
      '.speedScale = $speed
       | .prePhonemeLength = $pre
       | .postPhonemeLength = $post
       | .pitchScale = $pitch
       | .intonationScale = $intonation
       | .volumeScale = $volume
       | .pauseLengthScale = $pause' \
      "${query_file}" > "${tuned_file}"; then
      rc=1
    else
      mv "${tuned_file}" "${query_file}"
    fi
  fi

  if [ $rc -eq 0 ]; then
    phase_ms=$(_now_ms)
    if ! curl -fsS -m "${_vox_timeout}" -X POST \
      -H "Content-Type: application/json" \
      -d @"${query_file}" \
      "${_engine}/synthesis?speaker=${speaker}" \
      --output "${wav}"; then
      rc=1
    else
      log_debug "synthesis chunk=${chunk_label} elapsed_ms=$(( $(_now_ms) - phase_ms ))"
    fi
  fi

  rm -f "${query_file}" "${tuned_file}"
  return $rc
}

# SPEAKER 解決ヘルパー。--speaker フラグ値 or VOICEVOX_SPEAKER env or 3。
# 引数: [<override>]  空 or 未指定なら env/default に fallback。
# stdout: 決定した speaker ID
# 注意: speaker ID の形式/存在確認は行わない。VOICEVOX Engine への検証は呼び出し元責務。
voicevox_resolve_speaker() {
  local override="${1:-}"
  printf '%s\n' "${override:-${VOICEVOX_SPEAKER:-3}}"
}
