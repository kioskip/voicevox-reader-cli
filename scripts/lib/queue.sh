#!/bin/bash
# lib/queue.sh - キュー再生モードの中核 (B-015 / B-138)
#
# source して使う。set は呼ばない（caller の strict mode を尊重、
# doc/08-bash-rules.md §2）。全関数 local 徹底・bash 3.2 互換・連想配列なし・
# flock なし（macOS / Git Bash に flock が無いため mkdir ベースの協調機構）。
#
# 前提（caller で source 済みであること）:
#   - lib/log.sh    （_now_ms / log_info / log_warn）
#   - グローバル STATE_DIR が設定済み（vvread_state_dir の結果）
#
# キュー構造（vvread_queue_dirs_init が作成）:
#   ${STATE_DIR}/queue/
#     pending/            … 未再生エントリ
#     playing/            … 再生中エントリ（drainer が pop で移す）
#     failed/             … retry 上限超で退避
#     queue.lock/         … drainer 選出ロック（dir, mkdir 排他）
#     queue.mutate.lock/  … submit/evict/marker/pop の直列化ロック（dir）
#     queue_last_hook_ms  … 最新 hook 全文の生成時刻 marker
#     stop.request        … vvread stop の停止シグナル（drainer token 付き）
#
# エントリ名: ${ms}_${pid}.${nonce}.${speaker}.${source}.r${N}
#   ms=_now_ms / nonce=$RANDOM 連結 / speaker=非負整数 / source∈cli|hook|mcp /
#   r${N}=retry 世代。内容 = 発話テキスト本文（file 600 / dir 700）。
#
# ===== ロック設計（spec からの意図的逸脱・worklog 記録済み）=====
# spec は「tmp dir を atomic rename で公開」と書くが、POSIX では既存ディレクトリ
# への mv はネスト（中に移動）し排他に使えない（macOS/BSD に mv -T 無し）。
# 移植可能な atomic test-and-set は mkdir。よって mkdir 排他 + pid/token ファイル
# 方式を採る。安全契約（単一 drainer 選出 / owner token 一致時のみ release /
# 死亡 owner の reclaim / crash-mid-init でもデッドロックしない）は不変。
#
# ===== リエントランシー規約 =====
# mkdir ロックは再入不可。public 関数（vvread_queue_submit/clear/pop/
# stop_request）は mutation lock を取得する。`_..._locked` 等の内部ヘルパは
# 「ロック保持前提」で、内部から public 関数を呼ばない（self-deadlock 防止）。

# retry 上限（playing → pending を繰り返す上限。超えたら failed へ）
_QUEUE_RETRY_MAX="${VVREAD_QUEUE_RETRY_MAX:-2}"

# owner ファイルの区切り（pid<TAB>token）。bash 3.2 互換のためリテラル TAB を変数化。
_QUEUE_TAB="$(printf '\t')"

# ---------------------------------------------------------------------------
# 小物ヘルパ
# ---------------------------------------------------------------------------

# 非負整数判定
_queue_is_uint() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# VVREAD_QUEUE_MAX / VVREAD_QUEUE_FAILED_MAX を解決。
#   いずれも 1 以上の整数のみ。0 / 負 / 非数値は fallback + WARN（FB-5）。
_queue_resolve_max() {
  local v="${VVREAD_QUEUE_MAX:-64}"
  if _queue_is_uint "${v}" && [ "${v}" -ge 1 ]; then
    _QUEUE_MAX="${v}"
  else
    log_warn "queue: invalid VVREAD_QUEUE_MAX=${v} — fallback 64"
    _QUEUE_MAX=64
  fi
  local fv="${VVREAD_QUEUE_FAILED_MAX:-32}"
  if _queue_is_uint "${fv}" && [ "${fv}" -ge 1 ]; then
    _QUEUE_FAILED_MAX="${fv}"
  else
    log_warn "queue: invalid VVREAD_QUEUE_FAILED_MAX=${fv} — fallback 32"
    _QUEUE_FAILED_MAX=32
  fi
}

# 1 以上の整数のみ許可。不正値は WARN + default を stdout（log_warn は LOG_FILE 行きで
# stdout を汚さない）。
_queue_env_uint() {
  local v="${1:-}" d="${2:-}" name="${3:-}"
  if _queue_is_uint "${v}" && [ "${v}" -ge 1 ]; then
    echo "${v}"
  else
    log_warn "queue: invalid ${name}=${v} — fallback ${d}"
    echo "${d}"
  fi
}

# lock staleness / heartbeat / spin の調整値を 1 度だけ解決する（FB: env 検証）。
#   _QUEUE_INIT_GRACE   owner-absent dir の回収猶予（mid-init winner 保護）
#   _QUEUE_MUTATE_STALE owner 確定済み mutate.lock の staleness 閾値
#   _QUEUE_DRAIN_STALE  queue.lock heartbeat の staleness 閾値（double-play 境界）
#   _QUEUE_HB_INTERVAL  再生中 heartbeat 更新周期
#   _QUEUE_EMPTY_POP_MAX 空 pop spin の打ち切り回数
_queue_resolve_tuning() {
  _QUEUE_INIT_GRACE=$(_queue_env_uint "${VVREAD_QUEUE_INIT_GRACE_S:-2}" 2 VVREAD_QUEUE_INIT_GRACE_S)
  _QUEUE_MUTATE_STALE=$(_queue_env_uint "${VVREAD_QUEUE_MUTATE_STALE_S:-15}" 15 VVREAD_QUEUE_MUTATE_STALE_S)
  _QUEUE_DRAIN_STALE=$(_queue_env_uint "${VVREAD_QUEUE_DRAIN_STALE_S:-150}" 150 VVREAD_QUEUE_DRAIN_STALE_S)
  _QUEUE_HB_INTERVAL=$(_queue_env_uint "${VVREAD_QUEUE_HB_INTERVAL_S:-5}" 5 VVREAD_QUEUE_HB_INTERVAL_S)
  _QUEUE_EMPTY_POP_MAX=$(_queue_env_uint "${VVREAD_QUEUE_EMPTY_POP_MAX:-20}" 20 VVREAD_QUEUE_EMPTY_POP_MAX)
  # DRAIN_STALE <= 2*VOICEVOX_TIMEOUT は健全な長 synth を誤回収（double-play）し得る
  local vt="${VOICEVOX_TIMEOUT:-30}"
  if _queue_is_uint "${vt}" && [ "${_QUEUE_DRAIN_STALE}" -le "$((2 * vt))" ]; then
    log_warn "queue: VVREAD_QUEUE_DRAIN_STALE_S=${_QUEUE_DRAIN_STALE} <= 2*VOICEVOX_TIMEOUT(${vt}) — double-play risk"
  fi
}

