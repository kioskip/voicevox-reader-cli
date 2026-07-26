#!/bin/bash
# scripts/lib/cache_cleanup.sh — キャッシュ TTL 自動削除 (T-013)
#
# 依存:
#   STATE_DIR / CACHE_DIR — paths.sh で解決済みであること
#   VVREAD_CACHE_TTL_DAYS / VVREAD_CACHE_CLEANUP_INTERVAL_HOURS — settings.py env で設定
#   log_info / log_warn — log.sh が source 済みであること

# 非負整数チェック。空文字・非数値なら 1 を返す。
_vvread_cache_is_valid_uint() {
  case "${1:-}" in
    '' | *[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# TTL 期限切れの wav を削除する。
# $1: ttl_days（検証済み正整数）
_vvread_cache_cleanup() {
  local ttl_days="$1"
  local ttl_mins find_age count f

  # L-4: 共有ホストで他ユーザーに読まれないよう umask 077 で新規作成する
  ( umask 077; mkdir -p "${STATE_DIR}" ) 2>/dev/null || return 1
  [ -d "${CACHE_DIR}" ] || return 1

  ttl_mins=$(( ttl_days * 24 * 60 ))
  find_age=$(( ttl_mins - 1 ))

  count=0
  while IFS= read -r -d '' f; do
    if rm -f "$f" 2>/dev/null; then
      count=$(( count + 1 ))
    fi
  done < <(find "${CACHE_DIR}" -type f -name "*.wav" -mmin "+${find_age}" -print0 2>/dev/null)
  log_info "cache_cleanup ttl_days=${ttl_days} deleted=${count}"
}

# インターバルと排他ロックを確認し、期限が来ていればバックグラウンドで削除を実行する。
# VVREAD_CACHE_TTL_DAYS=0（デフォルト）のときは何もしない。
_vvread_cache_cleanup_if_due() {
  local ttl_days="${VVREAD_CACHE_TTL_DAYS:-0}"
  local interval_hours="${VVREAD_CACHE_CLEANUP_INTERVAL_HOURS:-24}"

  # 数値バリデーション
  if ! _vvread_cache_is_valid_uint "${ttl_days}"; then
    log_warn "cache_cleanup: invalid VVREAD_CACHE_TTL_DAYS='${ttl_days}', skipping"
    return 0
  fi
  if ! _vvread_cache_is_valid_uint "${interval_hours}"; then
    log_warn "cache_cleanup: invalid VVREAD_CACHE_CLEANUP_INTERVAL_HOURS='${interval_hours}', skipping"
    return 0
  fi

  # TTL=0 は無効
  [ "${ttl_days}" -gt 0 ] || return 0

  local interval_mins
  interval_mins=$(( interval_hours * 60 ))
  local last_file="${STATE_DIR}/cache_cleanup_last"
  local lock_dir="${STATE_DIR}/cache_cleanup.lock"

  # INTERVAL_HOURS > 0 のとき、最終実行から interval_mins 分以内なら skip
  # INTERVAL_HOURS=0 のときは毎回実行（interval_mins=0 → -mmin -0 条件は常に偽）
  if [ "${interval_mins}" -gt 0 ] && [ -f "${last_file}" ] && \
     find "${last_file}" -mmin "-${interval_mins}" 2>/dev/null | grep -q .; then
    return 0
  fi

  # 排他ロック（mkdir はアトミック）
  if ! mkdir "${lock_dir}" 2>/dev/null; then
    # stale ロック判定: 保持 PID が死んでいれば除去して再取得（U-119）
    local holder_pid
    holder_pid=$(cat "${lock_dir}/pid" 2>/dev/null || echo "")
    case "${holder_pid}" in
      "" | *[!0-9]*)
        # PID 不明 → stale とみなす
        ;;
      *)
        if [ "${holder_pid}" -ne 0 ] 2>/dev/null && kill -0 "${holder_pid}" 2>/dev/null; then
          return 0  # 生存中 = 別プロセスが cleanup 実行中
        fi
        ;;
    esac
    rm -rf "${lock_dir}" 2>/dev/null || true
    if ! mkdir "${lock_dir}" 2>/dev/null; then
      return 0  # 再取得レースに負けた → yield
    fi
  fi

  # バックグラウンド実行: cleanup 成功後のみ last-run marker を更新
  (
    if _vvread_cache_cleanup "${ttl_days}"; then
      touch "${last_file}" 2>/dev/null || true
    fi
    rm -rf "${lock_dir}" 2>/dev/null || true
  ) &
  # バックグラウンドワーカーの PID を記録（$! は & 直後のサブシェル PID）
  # $$ は say.sh 自身のため & 後すぐ dead になり stale 誤判定を招く
  printf '%s\n' "$!" > "${lock_dir}/pid" 2>/dev/null || true
}
