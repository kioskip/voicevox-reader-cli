#!/bin/bash
# scripts/cmd_on_stop.sh - vvread on-stop subcommand (R-006)
#
# Claude Code の Stop hook から呼ばれるエントリポイント。stdin で hook の JSON
# ペイロードを受け取り、transcript の最後の assistant メッセージを抽出して
# `vvread say` で発話する。
#
# 設計方針:
#   - 状態判定(disabled / mute_until)は本コマンドの責務(hook 文脈と
#     `vvread say` 直接実行の責務分離。手動 say は常時動くべき)
#   - 旧 tmp/ → R-001 path 体系の移行も hook 起動時に呼ぶ(idempotent / R-003)
#   - VOICEVOX Engine の health check + notify_error も維持(従来挙動)
#   - stdin 読込 + transcript 抽出は parse_transcript.py に委譲(S-002 統合)。
#     上限 2MB / read timeout 10s / 空入力安全終了は Python 側で実装
#   - 発話は `cmd_say.sh <text>` を subprocess 起動(exec ではなく)。理由:
#     LOG_NAME 境界が綺麗(on_stop ログと say ログが分かれる)
#   - 失敗してもサイレントに通過(hook を fail させない方針、現行 on_stop と同一)
#
# entry script (R-026): set -euo pipefail / Bash 3.2 互換 / shellcheck warning ゼロ。

set -euo pipefail

VVREAD_PROJECT_DIR="${VVREAD_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
VVREAD_SCRIPTS_DIR="${VVREAD_SCRIPTS_DIR:-${VVREAD_PROJECT_DIR}/scripts}"

# OS 別 path 解決 + 旧 tmp/ 移行 (R-001 / R-003)
# shellcheck source=../lib/paths.sh
source "${VVREAD_SCRIPTS_DIR}/lib/paths.sh"
STATE_DIR="$(vvread_state_dir)"
LOG_DIR="$(vvread_log_dir)"
CACHE_DIR="$(vvread_cache_dir)"
mkdir -p "${STATE_DIR}" "${LOG_DIR}" "${CACHE_DIR}"
vvread_migrate_legacy_tmp "${VVREAD_PROJECT_DIR}/tmp"

# venv の python を優先(parse_transcript.py 用)
PYTHON="${VVREAD_PROJECT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON}" ]; then
  PYTHON="python3"
fi

# ===== usage =====

usage() {
  cat >&2 <<'EOF'
Usage: vvread on-stop

  Claude Code の Stop hook 用エントリ。stdin で hook JSON ペイロードを受け取り、
  transcript の最後の assistant メッセージを発話する。

  本コマンドは手動起動を想定していない(hook 専用)。テストや動作確認では
  `echo '{"transcript_path": "..."}' | vvread on-stop` で疎通確認可能。

設定可能な環境変数:
  VOICEVOX_ENGINE_URL    VOICEVOX Engine ベース URL (default http://localhost:50021)
  VOICEVOX_ENGINE        VOICEVOX_ENGINE_URL の旧エイリアス（後方互換）
  VOICEVOX_SPEAKER       話者 ID (cmd_say に渡される)
  VOICEVOX_SPEED など    発話パラメータ(cmd_say と同じ)
  VVREAD_ON_STOP_TIMEOUT stdin 読込 timeout 秒 (default 10)
  VVREAD_ON_STOP_MAX_BYTES stdin 読込上限バイト (default 2097152 = 2MB)
EOF
  exit 1
}

# 引数を取らない設計だが、--help 等は許容する
case "${1:-}" in
  -h|--help)
    usage
    ;;
  "")
    : # 通常経路
    ;;
  *)
    printf 'vvread on-stop: unknown argument: %s\n' "$1" >&2
    usage
    ;;
esac

# ===== state checks =====

# 永続オフ
if [ -f "${STATE_DIR}/disabled" ]; then
  exit 0
fi

# 時限ミュート(現在時刻 < mute_until ならスキップ。期限切れなら削除)
if [ -f "${STATE_DIR}/mute_until" ]; then
  MUTE_UNTIL=$(cat "${STATE_DIR}/mute_until" 2>/dev/null || echo "0")
  NOW=$(date +%s)
  if [ "${NOW}" -lt "${MUTE_UNTIL}" ]; then
    exit 0
  else
    rm -f "${STATE_DIR}/mute_until"
  fi
fi

# ===== logger + notifier =====

# shellcheck disable=SC2034
LOG_NAME="on_stop"
# shellcheck source=../lib/log.sh
source "${VVREAD_SCRIPTS_DIR}/lib/log.sh"
# shellcheck source=../lib/notify.sh
source "${VVREAD_SCRIPTS_DIR}/lib/notify.sh"

# ===== VOICEVOX Engine health check =====

# S-008: VOICEVOX_ENGINE_URL (settings.py 解決) → VOICEVOX_ENGINE (legacy) の順で参照
_engine_base="${VOICEVOX_ENGINE_URL:-${VOICEVOX_ENGINE:-http://localhost:50021}}"
_engine_base="${_engine_base%/}"
ENGINE_URL="${_engine_base}/version"
if ! curl -sf -m 1 "${ENGINE_URL}" >/dev/null 2>&1; then
  log_info "engine unreachable url=${ENGINE_URL}"
  notify_error "vvread" "VOICEVOX Engine に接続できません (${ENGINE_URL})"
  exit 0
fi

# ===== stdin → transcript_path → 最終 assistant text =====

ON_STOP_TIMEOUT="${VVREAD_ON_STOP_TIMEOUT:-10}"
ON_STOP_MAX_BYTES="${VVREAD_ON_STOP_MAX_BYTES:-2097152}"

# parse_transcript.py の stderr は warning ログ用なので拾う(空入力 / 不正 JSON
# / oversize / timeout などを debug ログに残す)
PARSE_STDERR_FILE="$(mktemp -t vvread_on_stop.XXXXXX)"
trap 'rm -f "${PARSE_STDERR_FILE}" 2>/dev/null' EXIT

LAST_TEXT=$(
  "${PYTHON}" "${VVREAD_SCRIPTS_DIR}/parse_transcript.py" \
    --max-bytes "${ON_STOP_MAX_BYTES}" \
    --timeout "${ON_STOP_TIMEOUT}" \
    2>"${PARSE_STDERR_FILE}" || true
)

if [ -s "${PARSE_STDERR_FILE}" ]; then
  # parse_transcript の warning を on_stop ログに転記。複数行を 1 行にまとめる
  parse_warn=$(tr '\n' ' ' < "${PARSE_STDERR_FILE}" | sed 's/[[:space:]]*$//')
  log_info "parse_transcript_warning ${parse_warn}"
fi

if [ -z "${LAST_TEXT}" ]; then
  # transcript が無い / 空 / エラーで取れなかった → silent exit 0
  exit 0
fi

# ===== 発話 =====

# cmd_say.sh は内部で sanitize / chunk_split を行うため、生のテキストを渡す。
# subprocess 起動(exec ではない)で LOG_NAME=on_stop と LOG_NAME=say の境界を
# 維持。失敗してもサイレントに通過(hook を fail させない)。
log_info "dispatch_say chars=${#LAST_TEXT}"
# source=hook タグを付与（queue モード時の二重発火ポリシーで全文 = canonical 扱い）。
# marker 更新・eviction・stale 判定は lib/queue.sh::vvread_queue_submit に集約。
VVREAD_SAY_SOURCE=hook "${VVREAD_SCRIPTS_DIR}/cmd/say.sh" "${LAST_TEXT}" >/dev/null 2>&1 || true

exit 0