# キューディレクトリを初期化し QDIR / _QUEUE_MAX を設定する。
# 他の全関数の前に 1 度呼ぶこと。STATE_DIR 必須。
vvread_queue_dirs_init() {
  if [ -z "${STATE_DIR:-}" ]; then
    log_warn "queue: STATE_DIR unset — cannot init queue dirs"
    return 1
  fi
  QDIR="${STATE_DIR}/queue"
  ( umask 077
    mkdir -p "${QDIR}/pending" "${QDIR}/playing" "${QDIR}/failed" )
  _queue_resolve_max
  _queue_resolve_tuning
  return 0
}

# スキャン対象として安全なエントリか（通常ファイル・非 symlink・非 tmp）
_queue_scan_ok() {
  local p="${1:-}" base
  [ -f "${p}" ] || return 1
  [ -L "${p}" ] && return 1
  base="${p##*/}"
  case "${base}" in
    *.tmp.*) return 1 ;;
  esac
  return 0
}

# ディレクトリ内の有効エントリ件数を stdout
_queue_count() {
  local dir="${1:-}" n=0 f
  [ -d "${dir}" ] || { echo 0; return 0; }
  for f in "${dir}"/*; do
    _queue_scan_ok "${f}" || continue
    n=$((n + 1))
  done
  echo "${n}"
}

# ディレクトリ内の有効エントリを名前昇順（= ms 昇順 = 古い順）で full path 出力
_queue_sorted() {
  local dir="${1:-}"
  [ -d "${dir}" ] || return 0
  find "${dir}" -maxdepth 1 -type f 2>/dev/null | LC_ALL=C sort
}

# ---------------------------------------------------------------------------
# 破壊的操作の path guard
# ---------------------------------------------------------------------------

# 安全な queue entry path か検証（削除許可判定）。
#   許可: ${QDIR}/{pending,playing,failed}/<非空 basename> の通常ファイル（非 symlink）
#   拒否: 空 / `..` 含む / 範囲外 / symlink / queue subdir 自体が symlink
_queue_guard_path() {
  local p="${1:-}" d
  [ -n "${p}" ] || return 1
  [ -n "${QDIR:-}" ] || return 1
  case "${p}" in
    *..*) return 1 ;;
  esac
  case "${p}" in
    "${QDIR}/pending/"?*|"${QDIR}/playing/"?*|"${QDIR}/failed/"?*) ;;
    *) return 1 ;;
  esac
  # queue subdir / ルートが symlink なら拒否（差し替え攻撃防御）
  for d in "${QDIR}" "${QDIR}/pending" "${QDIR}/playing" "${QDIR}/failed"; do
    [ -L "${d}" ] && return 1
  done
  # 削除対象は通常ファイルのみ（symlink・ディレクトリ拒否）
  [ -L "${p}" ] && return 1
  [ -f "${p}" ] || return 1
  return 0
}

# guard を通したうえで rm。guard 失敗時は WARN + return 1（削除しない）。
_queue_safe_rm() {
  local p="${1:-}"
  if _queue_guard_path "${p}"; then
    rm -f "${p}" 2>/dev/null || true
    return 0
  fi
  log_warn "queue: refused unsafe rm path=${p}"
  return 1
}

# ---------------------------------------------------------------------------
# ロック（mkdir 排他 + pid/token）
# ---------------------------------------------------------------------------

# ---- owner モデル（canonical 1 ファイル + legacy fallback）-------------------
#
# 各 lock_dir の所有者は ${lock_dir}/owner（内容 "<pid>\t<token>"）で一意表現する。
# 旧形式（pid / token 別ファイル）は legacy fallback として読めるが新規書込は owner のみ。

# heartbeat / progress / dir mtime（いずれも epoch 秒）
_queue_now_s() { date +%s; }

# lock_dir の lease heartbeat（再生中・進捗時に更新）を atomic 書込
_queue_hb_write() {
  local d="${1:-}"
  printf '%s\n' "$(_queue_now_s)" > "${d}/hb.tmp.$$" 2>/dev/null \
    && mv "${d}/hb.tmp.$$" "${d}/hb" 2>/dev/null || true
}

# lock_dir の実処理 progress（pop/chunk/entry 完了時のみ更新）を atomic 書込
_queue_progress_write() {
  local d="${1:-}"
  printf '%s\n' "$(_queue_now_s)" > "${d}/progress.tmp.$$" 2>/dev/null \
    && mv "${d}/progress.tmp.$$" "${d}/progress" 2>/dev/null || true
}

# present-and-old 判定: ${d}/${name} が存在し age>thr のとき 0、不在/fresh/不正は 1。
_queue_age_stale() {
  local d="${1:-}" name="${2:-}" thr="${3:-}" v now age
  [ -f "${d}/${name}" ] || return 1
  v=$(cat "${d}/${name}" 2>/dev/null || echo "")
  _queue_is_uint "${v}" || return 1
  now=$(_queue_now_s); age=$((now - v)); [ "${age}" -lt 0 ] && age=0
  [ "${age}" -gt "${thr}" ]
}

# dir 自体の mtime age>thr 判定（owner-absent dir の grace 用）。
# macOS/BSD: stat -f %m / GNU: stat -c %Y。取得不能なら 1（=回収しない・安全側）。
_queue_dir_age_stale() {
  local d="${1:-}" thr="${2:-}" m now age
  m=$(stat -f %m "${d}" 2>/dev/null || stat -c %Y "${d}" 2>/dev/null || echo "")
  _queue_is_uint "${m}" || return 1
  now=$(_queue_now_s); age=$((now - m)); [ "${age}" -lt 0 ] && age=0
  [ "${age}" -gt "${thr}" ]
}

# owner を "pid<TAB>token" で stdout（return 0）。owner-absent は return 1。
#   1) ${lock_dir}/owner があれば canonical
#   2) 無く旧 pid/token があれば legacy fallback
#   3) どちらも無ければ owner-absent
_queue_read_owner() {
  local lock_dir="${1:-}" line pid token
  if [ -f "${lock_dir}/owner" ]; then
    line=$(cat "${lock_dir}/owner" 2>/dev/null || echo "")
    pid="${line%%${_QUEUE_TAB}*}"
    token="${line#*${_QUEUE_TAB}}"
    [ -n "${pid}" ] || return 1
    printf '%s%s%s\n' "${pid}" "${_QUEUE_TAB}" "${token}"
    return 0
  fi
  if [ -f "${lock_dir}/pid" ]; then
    pid=$(cat "${lock_dir}/pid" 2>/dev/null || echo "")
    token=$(cat "${lock_dir}/token" 2>/dev/null || echo "")
    [ -n "${pid}" ] || return 1
    printf '%s%s%s\n' "${pid}" "${_QUEUE_TAB}" "${token}"
    return 0
  fi
  return 1
}

# lock_dir を atomic rename で退避し削除（共通回収本体）。
_queue_reclaim_do() {
  local lock_dir="${1:-}" aside="${1:-}.stale.$$.${RANDOM}"
  if mv "${lock_dir}" "${aside}" 2>/dev/null; then
    rm -rf "${aside}" 2>/dev/null || true
  fi
}

# lock_dir を取得。成功で token を stdout に返し return 0、失敗で return 1。
# mkdir が atomic test-and-set。owner は hard-link で no-clobber publish し、
# 既存 owner があれば失敗（aside 後に古い acquire が新 lock の owner を上書きする
# race を防ぐ）。publish 後に stored owner == 自分 を再検証する。
vvread_queue_acquire() {
  local lock_dir="${1:-}"
  mkdir "${lock_dir}" 2>/dev/null || return 1
  local token tmp cur want
  token="$$.${RANDOM}.${RANDOM}"
  tmp="${lock_dir}.owner.tmp.$$.${RANDOM}"
  printf '%s%s%s\n' "$$" "${_QUEUE_TAB}" "${token}" > "${tmp}" 2>/dev/null \
    || { rm -f "${tmp}" 2>/dev/null || true; return 1; }
  # no-clobber: destination 既存なら ln は失敗 → 他者所有なので触らず撤退
  if ! ln "${tmp}" "${lock_dir}/owner" 2>/dev/null; then
    rm -f "${tmp}" 2>/dev/null || true
    return 1
  fi
  rm -f "${tmp}" 2>/dev/null || true
  # 自己再検証: stored owner == 自分（aside→再作成 race の検出）
  cur=$(_queue_read_owner "${lock_dir}" 2>/dev/null || echo "")
  want=$(printf '%s%s%s' "$$" "${_QUEUE_TAB}" "${token}")
  [ "${cur}" = "${want}" ] || return 1
  _queue_hb_write "${lock_dir}"
  printf '%s\n' "${token}"
  return 0
}

# stale ロックの回収。<dir> [allow_self] [stale_s]
#   ① owner-absent → dir mtime age>INIT_GRACE のときのみ回収（mid-init winner 保護）
#   ② owner pid 死亡 → 回収
#   ③ stale_s 指定 && hb age>stale_s:
#        pid==$$ は allow_self=1 のときのみ回収（queue.lock への footgun 回避）
#        他 pid は常に回収
#   それ以外 → 保持
vvread_queue_reclaim_stale() {
  local lock_dir="${1:-}" allow_self="${2:-0}" stale_s="${3:-}"
  [ -d "${lock_dir}" ] || return 0
  local owner pid
  owner=$(_queue_read_owner "${lock_dir}" 2>/dev/null || echo "")
  if [ -z "${owner}" ]; then
    if _queue_dir_age_stale "${lock_dir}" "${_QUEUE_INIT_GRACE:-2}"; then
      _queue_reclaim_do "${lock_dir}"
    fi
    return 0
  fi
  pid="${owner%%${_QUEUE_TAB}*}"
  if ! { _queue_is_uint "${pid}" && kill -0 "${pid}" 2>/dev/null; }; then
    _queue_reclaim_do "${lock_dir}"
    return 0
  fi
  if [ -n "${stale_s}" ] && _queue_age_stale "${lock_dir}" hb "${stale_s}"; then
    if [ "${pid}" = "$$" ]; then
      [ "${allow_self}" = "1" ] && _queue_reclaim_do "${lock_dir}"
    else
      _queue_reclaim_do "${lock_dir}"
    fi
  fi
  return 0
}

# token 一致時のみ release。owner/hb/progress を消してから rmdir（rm -rf しない）。
vvread_queue_release() {
  local lock_dir="${1:-}" token="${2:-}"
  [ -d "${lock_dir}" ] || return 0
  local owner cur
  owner=$(_queue_read_owner "${lock_dir}" 2>/dev/null || echo "")
  cur="${owner#*${_QUEUE_TAB}}"
  if [ -n "${token}" ] && [ -n "${owner}" ] && [ "${cur}" = "${token}" ]; then
    rm -f "${lock_dir}/owner" "${lock_dir}/pid" "${lock_dir}/token" \
          "${lock_dir}/hb" "${lock_dir}/progress" 2>/dev/null || true
    rm -f "${lock_dir}/"*.tmp.* 2>/dev/null || true
    rmdir "${lock_dir}" 2>/dev/null || true
    return 0
  fi
  return 1
}

# queue.lock の所有者か（token 一致）。owner reader 経由。
vvread_queue_owns_lock() {
  local qdir="${1:-}" token="${2:-}" owner cur
  [ -n "${token}" ] || return 1
  owner=$(_queue_read_owner "${qdir}/queue.lock" 2>/dev/null || echo "")
  [ -n "${owner}" ] || return 1
  cur="${owner#*${_QUEUE_TAB}}"
  [ "${cur}" = "${token}" ]
}

# token 一致時のみ queue.lock の hb（lease）/ progress（実処理）を更新。
vvread_queue_drain_heartbeat() {
  vvread_queue_owns_lock "${1:-}" "${2:-}" || return 1
  _queue_hb_write "${1}/queue.lock"
}
vvread_queue_progress() {
  vvread_queue_owns_lock "${1:-}" "${2:-}" || return 1
  _queue_progress_write "${1}/queue.lock"
}

# queue.lock の hb が DRAIN_STALE 超なら回収（他プロセスの生存 stuck drainer 救済。
# allow_self=0 = 自分の queue.lock は決して回収しない）。
vvread_queue_reclaim_drain_stale() {
  vvread_queue_reclaim_stale "${1:-}/queue.lock" 0 "${_QUEUE_DRAIN_STALE:-150}"
}

# lock_dir を stale 回収しつつブロッキング取得。成功で token を stdout。
# <dir> [max_tries] [allow_self] [stale_s]
_queue_lock_acquire_blocking() {
  local lock_dir="${1:-}" max_tries="${2:-100}" allow_self="${3:-0}" stale_s="${4:-}" token="" i=0
  while [ "${i}" -lt "${max_tries}" ]; do
    if token=$(vvread_queue_acquire "${lock_dir}"); then
      printf '%s\n' "${token}"
      return 0
    fi
    vvread_queue_reclaim_stale "${lock_dir}" "${allow_self}" "${stale_s}"
    i=$((i + 1))
    sleep 0.05 2>/dev/null || true
  done
  return 1
}

# mutation lock 取得（token を stdout）/ 解放。
# 自己リーク（pid==$$）も他プロセスの leak も MUTATE_STALE 超で回収（短期ロックなので
# 15s も保持していれば壊れている）。fresh な保持は触らない。
_queue_mutate_lock() {
  _queue_lock_acquire_blocking "${QDIR}/queue.mutate.lock" 100 1 "${_QUEUE_MUTATE_STALE:-15}"
}
_queue_mutate_unlock() {
  vvread_queue_release "${QDIR}/queue.mutate.lock" "${1:-}"
}

# ---------------------------------------------------------------------------
# marker（atomic write）
# ---------------------------------------------------------------------------

_queue_marker_write() {
  local marker="${1:-}" val="${2:-}"
  ( umask 077
    printf '%s\n' "${val}" > "${marker}.tmp.$$" \
      && mv "${marker}.tmp.$$" "${marker}" )
}

# ---------------------------------------------------------------------------
# エントリ名の組み立て / フィールド抽出
# ---------------------------------------------------------------------------

# head 部分（先頭 '.' より前）の '_' 個数を返す。1=旧形式 / 2=新形式（failed）。
_queue_head_uscount() {
  local h="${1%%.*}" n=0
  while [ "${h#*_}" != "${h}" ]; do
    n=$((n + 1)); h="${h#*_}"
  done
  echo "${n}"
}

# 【FB2-2】failed entry の形式判別。new=failed_ms 前置あり / legacy=なし。
_queue_failed_format() {
  if [ "$(_queue_head_uscount "${1:-}")" -ge 2 ]; then
    echo new
  else
    echo legacy
  fi
}

# basename からフィールドを取り出す。
#   field ∈ created_ms | failed_ms | nonce | speaker | source | retry
#   pending/playing: ${created_ms}_${pid}.${nonce}.${speaker}.${source}.r${N}
#   failed(new):     ${failed_ms}_${created_ms}_${pid}.${nonce}.${speaker}.${source}.r${N}
vvread_queue_entry_field() {
  local base="${1:-}" field="${2:-}"
  local head tail nonce speaker source retry uscount tmp
  head="${base%%.*}"     # [failed_ms_]created_ms_pid
  uscount=$(_queue_head_uscount "${base}")
  case "${field}" in
    failed_ms)
      # 新形式のみ failed_ms を持つ。旧形式は空。
      if [ "${uscount}" -ge 2 ]; then printf '%s\n' "${head%%_*}"; else printf '\n'; fi
      return 0 ;;
    created_ms)
      if [ "${uscount}" -ge 2 ]; then
        tmp="${head#*_}"; printf '%s\n' "${tmp%%_*}"   # failed_ms_ を剥がした次が created_ms
      else
        printf '%s\n' "${head%%_*}"
      fi
      return 0 ;;
  esac
  tail="${base#*.}"      # nonce.speaker.source.rN
  nonce="${tail%%.*}"; tail="${tail#*.}"
  speaker="${tail%%.*}"; tail="${tail#*.}"
  source="${tail%%.*}"; tail="${tail#*.}"
  retry="${tail#r}"
  case "${field}" in
    nonce)   printf '%s\n' "${nonce}" ;;
    speaker) printf '%s\n' "${speaker}" ;;
    source)  printf '%s\n' "${source}" ;;
    retry)   printf '%s\n' "${retry}" ;;
    *) return 1 ;;
  esac
  return 0
}

# pending に 1 エントリを書く（mutation lock 保持前提）。
# 既存衝突時は nonce 再生成で再試行（既存内容を上書きしない）。
# ms は lock 取得前に呼び元で捕捉したタイムスタンプ（FIFO 順序保証・F-118）。
_queue_enqueue() {
  local source="${1:-}" speaker="${2:-}" text="${3:-}" retry="${4:-0}" ms="${5:-}"
  [ -n "${ms}" ] || ms=$(_now_ms)
  local nonce entry tmp tries=0
  while [ "${tries}" -lt 20 ]; do
    nonce="${RANDOM}${RANDOM}"
    entry="${ms}_$$.${nonce}.${speaker}.${source}.r${retry}"
    if [ ! -e "${QDIR}/pending/${entry}" ]; then
      break
    fi
    tries=$((tries + 1))
  done
  ( umask 077
    tmp="${QDIR}/pending/${entry}.tmp.$$"
    printf '%s' "${text}" > "${tmp}" \
      && mv "${tmp}" "${QDIR}/pending/${entry}" )
  return 0
}

# ---------------------------------------------------------------------------
# submit（全 enqueue の共通入口・source 別分岐）
# ---------------------------------------------------------------------------

# pending の自動通知（hook/mcp）を evict（cli は残す）。mutation lock 保持前提。
_queue_evict_pending_auto() {
  local f base src
  for f in "${QDIR}/pending"/*; do
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    src=$(vvread_queue_entry_field "${base}" source)
    case "${src}" in
      hook|mcp) _queue_safe_rm "${f}" ;;
    esac
  done
}

# pending の最古の自動通知（hook/mcp）を 1 件 drop。drop で 0、無しで 1。
_queue_drop_oldest_auto() {
  local f base src
  # while-read で空白を含むパス（例: macOS の "Application Support"）に対応。
  # 未クォートの `for f in $(...)` は word-split でパスが壊れる（F-114b）。
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    src=$(vvread_queue_entry_field "${base}" source)
    case "${src}" in
      hook|mcp) _queue_safe_rm "${f}"; return 0 ;;
    esac
  done <<< "$(_queue_sorted "${QDIR}/pending")"
  return 1
}

# overflow 考慮の enqueue（mutation lock 保持前提）。
#   cli（手動）: full なら新規 reject + WARN（既存 cli は消さない）
#   hook/mcp（自動）: 最古の自動通知を drop して空ければ enqueue、無理なら reject
_queue_submit_with_overflow() {
  local source="${1:-}" speaker="${2:-}" text="${3:-}" ms="${4:-}"
  local count
  count=$(_queue_count "${QDIR}/pending")
  if [ "${count}" -ge "${_QUEUE_MAX}" ]; then
    case "${source}" in
      cli)
        log_warn "queue: full (max=${_QUEUE_MAX}) — rejecting manual cli entry"
        return 1
        ;;
      hook|mcp)
        if ! _queue_drop_oldest_auto; then
          log_warn "queue: full (max=${_QUEUE_MAX}) and no auto entry to drop — rejecting ${source}"
          return 1
        fi
        ;;
    esac
  fi
  _queue_enqueue "${source}" "${speaker}" "${text}" 0 "${ms}"
}

# 全 enqueue 共通入口。source 別分岐を mutation lock 内で実行する。
#   hook: marker 更新 → pending hook/mcp を evict → enqueue
#   mcp : CREATED_MS（env VVREAD_SAY_CREATED_MS）が marker 以下なら stale drop
#   cli : そのまま enqueue
# タイムスタンプは lock 取得前に捕捉し FIFO 順序を保証する（F-118）。
vvread_queue_submit() {
  local source="${1:-}" speaker="${2:-}" text="${3:-}"
  case "${source}" in
    cli|hook|mcp) ;;
    *) log_warn "queue: submit invalid source=${source}"; return 2 ;;
  esac
  if ! _queue_is_uint "${speaker}"; then
    log_warn "queue: submit invalid speaker=${speaker}"; return 2
  fi

  local submit_ms
  submit_ms=$(_now_ms)
  local tok
  if ! tok=$(_queue_mutate_lock); then
    log_warn "queue: submit failed to acquire mutation lock"
    return 1
  fi
  local rc=0
  _queue_submit_locked "${source}" "${speaker}" "${text}" "${submit_ms}" || rc=$?
  _queue_mutate_unlock "${tok}"
  return "${rc}"
}

# submit の本体（mutation lock 保持前提）
_queue_submit_locked() {
  local source="${1:-}" speaker="${2:-}" text="${3:-}" ms="${4:-}"
  case "${source}" in
    hook)
      _queue_marker_write "${QDIR}/queue_last_hook_ms" "$(_now_ms)"
      _queue_evict_pending_auto
      _queue_submit_with_overflow "${source}" "${speaker}" "${text}" "${ms}"
      ;;
    mcp)
      local created="${VVREAD_SAY_CREATED_MS:-}" marker
      if ! _queue_is_uint "${created}"; then
        log_warn "queue: mcp submit missing/invalid VVREAD_SAY_CREATED_MS — drop (safe side)"
        return 0
      fi
      marker=$(cat "${QDIR}/queue_last_hook_ms" 2>/dev/null || echo "")
      if _queue_is_uint "${marker}" && [ "${marker}" -ge "${created}" ]; then
        log_info "queue: mcp summary superseded by hook (marker=${marker} created=${created}) — drop"
        return 0
      fi
      _queue_submit_with_overflow "${source}" "${speaker}" "${text}" "${ms}"
      ;;
    cli)
      _queue_submit_with_overflow "${source}" "${speaker}" "${text}" "${ms}"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# pop / clear / recover / status
# ---------------------------------------------------------------------------

# pending の最古を playing へ移し basename を stdout（mutation lock を内部取得）。
# 対象消失時は skip-and-advance。pop できれば 0、空なら 1。
vvread_queue_pop() {
  local tok
  if ! tok=$(_queue_mutate_lock); then
    return 1
  fi
  local rc=1 f base
  # while-read で空白入りパス対応（未クォート for は word-split で壊れる・F-114b）。
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    if mv "${f}" "${QDIR}/playing/${base}" 2>/dev/null; then
      printf '%s\n' "${base}"
      rc=0
      break
    fi
  done <<< "$(_queue_sorted "${QDIR}/pending")"
  _queue_mutate_unlock "${tok}"
  return "${rc}"
}

# pending のみ削除（mutation lock 保持前提）
_queue_clear_pending_locked() {
  local f
  for f in "${QDIR}/pending"/*; do
    _queue_scan_ok "${f}" || continue
    _queue_safe_rm "${f}"
  done
}

# pending のみ削除（playing は残す）。再生は継続。
vvread_queue_clear() {
  local tok
  if ! tok=$(_queue_mutate_lock); then
    return 1
  fi
  _queue_clear_pending_locked
  _queue_mutate_unlock "${tok}"
  return 0
}

# failed 件数（新旧合算）が上限以上なら、新形式の最古（failed_ms 最小）を 1 件
# drop して空きを作る（mutation lock 保持前提）。旧形式は自動削除対象にしない。
_queue_failed_make_room() {
  local count f base
  count=$(_queue_count "${QDIR}/failed")
  [ "${count}" -ge "${_QUEUE_FAILED_MAX}" ] || return 0
  # while-read で空白入りパス対応（未クォート for は word-split で壊れる・F-114b）。
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    if [ "$(_queue_failed_format "${base}")" = "new" ]; then
      _queue_safe_rm "${f}"
      return 0
    fi
  done <<< "$(_queue_sorted "${QDIR}/failed")"
  return 1
}

# playing の orphan を retry+1 で回収（mutation lock 保持前提）。
#   r0 → pending/r1 / r1 → pending/r2 / r(>=MAX) → failed/r(N+1)
# failed へ退避する瞬間に failure 時刻 failed_ms を名前先頭へ付与する（FB-2）。
vvread_queue_recover_orphans() {
  local f base retry rest n fms
  for f in "${QDIR}/playing"/*; do
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    retry=$(vvread_queue_entry_field "${base}" retry)
    _queue_is_uint "${retry}" || retry=0
    rest="${base%.r*}"
    n=$((retry + 1))
    if [ "${retry}" -ge "${_QUEUE_RETRY_MAX}" ]; then
      _queue_failed_make_room
      fms=$(_now_ms)
      mv "${f}" "${QDIR}/failed/${fms}_${rest}.r${n}" 2>/dev/null || true
    else
      mv "${f}" "${QDIR}/pending/${rest}.r${n}" 2>/dev/null || true
    fi
  done
}

# queue.lock の状態を分類して stdout: none / ok / busy / wedge
#   live drainer 不在 → none
#   pending==0 → ok（処理対象なし）
#   progress fresh → ok
#   progress stale/absent + hb stale → wedge（生存だが進捗なし）
#   progress stale/absent + hb fresh → busy（再生/合成中）
vvread_queue_lock_class() {
  local qdir="${1:-}" owner pid pending thr
  owner=$(_queue_read_owner "${qdir}/queue.lock" 2>/dev/null || echo "")
  [ -n "${owner}" ] || { echo none; return 0; }
  pid="${owner%%${_QUEUE_TAB}*}"
  { _queue_is_uint "${pid}" && kill -0 "${pid}" 2>/dev/null; } || { echo none; return 0; }
  pending=$(_queue_count "${qdir}/pending")
  [ "${pending}" -eq 0 ] && { echo ok; return 0; }
  thr="${_QUEUE_DRAIN_STALE:-150}"
  if [ -f "${qdir}/queue.lock/progress" ] \
     && ! _queue_age_stale "${qdir}/queue.lock" progress "${thr}"; then
    echo ok; return 0
  fi
  if _queue_age_stale "${qdir}/queue.lock" hb "${thr}"; then
    echo wedge
  else
    echo busy
  fi
}

# mutate.lock が stale（保持しっぱなし）か。dead owner / hb age>MUTATE_STALE で 0。
_queue_mutate_is_stale() {
  local qdir="${1:-}" owner pid
  owner=$(_queue_read_owner "${qdir}/queue.mutate.lock" 2>/dev/null || echo "")
  [ -n "${owner}" ] || return 1
  pid="${owner%%${_QUEUE_TAB}*}"
  { _queue_is_uint "${pid}" && kill -0 "${pid}" 2>/dev/null; } || return 0
  _queue_age_stale "${qdir}/queue.mutate.lock" hb "${_QUEUE_MUTATE_STALE:-15}"
}

# pid が vvread drainer プロセスらしいか（best-effort identity check）。
# 不明（ps 取得不可 / command 不一致）は 1（= kill しない・安全側）。
_queue_pid_is_drainer() {
  local pid="${1:-}" cmd
  _queue_is_uint "${pid}" || return 1
  cmd=$(ps -p "${pid}" -o command= 2>/dev/null || ps -p "${pid}" -o args= 2>/dev/null || echo "")
  case "${cmd}" in
    *say.sh*|*vvread*) return 0 ;;
    *) return 1 ;;
  esac
}

# mode / pending / playing / failed / drainer 状態 / stale mutate を stdout
vvread_queue_status() {
  local mode="off"
  [ -f "${STATE_DIR}/queue_mode" ] && mode="on"
  printf 'mode: %s\n' "${mode}"
  printf 'pending: %s\n' "$(_queue_count "${QDIR}/pending")"
  printf 'playing: %s\n' "$(_queue_count "${QDIR}/playing")"
  printf 'failed: %s\n' "$(_queue_count "${QDIR}/failed")"
  case "$(vvread_queue_lock_class "${QDIR}")" in
    wedge) printf 'drainer: WARN wedged — run `vvread queue reset`\n' ;;
    busy)  printf 'drainer: busy (active playback/synthesis)\n' ;;
    ok)    printf 'drainer: ok\n' ;;
    *)     printf 'drainer: -\n' ;;
  esac
  if _queue_mutate_is_stale "${QDIR}"; then
    printf 'mutate.lock: WARN stale\n'
  fi
}

# 破壊的: drainer を identity 検証して kill し、queue dir を timestamp backup へ
# 退避（削除でなく mv）してから空 dir を再生成する。明示 `vvread queue reset` 専用。
# backup path を stdout（退避できなければ空）。player kill は caller の責務。
vvread_queue_reset() {
  local qdir="${1:-}"
  [ -n "${qdir}" ] || return 1
  # path guard: 想定外の qdir は触らない
  [ "${qdir}" = "${STATE_DIR}/queue" ] || {
    log_warn "queue: reset refused unexpected qdir=${qdir}"; return 1; }
  # 1. drainer pid を識別して停止（数値 / 非 $$ / 生存 / vvread drainer のみ）
  local owner pid i
  owner=$(_queue_read_owner "${qdir}/queue.lock" 2>/dev/null || echo "")
  if [ -n "${owner}" ]; then
    pid="${owner%%${_QUEUE_TAB}*}"
    if _queue_is_uint "${pid}" && [ "${pid}" != "$$" ] \
       && kill -0 "${pid}" 2>/dev/null && _queue_pid_is_drainer "${pid}"; then
      kill "${pid}" 2>/dev/null || true            # SIGTERM
      i=0
      while [ "${i}" -lt 20 ] && kill -0 "${pid}" 2>/dev/null; do
        sleep 0.1 2>/dev/null || true; i=$((i + 1))
      done
      kill -0 "${pid}" 2>/dev/null && kill -9 "${pid}" 2>/dev/null || true  # SIGKILL
    fi
  fi
  # 2. queue dir 全体を backup へ退避（lock/pending/playing/failed/signal を一括）
  local backup
  backup="${STATE_DIR}/queue.reset-backup.$(date +%Y%m%d_%H%M%S)"
  mv "${qdir}" "${backup}" 2>/dev/null || backup=""
  # 3. 空 queue dir を再生成
  ( umask 077; mkdir -p "${qdir}/pending" "${qdir}/playing" "${qdir}/failed" )
  # 4. backup path を返す
  [ -n "${backup}" ] && printf '%s\n' "${backup}"
  return 0
}

# ---------------------------------------------------------------------------
# stop 機構（vvread stop の全停止。drainer token 紐付け）
# ---------------------------------------------------------------------------

# live drainer がいれば token 付き stop.request を書き pending を削除して 0。
# drainer 不在なら pending + orphan playing + stale signal/lock を掃除して 1。
# （player kill は caller の責務。処理順: signal → pending 削除 → lock 解放 → kill）
vvread_queue_stop_request() {
  local qdir="${1:-}"
  local tok
  if ! tok=$(_queue_mutate_lock); then
    return 1
  fi
  local rc=1 live_token="" owner pid
  owner=$(_queue_read_owner "${qdir}/queue.lock" 2>/dev/null || echo "")
  if [ -n "${owner}" ]; then
    pid="${owner%%${_QUEUE_TAB}*}"
    if _queue_is_uint "${pid}" && kill -0 "${pid}" 2>/dev/null; then
      live_token="${owner#*${_QUEUE_TAB}}"
    fi
  fi
  if [ -n "${live_token}" ]; then
    ( umask 077
      printf '%s\n' "${live_token}" > "${qdir}/stop.request.tmp.$$" \
        && mv "${qdir}/stop.request.tmp.$$" "${qdir}/stop.request" )
    rm -f "${qdir}/skip.request" 2>/dev/null || true
    _queue_clear_pending_locked
    rc=0
  else
    _queue_clear_pending_locked
    local f
    for f in "${qdir}/playing"/*; do
      _queue_scan_ok "${f}" || continue
      _queue_safe_rm "${f}"
    done
    rm -f "${qdir}/stop.request" 2>/dev/null || true
    rm -f "${qdir}/skip.request" 2>/dev/null || true
    vvread_queue_reclaim_stale "${qdir}/queue.lock"
    rc=1
  fi
  _queue_mutate_unlock "${tok}"
  return "${rc}"
}

# drain ループが各 entry/chunk 再生前に呼ぶ。自分の token と一致する
# stop.request があれば 0（halt すべき）、無ければ 1。
vvread_queue_should_halt() {
  local qdir="${1:-}" token="${2:-}"
  [ -f "${qdir}/stop.request" ] || return 1
  local req
  req=$(cat "${qdir}/stop.request" 2>/dev/null || echo "")
  [ -n "${token}" ] && [ "${req}" = "${token}" ]
}

# halt 後に stop.request を消す（drainer が exit 前に呼ぶ）。
vvread_queue_clear_stop() {
  local qdir="${1:-}"
  rm -f "${qdir}/stop.request" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# skip 機構（現エントリのみ停止し次へ。B-144）
# ---------------------------------------------------------------------------
#
# stop と同型だが決定的差: pending を一切削除しない（halt ではなく現 entry の
# 残り chunk を捨てて次 entry へ continue させる signal）。

# live drainer がいれば token 付き skip.request を書いて 0、不在なら 1。
# pending は削除しない（stop_request との決定的差）。
vvread_queue_skip_request() {
  local qdir="${1:-}"
  local tok
  if ! tok=$(_queue_mutate_lock); then
    return 1
  fi
  local rc=1 live_token="" owner pid
  owner=$(_queue_read_owner "${qdir}/queue.lock" 2>/dev/null || echo "")
  if [ -n "${owner}" ]; then
    pid="${owner%%${_QUEUE_TAB}*}"
    if _queue_is_uint "${pid}" && kill -0 "${pid}" 2>/dev/null; then
      live_token="${owner#*${_QUEUE_TAB}}"
    fi
  fi
  if [ -n "${live_token}" ]; then
    ( umask 077
      printf '%s\n' "${live_token}" > "${qdir}/skip.request.tmp.$$" \
        && mv "${qdir}/skip.request.tmp.$$" "${qdir}/skip.request" )
    rc=0
  fi
  _queue_mutate_unlock "${tok}"
  return "${rc}"
}

# drain ループが各 entry/chunk 再生前に呼ぶ。自分の token と一致する
# skip.request があれば 0、無ければ 1。
vvread_queue_should_skip() {
  local qdir="${1:-}" token="${2:-}"
  [ -f "${qdir}/skip.request" ] || return 1
  local req
  req=$(cat "${qdir}/skip.request" 2>/dev/null || echo "")
  [ -n "${token}" ] && [ "${req}" = "${token}" ]
}

# 【FB2-1】token 一致時のみ skip.request を削除する。
# stop×skip の race（stop が消した直後に skip 側が新 token で再作成 → halt 後に
# 残る）を防ぎ、古い drainer が新しい token 向け signal を消さないことを保証する。
vvread_queue_clear_skip() {
  local qdir="${1:-}" token="${2:-}"
  [ -f "${qdir}/skip.request" ] || return 0
  local req
  req=$(cat "${qdir}/skip.request" 2>/dev/null || echo "")
  if [ -n "${token}" ] && [ "${req}" = "${token}" ]; then
    rm -f "${qdir}/skip.request" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# failed entry 管理 (B-145)
# ---------------------------------------------------------------------------
#
# failed/ は retry 上限超で退避されたエントリ。新形式は失敗時刻 failed_ms を
# 名前先頭に持つ（TTL cleanup の基準）。旧形式（failed_ms 前置前に退避済み）も
# 互換処理する。

# failed/ を走査して一覧を出力。
#   新形式: "${failed_ms} ${created_ms} ${speaker} ${source} ${retry}"
#   旧形式: "legacy ${created_ms} ${speaker} ${source} ${retry}"
vvread_queue_failed_list() {
  local f base fmt failed_ms created_ms speaker source retry
  # while-read で空白入りパス対応（未クォート for は word-split で壊れる・F-114b）。
  while IFS= read -r f; do
    [ -n "${f}" ] || continue
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    fmt=$(_queue_failed_format "${base}")
    created_ms=$(vvread_queue_entry_field "${base}" created_ms)
    speaker=$(vvread_queue_entry_field "${base}" speaker)
    source=$(vvread_queue_entry_field "${base}" source)
    retry=$(vvread_queue_entry_field "${base}" retry)
    if [ "${fmt}" = "new" ]; then
      failed_ms=$(vvread_queue_entry_field "${base}" failed_ms)
      printf '%s %s %s %s %s\n' "${failed_ms}" "${created_ms}" "${speaker}" "${source}" "${retry}"
    else
      printf 'legacy %s %s %s %s\n' "${created_ms}" "${speaker}" "${source}" "${retry}"
    fi
  done <<< "$(_queue_sorted "${QDIR}/failed")"
}

# failed/ の全エントリ（新旧両方）を削除。
vvread_queue_failed_clear() {
  local tok f
  if ! tok=$(_queue_mutate_lock); then return 1; fi
  for f in "${QDIR}/failed"/*; do
    _queue_scan_ok "${f}" || continue
    _queue_safe_rm "${f}"
  done
  _queue_mutate_unlock "${tok}"
  return 0
}

# 【FB-3】basename を厳格検証して failed entry を 1 件削除する。
#   拒否（return 2）: 空 / `/` / `\` / `..` / `.tmp.` を含む。
#   見つからない / guard 失敗: return 1。削除成功: return 0。
vvread_queue_failed_rm() {
  local qdir="${1:-}" name="${2:-}"
  case "${name}" in
    ''|*/*|*'\'*|*..*|*.tmp.*) return 2 ;;
  esac
  local tok target rc=1
  if ! tok=$(_queue_mutate_lock); then return 1; fi
  target="${qdir}/failed/${name}"
  if _queue_safe_rm "${target}"; then rc=0; fi
  _queue_mutate_unlock "${tok}"
  return "${rc}"
}

