"""scripts/cmd/queue.sh のテスト (B-015 WS-A2)

vvread queue on/off/status/clear の制御プレーン。
bin/vvread queue ... 経由で dispatch されることも確認する。
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_QUEUE = REPO / "scripts" / "cmd" / "queue.sh"


def _env(tmp_path: Path) -> dict:
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    base.update({
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
    })
    return base


def run_queue(*args, tmp_path, via_bin=False, timeout=30):
    cmd = [str(VVREAD), "queue", *args] if via_bin else [str(CMD_QUEUE), *args]
    return subprocess.run(cmd, env=_env(tmp_path), capture_output=True,
                          text=True, timeout=timeout)


def _qdir(tmp_path: Path) -> Path:
    return tmp_path / "state" / "queue"


def _make_pending(tmp_path: Path, name="100_1.55.3.cli.r0", body="x"):
    p = _qdir(tmp_path) / "pending"
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(body)


class TestOnOff:
    def test_on_creates_flag(self, tmp_path):
        r = run_queue("on", tmp_path=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "queue mode: on" in r.stdout
        assert (tmp_path / "state" / "queue_mode").is_file()

    def test_off_when_empty(self, tmp_path):
        run_queue("on", tmp_path=tmp_path)
        r = run_queue("off", tmp_path=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "queue mode: off" in r.stdout
        assert not (tmp_path / "state" / "queue_mode").exists()

    def test_off_rejected_when_pending(self, tmp_path):
        run_queue("on", tmp_path=tmp_path)  # inits dirs + flag
        _make_pending(tmp_path)
        r = run_queue("off", tmp_path=tmp_path)
        assert r.returncode == 1
        assert "queue is not empty" in r.stderr
        # flag remains
        assert (tmp_path / "state" / "queue_mode").is_file()


class TestStatusClear:
    def test_status_default(self, tmp_path):
        r = run_queue("status", tmp_path=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "mode: off" in r.stdout
        assert "pending: 0" in r.stdout
        assert "playing: 0" in r.stdout
        assert "failed: 0" in r.stdout

    def test_status_with_pending(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)  # init dirs
        _make_pending(tmp_path)
        r = run_queue("status", tmp_path=tmp_path)
        assert "pending: 1" in r.stdout

    def test_clear_removes_pending(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        _make_pending(tmp_path)
        r = run_queue("clear", tmp_path=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "queue cleared" in r.stdout
        remaining = list((_qdir(tmp_path) / "pending").glob("*"))
        assert remaining == []


class TestSkip:
    def test_skip_nothing_playing(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)  # init dirs
        r = run_queue("skip", tmp_path=tmp_path)
        assert r.returncode == 0
        assert "nothing playing" in r.stdout

    def test_skip_playing_but_no_live_drainer(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        # playing にエントリはあるが drainer プロセスは存在しない（orphan）
        pl = _qdir(tmp_path) / "playing"
        pl.mkdir(parents=True, exist_ok=True)
        (pl / "100_1.55.3.cli.r0").write_text("x")
        r = run_queue("skip", tmp_path=tmp_path)
        assert r.returncode == 0
        assert "nothing playing" in r.stdout


def _make_failed(tmp_path, name="1700000009999_1700000000000_1.55.3.mcp.r3", body="x"):
    d = _qdir(tmp_path) / "failed"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)
    return name


class TestFailed:
    def test_list_empty(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "list", tmp_path=tmp_path)
        assert r.returncode == 0
        assert "no failed entries" in r.stdout

    def test_list_with_entries(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        _make_failed(tmp_path)
        r = run_queue("failed", "list", tmp_path=tmp_path)
        assert r.returncode == 0
        assert "FAILED_MS" in r.stdout
        assert "1700000009999" in r.stdout

    def test_rm_valid(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        name = _make_failed(tmp_path)
        r = run_queue("failed", "rm", name, tmp_path=tmp_path)
        assert r.returncode == 0
        assert "removed" in r.stdout
        assert list((_qdir(tmp_path) / "failed").glob("*")) == []

    def test_rm_no_arg_exit_2(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "rm", tmp_path=tmp_path)
        assert r.returncode == 2

    def test_rm_invalid_name_exit_2(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "rm", "../../etc", tmp_path=tmp_path)
        assert r.returncode == 2

    def test_rm_not_found_exit_1(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "rm", "nope.r3", tmp_path=tmp_path)
        assert r.returncode == 1

    def test_clear(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        _make_failed(tmp_path)
        _make_failed(tmp_path, name="1700000008888_1700000000000_2.55.3.cli.r3")
        r = run_queue("failed", "clear", tmp_path=tmp_path)
        assert r.returncode == 0
        assert "failed cleared" in r.stdout
        assert list((_qdir(tmp_path) / "failed").glob("*")) == []

    def test_cleanup_ttl(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        # 非常に古い failed_ms → TTL 1s で削除される
        _make_failed(tmp_path, name="1600000000000_1600000000000_1.55.3.mcp.r3")
        r = run_queue("failed", "cleanup", "--ttl", "1s", tmp_path=tmp_path)
        assert r.returncode == 0, r.stderr
        assert "removed 1" in r.stdout

    def test_cleanup_invalid_ttl_exit_2(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "cleanup", "--ttl", "abc", tmp_path=tmp_path)
        assert r.returncode == 2

    def test_failed_unknown_sub_exit_2(self, tmp_path):
        run_queue("status", tmp_path=tmp_path)
        r = run_queue("failed", "bogus", tmp_path=tmp_path)
        assert r.returncode == 2


class TestDispatch:
    def test_via_bin_vvread(self, tmp_path):
        r = run_queue("on", tmp_path=tmp_path, via_bin=True)
        assert r.returncode == 0, r.stderr
        assert "queue mode: on" in r.stdout

    def test_unknown_sub_exit_2(self, tmp_path):
        r = run_queue("bogus", tmp_path=tmp_path)
        assert r.returncode == 2
        assert "unknown subcommand" in r.stderr

    def test_no_sub_usage_exit_2(self, tmp_path):
        r = run_queue(tmp_path=tmp_path)
        assert r.returncode == 2
        assert "Usage: vvread queue" in r.stderr
