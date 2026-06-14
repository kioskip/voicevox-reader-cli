"""tests/test_receiver_install.py — receiver_install / mcp_tools_install tests (B-148/B-149)"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import receiver_install as ri  # noqa: E402
import mcp_tools_install as mi  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_runner(returncode=0, stdout="", stderr=""):
    def runner(cmd, **kwargs):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return runner


def _call_tracking_runner(calls, responses=None):
    """calls リストに (cmd, kwargs) を記録し、responses で返り値を制御する。

    responses: list of (returncode, stdout, stderr) — 呼び出し順に使用。
    最後のエントリを繰り返す。
    """
    idx = [0]
    def runner(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if responses:
            i = min(idx[0], len(responses) - 1)
            rc, out, err = responses[i]
            idx[0] += 1
        else:
            rc, out, err = 0, "", ""
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    return runner


# ===========================================================================
# mcp_tools_install
# ===========================================================================

class TestCheckMcpToolsRegistration:
    def test_not_registered_when_get_fails(self):
        r = mi.check_mcp_tools_registration(_fake_runner(returncode=1))
        assert r == "not_registered"

    def test_registered_local_when_output_contains_local(self):
        r = mi.check_mcp_tools_registration(_fake_runner(returncode=0, stdout="scope: local"))
        assert r == "registered_local"

    def test_conflicting_when_registered_without_local(self):
        r = mi.check_mcp_tools_registration(_fake_runner(returncode=0, stdout="scope: project"))
        assert r == "conflicting_non_local"

    def test_oserror_returns_not_registered(self):
        def bad_runner(cmd, **kwargs):
            raise OSError("not found")
        r = mi.check_mcp_tools_registration(bad_runner)
        assert r == "not_registered"


class TestRegisterMcpTools:
    def test_dry_run_returns_true_without_runner(self, tmp_path):
        fake_repo = tmp_path / "repo"
        (fake_repo / "bin").mkdir(parents=True)
        r = mi.register_mcp_tools(fake_repo, dry_run=True, runner=_fake_runner(returncode=0))
        assert r is True

    def test_registers_with_scope_local_and_absolute_path(self, tmp_path):
        fake_repo = tmp_path / "repo"
        (fake_repo / "bin").mkdir(parents=True)
        calls = []
        runner = _call_tracking_runner(calls, [(0, "", "")])
        result = mi.register_mcp_tools(fake_repo, dry_run=False, runner=runner)
        assert result is True
        assert calls, "runner が呼ばれなかった"
        cmd = calls[0][0]
        assert cmd[:3] == ["claude", "mcp", "add"]
        assert "--scope" in cmd and "local" in cmd
        assert str(fake_repo / "bin" / "vvread") in cmd
        assert "mcp" in cmd
        assert "stdio" in cmd

    def test_returns_false_on_nonzero_exit(self, tmp_path, capsys):
        fake_repo = tmp_path / "repo"
        (fake_repo / "bin").mkdir(parents=True)
        result = mi.register_mcp_tools(fake_repo, dry_run=False, runner=_fake_runner(returncode=1))
        assert result is False


# ===========================================================================
# receiver_install
# ===========================================================================

class TestCheckBun:
    def test_returns_true_when_bun_found(self, monkeypatch):
        monkeypatch.setattr(ri.shutil, "which", lambda cmd: "/usr/local/bin/bun")
        assert ri.check_bun() is True

    def test_returns_false_when_bun_not_found(self, monkeypatch):
        monkeypatch.setattr(ri.shutil, "which", lambda cmd: None)
        assert ri.check_bun() is False


class TestEnsureReceiverDependencies:
    def test_already_installed_no_runner_call(self, tmp_path):
        receiver_dir = tmp_path / "receiver"
        sdk_path = receiver_dir / "node_modules" / "@modelcontextprotocol" / "sdk"
        sdk_path.mkdir(parents=True)
        calls = []
        runner = _call_tracking_runner(calls)
        result = ri.ensure_receiver_dependencies(receiver_dir, dry_run=False, runner=runner)
        assert result is True
        assert calls == [], "既インストール時にrunnerを呼ばないこと"

    def test_dry_run_returns_true_without_install(self, tmp_path):
        receiver_dir = tmp_path / "receiver"
        receiver_dir.mkdir()
        calls = []
        runner = _call_tracking_runner(calls)
        result = ri.ensure_receiver_dependencies(receiver_dir, dry_run=True, runner=runner)
        assert result is True
        assert calls == [], "dry_run でrunnerを呼ばないこと"

    def test_installs_when_sdk_missing(self, tmp_path):
        receiver_dir = tmp_path / "receiver"
        receiver_dir.mkdir()
        calls = []
        runner = _call_tracking_runner(calls, [(0, "", "")])
        result = ri.ensure_receiver_dependencies(receiver_dir, dry_run=False, runner=runner)
        assert result is True
        assert calls, "bun install が呼ばれなかった"
        cmd = calls[0][0]
        assert cmd[:2] == ["bun", "install"]
        assert "--frozen-lockfile" in cmd

    def test_returns_false_on_bun_install_failure(self, tmp_path):
        receiver_dir = tmp_path / "receiver"
        receiver_dir.mkdir()
        result = ri.ensure_receiver_dependencies(
            receiver_dir, dry_run=False, runner=_fake_runner(returncode=1)
        )
        assert result is False


class TestGetReceiverRegistrationStatus:
    def test_not_registered_when_get_fails(self):
        r = ri.get_receiver_registration_status(_fake_runner(returncode=1))
        assert r == "not_registered"

    def test_registered_local_when_output_contains_local(self):
        r = ri.get_receiver_registration_status(_fake_runner(returncode=0, stdout="scope: local"))
        assert r == "registered_local"

    def test_conflicting_when_no_local_in_output(self):
        r = ri.get_receiver_registration_status(_fake_runner(returncode=0, stdout="scope: project"))
        assert r == "conflicting_non_local"

    def test_oserror_returns_not_registered(self):
        def bad_runner(cmd, **kwargs):
            raise OSError("not found")
        r = ri.get_receiver_registration_status(bad_runner)
        assert r == "not_registered"


class TestRegisterReceiverMcp:
    def test_no_op_when_already_registered_local(self, tmp_path):
        fake_repo = tmp_path / "repo"
        calls = []
        runner = _call_tracking_runner(calls, [(0, "scope: local", "")])
        result = ri.register_receiver_mcp(fake_repo, dry_run=False, runner=runner)
        assert result is True
        add_cmds = [c for c, _ in calls if c and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds == [], "既登録時に mcp add を呼ばないこと"

    def test_returns_false_when_conflicting_non_local(self, tmp_path, capsys):
        fake_repo = tmp_path / "repo"
        runner = _fake_runner(returncode=0, stdout="scope: project")
        result = ri.register_receiver_mcp(fake_repo, dry_run=False, runner=runner)
        assert result is False

    def test_dry_run_returns_true_without_runner_call(self, tmp_path):
        fake_repo = tmp_path / "repo"
        calls = []
        # mcp get returns not_registered → dry_run skips mcp add
        runner = _call_tracking_runner(calls, [(1, "", "")])
        result = ri.register_receiver_mcp(fake_repo, dry_run=True, runner=runner)
        assert result is True
        add_cmds = [c for c, _ in calls if c and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds == [], "dry_run で mcp add を呼ばないこと"

    def test_registers_with_scope_local_and_absolute_bun_path(self, tmp_path):
        fake_repo = tmp_path / "repo"
        calls = []
        # first call: mcp get → not_registered; second call: mcp add → OK
        runner = _call_tracking_runner(calls, [(1, "", ""), (0, "", "")])
        result = ri.register_receiver_mcp(fake_repo, dry_run=False, runner=runner)
        assert result is True
        add_cmds = [c for c, _ in calls if c and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds, "claude mcp add が呼ばれなかった"
        cmd = add_cmds[0]
        assert "--scope" in cmd and "local" in cmd
        assert "vvread-receiver" in cmd
        assert "bun" in cmd
        assert str(fake_repo / "receiver" / "server.ts") in cmd
        assert "stdio" in cmd

    def test_does_not_modify_mcp_json(self, tmp_path):
        """register_receiver_mcp は .mcp.json を変更しない"""
        fake_repo = tmp_path / "repo"
        mcp_json = fake_repo / ".mcp.json"
        mcp_json.parent.mkdir(parents=True, exist_ok=True)
        mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
        calls = []
        runner = _call_tracking_runner(calls, [(1, "", ""), (0, "", "")])
        ri.register_receiver_mcp(fake_repo, dry_run=False, runner=runner)
        assert mcp_json.read_text(encoding="utf-8") == '{"mcpServers": {}}'

    def test_returns_false_on_mcp_add_failure(self, tmp_path):
        fake_repo = tmp_path / "repo"
        calls = []
        runner = _call_tracking_runner(calls, [(1, "", ""), (1, "", "error")])
        result = ri.register_receiver_mcp(fake_repo, dry_run=False, runner=runner)
        assert result is False