# 【FB-6】TTL cleanup（failure 時刻基準・手動実行のみ）。削除件数を stdout。
#   旧形式は対象外（failed_ms 不在）→ 件数を log_warn。
#   非数値 failed_ms は skip + log_warn。
vvread_queue_failed_cleanup() {
  local qdir="${1:-}" ttl_ms="${2:-}"
  if ! _queue_is_uint "${ttl_ms}"; then
    log_warn "queue: failed cleanup invalid ttl_ms=${ttl_ms}"
    return 2
  fi
  local tok f base fmt fms now age removed=0 legacy=0
  now=$(_now_ms)
  if ! tok=$(_queue_mutate_lock); then return 1; fi
  for f in "${qdir}/failed"/*; do
    _queue_scan_ok "${f}" || continue
    base="${f##*/}"
    fmt=$(_queue_failed_format "${base}")
    if [ "${fmt}" != "new" ]; then
      legacy=$((legacy + 1)); continue
    fi
    fms=$(vvread_queue_entry_field "${base}" failed_ms)
    if ! _queue_is_uint "${fms}"; then
      log_warn "queue: failed cleanup non-numeric failed_ms base=${base}"; continue
    fi
    age=$((now - fms))
    [ "${age}" -lt 0 ] && age=0
    if [ "${age}" -gt "${ttl_ms}" ]; then
      if _queue_safe_rm "${f}"; then removed=$((removed + 1)); fi
    fi
  done
  _queue_mutate_unlock "${tok}"
  if [ "${legacy}" -gt 0 ]; then
    log_warn "queue: failed cleanup skipped ${legacy} legacy entries (no failed_ms)"
  fi
  echo "${removed}"
  return 0
}
