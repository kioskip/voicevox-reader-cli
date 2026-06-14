"""lib/queue.sh の単体テスト (B-015 / B-138 WS-A1)

bash 関数を subprocess 経由で source して呼び出すスタイル
(test_lib_session.py と同じ流儀)。STATE_DIR を tmp に隔離し、
vvread_queue_dirs_init 済みの状態でスクリプトを評価する。

テスト対象は queue.sh の契約:
  - submit/pop/clear/recover/status のキュー操作
  - mkdir 排他ロックの契約（単一 drainer / owner-only release /
    死亡 owner reclaim / crash-mid-init でもデッドロックしない）
  - source 別 submit 分岐（hook evict / mcp stale drop / cli 維持）
  - 破壊的操作の path guard
  - stop 機構（token 付き stop.request）
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB_QUEUE = REPO / "scripts" / "lib" / "queue.sh"
LIB_LOG = REPO / "scripts" / "lib" / "log.sh"

# 確実に「死亡」している PID。2^31-1 は macOS/Linux の PID 有効上限
# （macOS kern.maxproc, Linux pid_max ≤ 2^22）を超えるため kill -0 が必ず失敗する。
# 短命プロセスの PID 再利用に依存しないための決定的な値。
DEAD_PID = "2147483647"


def run_q(script: str, tmp_path: Path, env_extra=None, init=True):
    """log.sh + queue.sh を source し、STATE_DIR を隔離して script を実行。

    set -e は付けない（中間コマンドの非ゼロ復帰を許容してrcを明示確認するため）。
    """
    state = tmp_path / "state"
    log = tmp_path / "log"
    init_line = "vvread_queue_dirs_init; " if init else ""
    full = (
        "set -uo pipefail; "
        f'STATE_DIR="{state}"; LOG_DIR="{log}"; '
        f'source "{LIB_LOG}"; source "{LIB_QUEUE}"; '
        f"{init_line}"
        f"{script}"
    )
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env=env, timeout=30)


def _qdir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "queue"


def run_q_state(script: str, state: Path, log: Path):
    """STATE_DIR を明示指定して script を実行（空白入りパス検証用）。"""
    full = (
        "set -uo pipefail; "
        f'STATE_DIR="{state}"; LOG_DIR="{log}"; '
        f'source "{LIB_LOG}"; source "{LIB_QUEUE}"; '
        "vvread_queue_dirs_init; "
        f"{script}"
    )
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env=env, timeout=30)


# ---------------------------------------------------------------------------
# dirs_init / 権限
# ---------------------------------------------------------------------------

class TestDirsInit:
    def test_creates_subdirs(self, tmp_path):
        r = run_q("true", tmp_path)
        assert r.returncode == 0, r.stderr
        for sub in ("pending", "playing", "failed"):
            assert (_qdir(tmp_path) / sub).is_dir()

    def test_dir_perms_700(self, tmp_path):
        run_q("true", tmp_path)
        mode = os.stat(_qdir(tmp_path) / "pending").st_mode & 0o777
        assert mode == 0o700, oct(mode)

    def test_entry_file_perms_600(self, tmp_path):
        run_q('vvread_queue_submit cli 3 "hello"', tmp_path)
        files = list((_qdir(tmp_path) / "pending").glob("*"))
        assert len(files) == 1
        mode = files[0].stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)

    def test_no_state_dir_returns_1(self, tmp_path):
        # STATE_DIR を空にして dirs_init → return 1
        full = (
            "set -uo pipefail; "
            'STATE_DIR=""; LOG_DIR="%s"; ' % (tmp_path / "log")
            + f'source "{LIB_LOG}"; source "{LIB_QUEUE}"; '
            "vvread_queue_dirs_init; echo rc=$?"
        )
        r = subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                           timeout=30)
        assert "rc=1" in r.stdout


# ---------------------------------------------------------------------------
# submit / pop / entry_field
# ---------------------------------------------------------------------------

class TestSubmitPop:
    def test_submit_creates_pending(self, tmp_path):
        r = run_q('vvread_queue_submit cli 3 "hello"; echo rc=$?', tmp_path)
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 1

    def test_pop_preserves_name_and_fields(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 7 "konnichiwa"; '
            'base=$(vvread_queue_pop); '
            'echo "SPK=$(vvread_queue_entry_field "$base" speaker)"; '
            'echo "SRC=$(vvread_queue_entry_field "$base" source)"; '
            'echo "RET=$(vvread_queue_entry_field "$base" retry)"; '
            '[ -f "${QDIR}/playing/$base" ] && echo PLAYING_OK; '
            'printf "BODY=%s\\n" "$(cat "${QDIR}/playing/$base")"',
            tmp_path,
        )
        assert "SPK=7" in r.stdout, r.stdout
        assert "SRC=cli" in r.stdout
        assert "RET=0" in r.stdout
        assert "PLAYING_OK" in r.stdout
        assert "BODY=konnichiwa" in r.stdout

    def test_pop_empty_returns_1(self, tmp_path):
        r = run_q('vvread_queue_pop; echo rc=$?', tmp_path)
        assert "rc=1" in r.stdout

    def test_created_ms_is_epoch_ms(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "x"; base=$(vvread_queue_pop); '
            'vvread_queue_entry_field "$base" created_ms',
            tmp_path,
        )
        ms = r.stdout.strip().splitlines()[-1]
        assert ms.isdigit()
        # epoch ms は 13 桁（2001 年以降）。秒粒度 fallback でも 10 桁以上。
        assert len(ms) >= 12, ms

    def test_invalid_source_rejected(self, tmp_path):
        r = run_q('vvread_queue_submit bogus 3 "x"; echo rc=$?', tmp_path)
        assert "rc=2" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0

    def test_invalid_speaker_rejected(self, tmp_path):
        r = run_q('vvread_queue_submit cli abc "x"; echo rc=$?', tmp_path)
        assert "rc=2" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0

    def test_two_submits_no_overwrite(self, tmp_path):
        # 連続 submit が互いを上書きしない（nonce 再生成）
        run_q('vvread_queue_submit cli 3 "first"; '
              'vvread_queue_submit cli 3 "second"', tmp_path)
        files = sorted((_qdir(tmp_path) / "pending").glob("*"))
        assert len(files) == 2
        bodies = {f.read_text() for f in files}
        assert bodies == {"first", "second"}

    def test_submit_fifo_pop_order(self, tmp_path):
        # F-118: 複数 submit のエントリが FIFO（提出順）で pop される。
        # submit_ms は mutate_lock 取得前に捕捉されるため、ロック競合があっても
        # タイムスタンプが提出順を反映する（逆転しない）。
        r = run_q(
            'vvread_queue_submit cli 3 "first"; '
            'vvread_queue_submit cli 3 "second"; '
            'vvread_queue_submit cli 3 "third"; '
            'b1=$(vvread_queue_pop); b2=$(vvread_queue_pop); b3=$(vvread_queue_pop); '
            'cat "${QDIR}/playing/${b1}"; echo; '
            'cat "${QDIR}/playing/${b2}"; echo; '
            'cat "${QDIR}/playing/${b3}"; echo',
            tmp_path,
        )
        lines = [l for l in r.stdout.splitlines() if l]
        assert lines == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# source 別分岐（collision policy）
# ---------------------------------------------------------------------------

class TestSourceBranching:
    def test_hook_evicts_pending_auto_keeps_cli(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "manual"; '
            'VVREAD_SAY_CREATED_MS=99999999999999 vvread_queue_submit mcp 3 "summary"; '
            'vvread_queue_submit hook 3 "fulltext"; '
            'for f in "${QDIR}/pending"/*; do '
            '  b=$(basename "$f"); echo "SRC=$(vvread_queue_entry_field "$b" source)"; '
            'done | sort | uniq -c',
            tmp_path,
        )
        # mcp は evict され、cli と hook が残る
        assert "SRC=cli" in r.stdout
        assert "SRC=hook" in r.stdout
        assert "SRC=mcp" not in r.stdout

    def test_mcp_stale_dropped_by_hook_marker(self, tmp_path):
        # hook が marker=now を立てた後、古い CREATED_MS の mcp は drop
        r = run_q(
            'vvread_queue_submit hook 3 "fulltext"; '
            'VVREAD_SAY_CREATED_MS=1 vvread_queue_submit mcp 3 "old summary"; echo rc=$?',
            tmp_path,
        )
        assert "rc=0" in r.stdout  # drop は正常終了
        # pending は hook 1 件のみ
        assert _queue_count(tmp_path, "pending") == 1
        srcs = _entry_sources(tmp_path, "pending")
        assert srcs == ["hook"]

    def test_mcp_fresh_enqueued(self, tmp_path):
        # marker 無し（hook 未到着）なら mcp は enqueue される
        r = run_q(
            'VVREAD_SAY_CREATED_MS=99999999999999 vvread_queue_submit mcp 3 "summary"; echo rc=$?',
            tmp_path,
        )
        assert "rc=0" in r.stdout
        assert _entry_sources(tmp_path, "pending") == ["mcp"]

    def test_mcp_missing_created_ms_dropped(self, tmp_path):
        r = run_q('vvread_queue_submit mcp 3 "summary"; echo rc=$?', tmp_path)
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0

    def test_marker_atomic_no_tmp_left(self, tmp_path):
        run_q('vvread_queue_submit hook 3 "x"', tmp_path)
        marker = _qdir(tmp_path) / "queue_last_hook_ms"
        assert marker.is_file()
        # tmp ファイルが残らない
        tmps = list(_qdir(tmp_path).glob("queue_last_hook_ms.tmp.*"))
        assert tmps == []


# ---------------------------------------------------------------------------
# clear / overflow（playing を消さない）
# ---------------------------------------------------------------------------

class TestClearOverflow:
    def test_clear_removes_pending_keeps_playing(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_pop >/dev/null; '  # → playing
            'vvread_queue_submit cli 3 "b"; '  # → pending
            'vvread_queue_clear; echo rc=$?',
            tmp_path,
        )
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0
        assert _queue_count(tmp_path, "playing") == 1

    def test_overflow_cli_rejected(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_submit cli 3 "b"; '
            'vvread_queue_submit cli 3 "c"; echo rc=$?',
            tmp_path, env_extra={"VVREAD_QUEUE_MAX": "2"},
        )
        assert "rc=1" in r.stdout
        assert _queue_count(tmp_path, "pending") == 2

    def test_overflow_auto_drops_oldest(self, tmp_path):
        # MAX=2、mcp を 3 件 → 最古を drop して 2 件に保つ
        r = run_q(
            'VVREAD_SAY_CREATED_MS=99999999999999 vvread_queue_submit mcp 3 "m1"; '
            'VVREAD_SAY_CREATED_MS=99999999999999 vvread_queue_submit mcp 3 "m2"; '
            'VVREAD_SAY_CREATED_MS=99999999999999 vvread_queue_submit mcp 3 "m3"; echo rc=$?',
            tmp_path, env_extra={"VVREAD_QUEUE_MAX": "2"},
        )
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 2

    def test_invalid_queue_max_fallback_64(self, tmp_path):
        # 不正値は 64 へ fallback（= 3 件目も入る）
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_submit cli 3 "b"; '
            'vvread_queue_submit cli 3 "c"; echo rc=$?',
            tmp_path, env_extra={"VVREAD_QUEUE_MAX": "0"},
        )
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 3


# ---------------------------------------------------------------------------
# recover_orphans（retry+1 / failed 退避）
# ---------------------------------------------------------------------------

class TestRecoverOrphans:
    def test_orphan_requeued_with_retry_increment(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_pop >/dev/null; '  # playing r0
            'vvread_queue_recover_orphans; '
            'for f in "${QDIR}/pending"/*; do b=$(basename "$f"); '
            '  echo "RET=$(vvread_queue_entry_field "$b" retry)"; done',
            tmp_path,
        )
        assert "RET=1" in r.stdout
        assert _queue_count(tmp_path, "playing") == 0
        assert _queue_count(tmp_path, "pending") == 1

    def test_orphan_exceeding_retry_goes_failed(self, tmp_path):
        r = run_q(
            # r2 の playing を直接作る（retry 上限 = 2）
            'printf "x" > "${QDIR}/playing/100_1.55.3.cli.r2"; '
            'vvread_queue_recover_orphans; echo done',
            tmp_path,
        )
        assert "done" in r.stdout
        assert _queue_count(tmp_path, "playing") == 0
        assert _queue_count(tmp_path, "failed") == 1


# ---------------------------------------------------------------------------
# ロック契約（advisor 指摘の決定的テスト）
# ---------------------------------------------------------------------------

class TestLockContract:
    def test_single_drainer_election(self, tmp_path):
        # A 取得（生存 pid=$$）→ B 取得失敗 → reclaim no-op（生存）
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); echo "TOK=$tok"; '
            'vvread_queue_acquire "${QDIR}/queue.lock" && echo SECOND_OK || echo SECOND_FAIL; '
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL_LOCKED',
            tmp_path,
        )
        assert "SECOND_FAIL" in r.stdout
        assert "STILL_LOCKED" in r.stdout

    def test_dead_owner_reclaimed(self, tmp_path):
        # 死亡 pid の owner → reclaim で退避 → 取得可能（決定的な DEAD_PID を使用）
        # owner は canonical "pid<TAB>token" 形式で上書きする。
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "%s\\t%s\\n" "' + DEAD_PID + '" "tok" > "${QDIR}/queue.lock/owner"; '
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock") && echo "REACQUIRED=$tok" || echo FAIL',
            tmp_path,
        )
        assert "REACQUIRED=" in r.stdout

    def test_crash_mid_init_reclaimed(self, tmp_path):
        # token 無し + 死亡 pid（mkdir 直後にクラッシュ）→ reclaim で必ず回収
        r = run_q(
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\n" "' + DEAD_PID + '" > "${QDIR}/queue.lock/pid"; '  # token は書かない
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "RECLAIMED" in r.stdout

    def test_empty_owner_absent_within_grace_kept(self, tmp_path):
        # owner-absent の空ロックは INIT_GRACE 内（mid-init winner）は回収しない
        r = run_q(
            'mkdir -p "${QDIR}/queue.lock"; '
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "STILL" in r.stdout

    def test_empty_owner_absent_after_grace_reclaimed(self, tmp_path):
        # owner-absent でも dir mtime が INIT_GRACE 超なら回収（デッドロックさせない）
        # touch -t で dir mtime を過去に backdate して決定的に grace 超過させる。
        r = run_q(
            'mkdir -p "${QDIR}/queue.lock"; '
            'touch -t 202001010000 "${QDIR}/queue.lock"; '
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "RECLAIMED" in r.stdout

    def test_release_only_by_owner(self, tmp_path):
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'vvread_queue_release "${QDIR}/queue.lock" WRONG && echo REL_OK || echo REL_FAIL; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL_LOCKED; '
            'vvread_queue_release "${QDIR}/queue.lock" "$tok" && echo REL_OK2 || echo REL_FAIL2; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo GONE',
            tmp_path,
        )
        assert "REL_FAIL" in r.stdout
        assert "STILL_LOCKED" in r.stdout
        assert "REL_OK2" in r.stdout
        assert "GONE" in r.stdout


# ---------------------------------------------------------------------------
# owner モデル / token 契約 / staleness 条件付き self-reclaim（F-114 Phase 1）
# ---------------------------------------------------------------------------

class TestOwnerModelAndSelfReclaim:
    def test_canonical_owner_written(self, tmp_path):
        # acquire は canonical owner（"pid<TAB>token"）を書く
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            '[ -f "${QDIR}/queue.lock/owner" ] && echo HAS_OWNER; '
            'opid=$(cut -f1 "${QDIR}/queue.lock/owner"); '
            'otok=$(cut -f2 "${QDIR}/queue.lock/owner"); '
            '[ "$opid" = "$$" ] && [ "$otok" = "$tok" ] && echo OWNER_MATCH',
            tmp_path,
        )
        assert "HAS_OWNER" in r.stdout
        assert "OWNER_MATCH" in r.stdout

    def test_token_contract_acquire_stored_release(self, tmp_path):
        # acquire returned token == stored owner token == release token
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'stored=$(cat "${QDIR}/queue.lock/owner" | cut -f2); '
            '[ "$tok" = "$stored" ] && echo CONTRACT_OK || echo CONTRACT_FAIL; '
            'vvread_queue_release "${QDIR}/queue.lock" "$stored" && echo REL_OK || echo REL_FAIL',
            tmp_path,
        )
        assert "CONTRACT_OK" in r.stdout
        assert "REL_OK" in r.stdout

    def test_legacy_pid_token_fallback(self, tmp_path):
        # owner 不在 + 旧 pid/token → legacy fallback で読める（死亡 pid は回収される）
        r = run_q(
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\n" "' + DEAD_PID + '" > "${QDIR}/queue.lock/pid"; '
            'printf "%s\\n" "legacytok" > "${QDIR}/queue.lock/token"; '
            'o=$(_queue_read_owner "${QDIR}/queue.lock"); '
            'printf "%s" "$o" | grep -q "^' + DEAD_PID + '" && echo LEGACY_READ; '
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "LEGACY_READ" in r.stdout
        assert "RECLAIMED" in r.stdout

    def test_owner_publish_no_clobber(self, tmp_path):
        # 既存 owner がある lock dir に acquire しても owner を上書きしない（mkdir で弾く）
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'vvread_queue_acquire "${QDIR}/queue.lock" && echo SECOND_OK || echo SECOND_FAIL; '
            'cur=$(cat "${QDIR}/queue.lock/owner" | cut -f2); '
            '[ "$cur" = "$tok" ] && echo OWNER_INTACT || echo OWNER_CLOBBERED',
            tmp_path,
        )
        assert "SECOND_FAIL" in r.stdout
        assert "OWNER_INTACT" in r.stdout

    def test_fresh_self_owned_mutate_not_reclaimed(self, tmp_path):
        # fresh な self-owned mutate.lock（hb 直近）は self-reclaim しない（nested 保護）
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.mutate.lock"); '
            'vvread_queue_reclaim_stale "${QDIR}/queue.mutate.lock" 1 15; '
            '[ -d "${QDIR}/queue.mutate.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "STILL" in r.stdout

    def test_stale_self_owned_mutate_reclaimed(self, tmp_path):
        # stale な self-owned mutate.lock（hb を過去に backdate）は self-reclaim する
        # = 確定 wedge（自己リーク）の決定的解消
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.mutate.lock" >/dev/null; '
            'printf "%s\\n" "100" > "${QDIR}/queue.mutate.lock/hb"; '  # 1970 相当 → 超過
            'vvread_queue_reclaim_stale "${QDIR}/queue.mutate.lock" 1 15; '
            '[ -d "${QDIR}/queue.mutate.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "RECLAIMED" in r.stdout

    def test_self_leaked_mutate_pop_recovers(self, tmp_path):
        # 決定的注入: 自分の pid で stale な mutate.lock を漏らした状態でも pop が
        # self-reclaim して前進する（confirmed wedge の再現→解消）
        r = run_q(
            'vvread_queue_submit cli 3 "entry one" >/dev/null; '
            # mutate.lock を自 pid で漏らす（hb を過去に）→ pop が self-reclaim できるか
            'vvread_queue_acquire "${QDIR}/queue.mutate.lock" >/dev/null; '
            'printf "%s\\n" "100" > "${QDIR}/queue.mutate.lock/hb"; '
            'b=$(vvread_queue_pop) && echo "POP_OK=$b" || echo POP_FAIL',
            tmp_path,
        )
        assert "POP_OK=" in r.stdout

    def test_queue_lock_self_not_reclaimed_without_allow_self(self, tmp_path):
        # 罠1回帰: allow_self=0 では自分の生存 queue.lock を stale でも回収しない
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "%s\\n" "100" > "${QDIR}/queue.lock/hb"; '  # stale
            'vvread_queue_reclaim_stale "${QDIR}/queue.lock" 0 150; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "STILL" in r.stdout


# ---------------------------------------------------------------------------
# queue.lock heartbeat 回収 / ownership（F-114 Phase 2・double-play 境界）
# ---------------------------------------------------------------------------

class TestDrainReclaimAndOwnership:
    def test_owns_lock_true_false(self, tmp_path):
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'vvread_queue_owns_lock "${QDIR}" "$tok" && echo OWN_OK || echo OWN_FAIL; '
            'vvread_queue_owns_lock "${QDIR}" WRONG && echo WRONG_OK || echo WRONG_FAIL',
            tmp_path,
        )
        assert "OWN_OK" in r.stdout
        assert "WRONG_FAIL" in r.stdout

    def test_other_live_stale_hb_reclaimed(self, tmp_path):
        # 他生存 pid + 古い hb → reclaim_drain_stale で回収（生存 stuck drainer 救済）
        r = run_q(
            'sleep 60 & op=$!; '
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\t%s\\n" "$op" "otok" > "${QDIR}/queue.lock/owner"; '
            'printf "100\\n" > "${QDIR}/queue.lock/hb"; '
            'vvread_queue_reclaim_drain_stale "${QDIR}"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED; '
            'kill $op 2>/dev/null',
            tmp_path,
        )
        assert "RECLAIMED" in r.stdout

    def test_other_live_fresh_hb_kept(self, tmp_path):
        # 他生存 pid + 新しい hb（再生中相当）→ 回収しない（double-play 防止）
        r = run_q(
            'sleep 60 & op=$!; '
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\t%s\\n" "$op" "otok" > "${QDIR}/queue.lock/owner"; '
            'printf "%s\\n" "$(date +%s)" > "${QDIR}/queue.lock/hb"; '
            'vvread_queue_reclaim_drain_stale "${QDIR}"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED; '
            'kill $op 2>/dev/null',
            tmp_path,
        )
        assert "STILL" in r.stdout

    def test_other_live_hb_absent_kept(self, tmp_path):
        # 他生存 pid + hb 不在 → 回収しない（compat: 無限に古い扱いにしない）
        r = run_q(
            'sleep 60 & op=$!; '
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\t%s\\n" "$op" "otok" > "${QDIR}/queue.lock/owner"; '
            'vvread_queue_reclaim_drain_stale "${QDIR}"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED; '
            'kill $op 2>/dev/null',
            tmp_path,
        )
        assert "STILL" in r.stdout

    def test_dead_pid_hb_absent_reclaimed(self, tmp_path):
        # 死亡 pid + hb 不在 → 回収（hb に関係なく dead は回収）
        r = run_q(
            'mkdir -p "${QDIR}/queue.lock"; '
            'printf "%s\\t%s\\n" "' + DEAD_PID + '" "otok" > "${QDIR}/queue.lock/owner"; '
            'vvread_queue_reclaim_drain_stale "${QDIR}"; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL || echo RECLAIMED',
            tmp_path,
        )
        assert "RECLAIMED" in r.stdout

    def test_hb_and_progress_updated_separately(self, tmp_path):
        # drain_heartbeat は hb のみ / progress は progress のみ更新（分離）
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'rm -f "${QDIR}/queue.lock/progress"; '
            'vvread_queue_drain_heartbeat "${QDIR}" "$tok"; '
            '[ -f "${QDIR}/queue.lock/hb" ] && echo HAS_HB; '
            '[ -f "${QDIR}/queue.lock/progress" ] && echo HAS_PROG_UNEXPECTED || echo NO_PROG; '
            'vvread_queue_progress "${QDIR}" "$tok"; '
            '[ -f "${QDIR}/queue.lock/progress" ] && echo HAS_PROG',
            tmp_path,
        )
        assert "HAS_HB" in r.stdout
        assert "NO_PROG" in r.stdout
        assert "HAS_PROG" in r.stdout

    def test_drain_heartbeat_only_by_owner(self, tmp_path):
        # token 不一致では hb を更新しない
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "100\\n" > "${QDIR}/queue.lock/hb"; '
            'vvread_queue_drain_heartbeat "${QDIR}" WRONG && echo HB_OK || echo HB_FAIL; '
            'v=$(cat "${QDIR}/queue.lock/hb"); [ "$v" = "100" ] && echo HB_UNCHANGED',
            tmp_path,
        )
        assert "HB_FAIL" in r.stdout
        assert "HB_UNCHANGED" in r.stdout


# ---------------------------------------------------------------------------
# wedge/busy 分類 / stale mutate / reset（F-114 Phase 4 observability）
# ---------------------------------------------------------------------------

class TestWedgeClassAndReset:
    def test_class_none_without_drainer(self, tmp_path):
        r = run_q('echo "CLS=$(vvread_queue_lock_class "${QDIR}")"', tmp_path)
        assert "CLS=none" in r.stdout

    def test_class_ok_fresh_progress(self, tmp_path):
        # 生存 drainer + pending>0 + progress fresh → ok
        r = run_q(
            'vvread_queue_submit cli 3 "x" >/dev/null; '
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "%s\\n" "$(date +%s)" > "${QDIR}/queue.lock/progress"; '
            'echo "CLS=$(vvread_queue_lock_class "${QDIR}")"',
            tmp_path,
        )
        assert "CLS=ok" in r.stdout

    def test_class_wedge_stale_hb_and_progress(self, tmp_path):
        # 生存 drainer + pending>0 + progress stale + hb stale → wedge
        r = run_q(
            'vvread_queue_submit cli 3 "x" >/dev/null; '
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "100\\n" > "${QDIR}/queue.lock/hb"; '
            'printf "100\\n" > "${QDIR}/queue.lock/progress"; '
            'echo "CLS=$(vvread_queue_lock_class "${QDIR}")"',
            tmp_path,
        )
        assert "CLS=wedge" in r.stdout

    def test_class_busy_fresh_hb_stale_progress(self, tmp_path):
        # 生存 drainer + pending>0 + progress stale + hb fresh → busy（再生中）
        r = run_q(
            'vvread_queue_submit cli 3 "x" >/dev/null; '
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "%s\\n" "$(date +%s)" > "${QDIR}/queue.lock/hb"; '
            'printf "100\\n" > "${QDIR}/queue.lock/progress"; '
            'echo "CLS=$(vvread_queue_lock_class "${QDIR}")"',
            tmp_path,
        )
        assert "CLS=busy" in r.stdout

    def test_status_prints_wedge_warn(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "x" >/dev/null; '
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'printf "100\\n" > "${QDIR}/queue.lock/hb"; '
            'printf "100\\n" > "${QDIR}/queue.lock/progress"; '
            'vvread_queue_status',
            tmp_path,
        )
        assert "drainer: WARN wedged" in r.stdout

    def test_mutate_stale_detected(self, tmp_path):
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.mutate.lock" >/dev/null; '
            'printf "100\\n" > "${QDIR}/queue.mutate.lock/hb"; '
            'vvread_queue_status; '
            '_queue_mutate_is_stale "${QDIR}" && echo MUTATE_STALE || echo MUTATE_FRESH',
            tmp_path,
        )
        assert "mutate.lock: WARN stale" in r.stdout
        assert "MUTATE_STALE" in r.stdout

    def test_reset_backs_up_and_recreates(self, tmp_path):
        # reset は queue dir を backup へ退避し空 dir を再生成。owner pid==$$ なので
        # kill はスキップされる（自分自身は kill しない）。
        r = run_q(
            'vvread_queue_submit cli 3 "x" >/dev/null; '
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'bk=$(vvread_queue_reset "${QDIR}"); '
            'echo "BK=$bk"; '
            '[ -d "$bk" ] && echo BACKUP_EXISTS; '
            '[ -d "$bk/queue.lock" ] && echo LOCK_IN_BACKUP; '
            '[ -d "${QDIR}/pending" ] && echo PENDING_RECREATED; '
            '[ -d "${QDIR}/queue.lock" ] && echo STILL_LOCKED || echo LOCK_GONE; '
            '[ "$(_queue_count "${QDIR}/pending")" -eq 0 ] && echo PENDING_EMPTY',
            tmp_path,
        )
        assert "BACKUP_EXISTS" in r.stdout
        assert "LOCK_IN_BACKUP" in r.stdout
        assert "PENDING_RECREATED" in r.stdout
        assert "LOCK_GONE" in r.stdout
        assert "PENDING_EMPTY" in r.stdout

    def test_reset_does_not_kill_self(self, tmp_path):
        # owner pid==$$（テスト自身）→ kill されない（プロセスが生き続ける）
        r = run_q(
            'vvread_queue_acquire "${QDIR}/queue.lock" >/dev/null; '
            'vvread_queue_reset "${QDIR}" >/dev/null; '
            'echo SURVIVED',
            tmp_path,
        )
        assert "SURVIVED" in r.stdout


# ---------------------------------------------------------------------------
# 空白入り STATE_DIR（macOS "Application Support"）の回帰（F-114b）
# ---------------------------------------------------------------------------
#
# 未クォートの `for f in $(_queue_sorted ...)` は word-split でパスが壊れ、
# pop の mv が全件失敗→pending が永久に drain されず queue が wedge する。
# fake テストは tmp_path に空白を含まないため見逃していた実機バグ。

class TestSpaceInStatePath:
    def _state(self, tmp_path):
        return (tmp_path / "Application Support" / "vvread", tmp_path / "log")

    def test_pop_succeeds_with_space_in_path(self, tmp_path):
        state, log = self._state(tmp_path)
        r = run_q_state(
            'vvread_queue_submit cli 3 "hello" >/dev/null; '
            'b=$(vvread_queue_pop) && echo "POP=$b" || echo POP_EMPTY; '
            '[ -f "${STATE_DIR}/queue/playing/$b" ] && echo IN_PLAYING',
            state, log,
        )
        assert "POP=" in r.stdout, r.stdout + r.stderr
        assert "POP_EMPTY" not in r.stdout
        assert "IN_PLAYING" in r.stdout

    def test_pop_order_with_space_in_path(self, tmp_path):
        # 複数 entry が古い順に pop される（word-split なら 1 件も pop できない）
        state, log = self._state(tmp_path)
        r = run_q_state(
            'vvread_queue_submit cli 3 "first" >/dev/null; sleep 0.01; '
            'vvread_queue_submit cli 3 "second" >/dev/null; '
            'b1=$(vvread_queue_pop); echo "P1=$(cat "${STATE_DIR}/queue/playing/$b1")"; '
            'b2=$(vvread_queue_pop); echo "P2=$(cat "${STATE_DIR}/queue/playing/$b2")"',
            state, log,
        )
        assert "P1=first" in r.stdout, r.stdout + r.stderr
        assert "P2=second" in r.stdout

    def test_failed_list_with_space_in_path(self, tmp_path):
        # failed_list も _queue_sorted を回す → 空白パスで一覧が壊れないこと
        state, log = self._state(tmp_path)
        r = run_q_state(
            'mkdir -p "${STATE_DIR}/queue/failed"; '
            'printf "x" > "${STATE_DIR}/queue/failed/1700000000000_1700000000000_9.1.3.cli.r3"; '
            'vvread_queue_failed_list',
            state, log,
        )
        assert "3 cli" in r.stdout, r.stdout + r.stderr

    def test_drop_oldest_auto_with_space_in_path(self, tmp_path):
        # overflow drop（hook/mcp）も _queue_sorted を回す
        state, log = self._state(tmp_path)
        r = run_q_state(
            'vvread_queue_submit hook 3 "auto1" >/dev/null; '
            '_queue_drop_oldest_auto && echo DROPPED || echo NO_DROP',
            state, log,
        )
        assert "DROPPED" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# 破壊的操作の path guard
# ---------------------------------------------------------------------------

class TestPathGuard:
    def test_refuse_rm_outside_queue(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("keep me")
        r = run_q(
            f'_queue_safe_rm "{outside}" && echo RM || echo REFUSED',
            tmp_path,
        )
        assert "REFUSED" in r.stdout
        assert outside.exists()

    def test_refuse_rm_symlink_entry(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("secret")
        r = run_q(
            f'ln -s "{secret}" "${{QDIR}}/pending/evil"; '
            '_queue_safe_rm "${QDIR}/pending/evil" && echo RM || echo REFUSED',
            tmp_path,
        )
        assert "REFUSED" in r.stdout
        assert secret.exists()

    def test_refuse_dotdot_path(self, tmp_path):
        r = run_q(
            '_queue_safe_rm "${QDIR}/pending/../../evil" && echo RM || echo REFUSED',
            tmp_path,
        )
        assert "REFUSED" in r.stdout


# ---------------------------------------------------------------------------
# stop 機構
# ---------------------------------------------------------------------------

class TestStop:
    def test_should_halt_token_match(self, tmp_path):
        r = run_q(
            'printf "TOK1\\n" > "${QDIR}/stop.request"; '
            'vvread_queue_should_halt "${QDIR}" TOK1 && echo HALT || echo NO; '
            'vvread_queue_should_halt "${QDIR}" TOK2 && echo HALT2 || echo NO2',
            tmp_path,
        )
        lines = r.stdout.split()
        assert "HALT" in lines
        assert "NO2" in lines

    def test_clear_stop_removes(self, tmp_path):
        r = run_q(
            'printf "TOK\\n" > "${QDIR}/stop.request"; '
            'vvread_queue_clear_stop "${QDIR}"; '
            '[ -f "${QDIR}/stop.request" ] && echo STILL || echo GONE',
            tmp_path,
        )
        assert "GONE" in r.stdout

    def test_stop_request_live_drainer(self, tmp_path):
        # 生存 drainer（queue.lock を $$ で保持）→ stop.request に token 書込 + pending 削除
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'vvread_queue_submit cli 3 "a"; vvread_queue_submit cli 3 "b"; '
            'vvread_queue_stop_request "${QDIR}"; echo "rc=$?"; '
            'printf "REQ=%s\\n" "$(cat "${QDIR}/stop.request")"; '
            'printf "TOK=%s\\n" "$tok"',
            tmp_path,
        )
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0
        # stop.request == drainer token
        req = [l for l in r.stdout.splitlines() if l.startswith("REQ=")][0][4:]
        tok = [l for l in r.stdout.splitlines() if l.startswith("TOK=")][0][4:]
        assert req == tok and tok != ""

    def test_stop_request_no_drainer_cleans_up(self, tmp_path):
        # drainer 不在 → pending + orphan playing 削除、stop.request は作らない
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_pop >/dev/null; '  # → playing
            'vvread_queue_submit cli 3 "b"; '  # → pending
            'vvread_queue_stop_request "${QDIR}"; echo "rc=$?"; '
            '[ -f "${QDIR}/stop.request" ] && echo STOPREQ || echo NOSTOPREQ',
            tmp_path,
        )
        assert "rc=1" in r.stdout
        assert "NOSTOPREQ" in r.stdout
        assert _queue_count(tmp_path, "pending") == 0
        assert _queue_count(tmp_path, "playing") == 0


# ---------------------------------------------------------------------------
# skip 機構 (B-144)
# ---------------------------------------------------------------------------

class TestSkip:
    def test_skip_request_live_drainer_writes_token_keeps_pending(self, tmp_path):
        # 生存 drainer → skip.request に token 書込・pending は不変（stop との差）
        r = run_q(
            'tok=$(vvread_queue_acquire "${QDIR}/queue.lock"); '
            'vvread_queue_submit cli 3 "a"; vvread_queue_submit cli 3 "b"; '
            'vvread_queue_skip_request "${QDIR}"; echo "rc=$?"; '
            'printf "REQ=%s\\n" "$(cat "${QDIR}/skip.request")"; '
            'printf "TOK=%s\\n" "$tok"',
            tmp_path,
        )
        assert "rc=0" in r.stdout
        # pending は削除されない（stop との決定的差）
        assert _queue_count(tmp_path, "pending") == 2
        req = [l for l in r.stdout.splitlines() if l.startswith("REQ=")][0][4:]
        tok = [l for l in r.stdout.splitlines() if l.startswith("TOK=")][0][4:]
        assert req == tok and tok != ""

    def test_skip_request_no_drainer_returns_1_writes_nothing(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "a"; '
            'vvread_queue_skip_request "${QDIR}"; echo "rc=$?"; '
            '[ -f "${QDIR}/skip.request" ] && echo SKIPREQ || echo NOSKIPREQ',
            tmp_path,
        )
        assert "rc=1" in r.stdout
        assert "NOSKIPREQ" in r.stdout
        assert _queue_count(tmp_path, "pending") == 1  # pending 不変

    def test_should_skip_token_match(self, tmp_path):
        r = run_q(
            'printf "TOK1\\n" > "${QDIR}/skip.request"; '
            'vvread_queue_should_skip "${QDIR}" TOK1 && echo SKIP || echo NO; '
            'vvread_queue_should_skip "${QDIR}" TOK2 && echo SKIP2 || echo NO2',
            tmp_path,
        )
        lines = r.stdout.split()
        assert "SKIP" in lines
        assert "NO2" in lines

    def test_clear_skip_only_on_token_match(self, tmp_path):
        # token 不一致では削除しない（古い drainer が新 token signal を消さない）
        r = run_q(
            'printf "NEWTOK\\n" > "${QDIR}/skip.request"; '
            'vvread_queue_clear_skip "${QDIR}" OLDTOK; '
            '[ -f "${QDIR}/skip.request" ] && echo KEPT || echo GONE; '
            'vvread_queue_clear_skip "${QDIR}" NEWTOK; '
            '[ -f "${QDIR}/skip.request" ] && echo KEPT2 || echo GONE2',
            tmp_path,
        )
        assert "KEPT" in r.stdout.split()
        assert "GONE2" in r.stdout.split()

    def test_stop_clears_stale_skip_request(self, tmp_path):
        # 【FB-1】stop は全停止なので stale skip.request も掃除する
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_pop >/dev/null; '
            'printf "STALE\\n" > "${QDIR}/skip.request"; '
            'vvread_queue_stop_request "${QDIR}" >/dev/null 2>&1; '
            '[ -f "${QDIR}/skip.request" ] && echo KEPT || echo GONE',
            tmp_path,
        )
        assert "GONE" in r.stdout.split()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_counts(self, tmp_path):
        r = run_q(
            'vvread_queue_submit cli 3 "a"; vvread_queue_pop >/dev/null; '  # playing 1
            'vvread_queue_submit cli 3 "b"; '  # pending 1
            'vvread_queue_status',
            tmp_path,
        )
        assert "pending: 1" in r.stdout
        assert "playing: 1" in r.stdout
        assert "failed: 0" in r.stdout
        assert "mode: off" in r.stdout

    def test_status_mode_on(self, tmp_path):
        r = run_q(
            'touch "${STATE_DIR}/queue_mode"; vvread_queue_status',
            tmp_path,
        )
        assert "mode: on" in r.stdout


# ---------------------------------------------------------------------------
# failed entry 管理 (B-145)
# ---------------------------------------------------------------------------

class TestFailedManagement:
    def test_entry_field_new_format(self, tmp_path):
        # 新形式: failed_ms_created_ms_pid.nonce.speaker.source.rN
        r = run_q(
            'b="1700000009999_1700000000000_42.55.3.mcp.r3"; '
            'echo "FMS=$(vvread_queue_entry_field "$b" failed_ms)"; '
            'echo "CMS=$(vvread_queue_entry_field "$b" created_ms)"; '
            'echo "SPK=$(vvread_queue_entry_field "$b" speaker)"; '
            'echo "SRC=$(vvread_queue_entry_field "$b" source)"; '
            'echo "RET=$(vvread_queue_entry_field "$b" retry)"',
            tmp_path,
        )
        assert "FMS=1700000009999" in r.stdout
        assert "CMS=1700000000000" in r.stdout
        assert "SPK=3" in r.stdout
        assert "SRC=mcp" in r.stdout
        assert "RET=3" in r.stdout

    def test_entry_field_legacy_format(self, tmp_path):
        # 旧形式: created_ms_pid.nonce.speaker.source.rN（failed_ms 空）
        r = run_q(
            'b="1700000000000_42.55.3.cli.r3"; '
            'echo "FMS=[$(vvread_queue_entry_field "$b" failed_ms)]"; '
            'echo "CMS=$(vvread_queue_entry_field "$b" created_ms)"',
            tmp_path,
        )
        assert "FMS=[]" in r.stdout
        assert "CMS=1700000000000" in r.stdout

    def test_failed_list_new_and_legacy(self, tmp_path):
        r = run_q(
            'printf x > "${QDIR}/failed/1700000009999_1700000000000_1.55.3.mcp.r3"; '
            'printf x > "${QDIR}/failed/1690000000000_1.55.3.cli.r3"; '
            'vvread_queue_failed_list',
            tmp_path,
        )
        # 新形式は failed_ms 先頭、旧形式は legacy ラベル
        assert "1700000009999 1700000000000 3 mcp 3" in r.stdout
        assert "legacy 1690000000000 3 cli 3" in r.stdout

    def test_failed_clear_removes_all(self, tmp_path):
        r = run_q(
            'printf x > "${QDIR}/failed/1700000009999_1700000000000_1.55.3.mcp.r3"; '
            'printf x > "${QDIR}/failed/1690000000000_1.55.3.cli.r3"; '
            'vvread_queue_failed_clear; echo done',
            tmp_path,
        )
        assert "done" in r.stdout
        assert _queue_count(tmp_path, "failed") == 0

    def test_failed_cleanup_ttl_by_failure_time(self, tmp_path):
        # 古い failed_ms（1700000000000）+ 近傍 failed_ms（_now_ms）→ 古のみ削除
        r = run_q(
            'printf x > "${QDIR}/failed/1700000000000_1700000000000_1.55.3.mcp.r3"; '
            'fresh="$(_now_ms)"; '
            'printf x > "${QDIR}/failed/${fresh}_1700000000000_1.55.3.mcp.r3"; '
            'removed=$(vvread_queue_failed_cleanup "${QDIR}" 86400000); '
            'echo "REMOVED=${removed}"',
            tmp_path,
        )
        assert "REMOVED=1" in r.stdout
        assert _queue_count(tmp_path, "failed") == 1  # 新しい方が残る

    def test_failed_cleanup_skips_legacy(self, tmp_path):
        r = run_q(
            'printf x > "${QDIR}/failed/1700000000000_1.55.3.cli.r3"; '  # legacy
            'removed=$(vvread_queue_failed_cleanup "${QDIR}" 1); '
            'echo "REMOVED=${removed}"',
            tmp_path,
        )
        assert "REMOVED=0" in r.stdout
        assert _queue_count(tmp_path, "failed") == 1  # legacy は削除されない

    def test_failed_cleanup_invalid_ttl(self, tmp_path):
        r = run_q('vvread_queue_failed_cleanup "${QDIR}" abc; echo "rc=$?"', tmp_path)
        assert "rc=2" in r.stdout

    def test_failed_rm_valid(self, tmp_path):
        r = run_q(
            'printf x > "${QDIR}/failed/1700000009999_1700000000000_1.55.3.mcp.r3"; '
            'vvread_queue_failed_rm "${QDIR}" "1700000009999_1700000000000_1.55.3.mcp.r3"; '
            'echo "rc=$?"',
            tmp_path,
        )
        assert "rc=0" in r.stdout
        assert _queue_count(tmp_path, "failed") == 0

    def test_failed_rm_rejects_path_traversal(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("x")
        r = run_q(
            'vvread_queue_failed_rm "${QDIR}" "../../secret"; echo "rc=$?"',
            tmp_path,
        )
        assert "rc=2" in r.stdout
        assert secret.exists()

    def test_failed_rm_rejects_slash(self, tmp_path):
        r = run_q('vvread_queue_failed_rm "${QDIR}" "a/b"; echo "rc=$?"', tmp_path)
        assert "rc=2" in r.stdout

    def test_failed_rm_missing_returns_1(self, tmp_path):
        r = run_q('vvread_queue_failed_rm "${QDIR}" "nonexistent.r3"; echo "rc=$?"', tmp_path)
        assert "rc=1" in r.stdout


class TestFailedMax:
    def test_recover_orphan_writes_new_format_with_failed_ms(self, tmp_path):
        # retry 上限超の playing → failed へ failure 時刻付き（新形式）で退避
        r = run_q(
            'printf x > "${QDIR}/playing/100_1.55.3.cli.r2"; '
            'vvread_queue_recover_orphans; '
            'for f in "${QDIR}/failed"/*; do b=$(basename "$f"); '
            '  echo "FMT=$(_queue_failed_format "$b")"; '
            '  echo "FMS=$(vvread_queue_entry_field "$b" failed_ms)"; done',
            tmp_path,
        )
        assert "FMT=new" in r.stdout
        # failed_ms は数値
        fms_line = [l for l in r.stdout.splitlines() if l.startswith("FMS=")][0][4:]
        assert fms_line.isdigit() and len(fms_line) >= 12

    def test_failed_max_drops_oldest_new(self, tmp_path):
        # FAILED_MAX=1、既存 new failed 1 件 + retry 超 playing → 最古 drop で 1 件維持
        r = run_q(
            'printf x > "${QDIR}/failed/1600000000000_1600000000000_1.55.3.mcp.r3"; '  # 古い new
            'printf x > "${QDIR}/playing/100_1.55.3.cli.r2"; '
            'vvread_queue_recover_orphans; echo done',
            tmp_path, env_extra={"VVREAD_QUEUE_FAILED_MAX": "1"},
        )
        assert "done" in r.stdout
        assert _queue_count(tmp_path, "failed") == 1
        # 残るのは新しく退避した方（古い 1600... は drop 済み）
        remaining = [f.name for f in (_qdir(tmp_path) / "failed").iterdir()]
        assert not any(n.startswith("1600000000000_") for n in remaining), remaining

    def test_failed_max_does_not_drop_legacy(self, tmp_path):
        # FAILED_MAX=1、既存 legacy 1 件 → legacy は自動 drop 対象外なので両方残る
        r = run_q(
            'printf x > "${QDIR}/failed/1600000000000_1.55.3.cli.r3"; '  # legacy
            'printf x > "${QDIR}/playing/100_1.55.3.cli.r2"; '
            'vvread_queue_recover_orphans; echo done',
            tmp_path, env_extra={"VVREAD_QUEUE_FAILED_MAX": "1"},
        )
        assert "done" in r.stdout
        # legacy は drop されないので 2 件（legacy + 新規退避）
        assert _queue_count(tmp_path, "failed") == 2

    def test_failed_max_zero_falls_back_to_32(self, tmp_path):
        # FAILED_MAX=0 は 32 へ fallback → 数件では drop しない
        r = run_q(
            'printf x > "${QDIR}/failed/1600000000000_1600000000000_1.55.3.mcp.r3"; '
            'printf x > "${QDIR}/failed/1600000000001_1600000000000_1.55.3.mcp.r3"; '
            'printf x > "${QDIR}/playing/100_1.55.3.cli.r2"; '
            'vvread_queue_recover_orphans; echo done',
            tmp_path, env_extra={"VVREAD_QUEUE_FAILED_MAX": "0"},
        )
        assert "done" in r.stdout
        assert _queue_count(tmp_path, "failed") == 3  # drop されない


# ---------------------------------------------------------------------------
# python 側ヘルパ
# ---------------------------------------------------------------------------

def _queue_count(tmp_path: Path, sub: str) -> int:
    d = _qdir(tmp_path) / sub
    if not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir()
               if f.is_file() and not f.is_symlink() and ".tmp." not in f.name)


def _entry_sources(tmp_path: Path, sub: str):
    d = _qdir(tmp_path) / sub
    out = []
    for f in sorted(d.iterdir()):
        if not f.is_file() or f.is_symlink() or ".tmp." in f.name:
            continue
        # name: ms_pid.nonce.speaker.source.rN
        parts = f.name.split(".")
        out.append(parts[-2])  # source
    return out
