#!/bin/bash
# lib_log.sh - voiceClaude 共通ロガー
#
# source して使う。呼び出し側で LOG_DIR を定義しておくこと
# (lib_paths.sh の vvread_log_dir で取得した値)。
# LOG_NAME(default "speak")でログタグを切り替え可能(例: voice / speak)。
#
# 環境変数:
#   VOICEVOX_LOG_LEVEL      OFF / INFO / DEBUG (default INFO)
#   VOICEVOX_LOG_FILE       出力先パス (default ${LOG_DIR}/speak.log)
#   VOICEVOX_LOG_MAX_BYTES  ログサイズ上限 (default 10485760 = 10 MiB)。
#                           source 時に超過判定し、超えていれば LOG_FILE を
#                           LOG_FILE.1 にローテーション(上書き)する。
#                           on_stop / speak / voice はすべて短命プロセスなので
#                           「source 時に 1 回 stat」で過剰肥大を抑える方針。
#                           履歴は .1 の 1 世代のみ保持(回したら捨てる)。

LOG_NAME="${LOG_NAME:-speak}"
LOG_LEVEL="${VOICEVOX_LOG_LEVEL:-INFO}"
LOG_FILE="${VOICEVOX_LOG_FILE:-${LOG_DIR}/speak.log}"
LOG_MAX_BYTES="${VOICEVOX_LOG_MAX_BYTES:-10485760}"

case "${LOG_LEVEL}" in
  DEBUG|debug) LOG_LEVEL_NUM=2 ;;
  INFO|info)   LOG_LEVEL_NUM=1 ;;
  OFF|off)     LOG_LEVEL_NUM=0 ;;
  *)           LOG_LEVEL_NUM=1 ;;
esac

# 超過していれば 1 世代だけ退避する。macOS には flock が無いので厳密な排他は
# 諦め、複数プロセスが同時に rotate しに来たら mv が冪等に上書きで終わる前提。
# 最悪「1 ミリ秒前のログが .1 に二重書きされる」程度で、ログの整合性は失わない。
_log_rotate_if_needed() {
  [ -f "${LOG_FILE}" ] || return 0
  [ "${LOG_MAX_BYTES}" -gt 0 ] 2>/dev/null || return 0
  # ファイルサイズ取得は wc -c でクロスプラットフォーム対応 (macOS の
  # `stat -f%z` と GNU の `stat -c%s` の差を回避)。
  local size
  size=$(wc -c < "${LOG_FILE}" 2>/dev/null | tr -d ' ' || echo 0)
  [ -n "${size}" ] || size=0
  if [ "${size}" -gt "${LOG_MAX_BYTES}" ]; then
    mv -f "${LOG_FILE}" "${LOG_FILE}.1" 2>/dev/null || true
  fi
}

# ログ出力先のディレクトリは初回 source 時に確実に作る。
# OFF の場合は書き込まないので mkdir も rotate も不要。
if [ "${LOG_LEVEL_NUM}" -gt 0 ]; then
  # L-4: 共有ホストで他ユーザーに読まれないよう umask 077 で新規作成する
  ( umask 077; mkdir -p "$(dirname "${LOG_FILE}")" ) 2>/dev/null || true
  _log_rotate_if_needed
fi

# C1 制御文字(U+0080-U+009F、例: U+009B=CSI)の UTF-8 2-byte 表現
# (\xc2\x80-\xc2\x9f)を除去するためのパターン一覧。source 時に一度だけ構築し、
# _log_write の呼び出しごとの再構築を避ける。
# 単独バイト範囲([$'\x80'-$'\x9f'] 等)でのマッチはロケール依存で不安定な上、
# 日本語等マルチバイト文字の継続バイト(0x80-0xBF は C1 の範囲と重なる)まで
# 削ってしまう。必ず先頭バイト \xc2 との 2-byte ペア全体を literal 列挙して
# のみ除去することで、この事故を避ける(Codex レビュー指摘: C0 のみ除去では
# U+009B 等の C1 制御文字による ANSI 注入がすり抜けていた)。
_LOG_C1_PATTERNS=(
  $'\xc2\x80' $'\xc2\x81' $'\xc2\x82' $'\xc2\x83' $'\xc2\x84' $'\xc2\x85' $'\xc2\x86' $'\xc2\x87'
  $'\xc2\x88' $'\xc2\x89' $'\xc2\x8a' $'\xc2\x8b' $'\xc2\x8c' $'\xc2\x8d' $'\xc2\x8e' $'\xc2\x8f'
  $'\xc2\x90' $'\xc2\x91' $'\xc2\x92' $'\xc2\x93' $'\xc2\x94' $'\xc2\x95' $'\xc2\x96' $'\xc2\x97'
  $'\xc2\x98' $'\xc2\x99' $'\xc2\x9a' $'\xc2\x9b' $'\xc2\x9c' $'\xc2\x9d' $'\xc2\x9e' $'\xc2\x9f'
)

# ms 精度の epoch(macOS の date は %N 非対応なので perl を使う)
_now_ms() {
  perl -MTime::HiRes=time -e 'printf "%d\n", time()*1000' 2>/dev/null \
    || echo $(( $(date +%s) * 1000 ))
}

_log_write() {
  local need="$1"; shift
  local lvl="$1"; shift
  [ "${LOG_LEVEL_NUM}" -ge "${need}" ] || return 0
  local now ms3 ts msg
  now=$(_now_ms)
  ms3=$(printf '%03d' $(( now % 1000 )))
  ts=$(date +"%Y-%m-%d %H:%M:%S")
  # L-3bash (defense in depth): ESC(\x1b) / CR(\x0d) / C1 制御文字を除去して
  # ログ偽装(ANSI エスケープでの表示改ざん・行の上書き)を防ぐ。ここは全 log
  # 呼び出しのホットパスなのでサブプロセス起動(tr 等)は避け、pure bash 置換
  # (builtin のみ、fork なし)で済ませる。C1 は _LOG_C1_PATTERNS 参照。
  # untrusted テキストがそのまま渡る箇所(say.sh の enqueue プレビュー等)は
  # 呼び出し元で Unicode カテゴリベースの除去によりさらに厳密に無害化する。
  msg="$*"
  msg="${msg//$'\x1b'/}"
  msg="${msg//$'\r'/}"
  local _c1
  for _c1 in "${_LOG_C1_PATTERNS[@]}"; do
    msg="${msg//${_c1}/}"
  done
  printf '[%s.%s] %s.%-5s [%d]: %s\n' "${ts}" "${ms3}" "${LOG_NAME}" "${lvl}" "$$" "${msg}" >> "${LOG_FILE}"
}

log_warn()  { _log_write 1 "WARN"  "$@"; }
log_info()  { _log_write 1 "INFO"  "$@"; }
log_debug() { _log_write 2 "DEBUG" "$@"; }
