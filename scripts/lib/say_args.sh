#!/bin/bash
# lib/say_args.sh - vvread say CLI entrypoint 専用の usage + 引数パース (R-103)
#
# !! CLI entrypoint 専用 !!
# このファイルは cmd/say.sh から source されることを前提としており、
# 汎用再利用 lib ではない。vvread_say_usage / vvread_say_parse_args は
# 同一プロセス source 前提で exit 1 を直接呼ぶ（return ではない）。
#
# source して使う。set は呼ばない（caller の strict mode を尊重）。
# lib 依存なし。
#
# 提供する関数:
#   vvread_say_usage
#     stderr に usage を表示し exit 1 する。
#     -h|--help の exit code は既存挙動（exit 1）に合わせる。
#   vvread_say_parse_args "$@"
#     グローバル変数 TEXT, SPEAKER_OVERRIDE を設定して返す。
#     バリデーション失敗時は exit 1（source = 同一プロセスなので caller を終了）。

vvread_say_usage() {
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

vvread_say_parse_args() {
  TEXT=""
  # shellcheck disable=SC2034
  SPEAKER_OVERRIDE=""

  while [ $# -gt 0 ]; do
    case "$1" in
      --speaker)
        if [ $# -lt 2 ]; then
          printf 'vvread say: --speaker requires an argument\n' >&2
          exit 1
        fi
        # shellcheck disable=SC2034
        SPEAKER_OVERRIDE="$2"
        shift 2
        ;;
      --speaker=*)
        # shellcheck disable=SC2034
        SPEAKER_OVERRIDE="${1#--speaker=}"
        shift
        ;;
      -h|--help)
        vvread_say_usage
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
    vvread_say_usage
  fi
}
