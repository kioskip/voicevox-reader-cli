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
#   - 以下のローカル/環境変数が解決済み(defaulting は呼び出し側の責務)
#       ENGINE              VOICEVOX Engine の base URL
#       SPEED_SCALE         speedScale
#       PITCH_SCALE         pitchScale
#       INTONATION_SCALE    intonationScale
#       VOLUME_SCALE        volumeScale
#       PAUSE_LENGTH_SCALE  pauseLengthScale
#       PRE_PHONEME         prePhonemeLength
#       POST_PHONEME        postPhonemeLength
#
# defaulting を lib 側に持たせると呼び出し側 (speak.sh) との二重管理になり、
# キャッシュキー計算と合成パラメータが drift する事故が起きる。よって本 lib は
# 値の解決責任を持たず、bash の dynamic scoping で呼び出し元のローカル変数を読む。

voicevox_synthesize() {
  local wav="$1" text="$2" speaker="$3" chunk_label="${4:-?}"
  local query_file="${wav}.query.json"
  local tuned_file="${query_file}.tuned"
  local encoded phase_ms rc=0

  encoded=$(python3 -c "import urllib.parse, sys; print(urllib.parse.quote(sys.argv[1]))" "${text}") || {
    rm -f "${query_file}" "${tuned_file}"
    return 1
  }

  phase_ms=$(_now_ms)
  if ! curl -fsS -X POST \
    "${ENGINE}/audio_query?speaker=${speaker}&text=${encoded}" \
    -o "${query_file}"; then
    rc=1
  else
    log_debug "audio_query chunk=${chunk_label} elapsed_ms=$(( $(_now_ms) - phase_ms ))"
  fi

  if [ $rc -eq 0 ]; then
    if ! jq \
      --argjson speed "${SPEED_SCALE}" \
      --argjson pre "${PRE_PHONEME}" \
      --argjson post "${POST_PHONEME}" \
      --argjson pitch "${PITCH_SCALE}" \
      --argjson intonation "${INTONATION_SCALE}" \
      --argjson volume "${VOLUME_SCALE}" \
      --argjson pause "${PAUSE_LENGTH_SCALE}" \
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
    if ! curl -fsS -X POST \
      -H "Content-Type: application/json" \
      -d @"${query_file}" \
      "${ENGINE}/synthesis?speaker=${speaker}" \
      --output "${wav}"; then
      rc=1
    else
      log_debug "synthesis chunk=${chunk_label} elapsed_ms=$(( $(_now_ms) - phase_ms ))"
    fi
  fi

  rm -f "${query_file}" "${tuned_file}"
  return $rc
}
