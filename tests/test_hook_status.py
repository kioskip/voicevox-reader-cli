"""scripts/hook_status.py のテスト (F-112)

get_vvread_hook_status / _get_scope_hook_status の単体テスト。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import hook_status as hs  # noqa: E402


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _write_claude_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _modern_hook_block(command: str = "vvread on-stop") -> dict:
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _legacy_hook_block() -> dict:
    return {"matcher": "", "hooks": [{"type": "command", "command": "/path/scripts/on_stop.sh"}]}


# ---------------------------------------------------------------------------
# is_voiceclaude_hook
# ---------------------------------------------------------------------------


class TestIsVoiceclaudeHook:
    def test_vvread_on_stop(self):
        assert hs.is_voiceclaude_hook("vvread on-stop")

    def test_vvread_tab_on_stop(self):
        assert hs.is_voiceclaude_hook("vvread\ton-stop")

    def test_absolute_bin_vvread(self):
        assert hs.is_voiceclaude_hook("/home/user/repo/bin/vvread on-stop")

    def test_on_stop_sh_legacy(self):
        assert hs.is_voiceclaude_hook("/path/to/scripts/on_stop.sh")

    def test_on_stop_sh_partial_legacy(self):
        assert hs.is_voiceclaude_hook("/on_stop.sh")

    def test_unrelated_command(self):
        assert not hs.is_voiceclaude_hook("echo hello")

    def test_non_string(self):
        assert not hs.is_voiceclaude_hook(None)


# ---------------------------------------------------------------------------
# resolve_settings_path
# ---------------------------------------------------------------------------


class TestResolveSettingsPath:
    def test_project_local(self, tmp_path):
        p = hs.resolve_settings_path("project-local", cwd=tmp_path, home=Path("/home/u"))
        assert p == tmp_path / ".claude" / "settings.local.json"

    def test_project(self, tmp_path):
        p = hs.resolve_settings_path("project", cwd=tmp_path, home=Path("/home/u"))
        assert p == tmp_path / ".claude" / "settings.json"

    def test_user(self, tmp_path):
        p = hs.resolve_settings_path("user", cwd=tmp_path, home=tmp_path)
        assert p == tmp_path / ".claude" / "settings.json"

    def test_unknown_scope_raises(self, tmp_path):
        with pytest.raises(ValueError):
            hs.resolve_settings_path("global", cwd=tmp_path, home=tmp_path)


# ---------------------------------------------------------------------------
# _get_scope_hook_status
# ---------------------------------------------------------------------------


class TestGetScopeHookStatus:
    def test_no_file_returns_none(self, tmp_path):
        assert hs._get_scope_hook_status("project", tmp_path, tmp_path) == "none"

    def test_modern_hook_registered(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {"hooks": {"Stop": [_modern_hook_block()]}})
        assert hs._get_scope_hook_status("project", tmp_path, tmp_path) == "registered"

    def test_legacy_hook(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {"hooks": {"Stop": [_legacy_hook_block()]}})
        assert hs._get_scope_hook_status("project", tmp_path, tmp_path) == "legacy"

    def test_no_hooks_in_file(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {"other": "data"})
        assert hs._get_scope_hook_status("project", tmp_path, tmp_path) == "none"

    def test_modern_and_legacy_same_scope_returns_legacy(self, tmp_path):
        """同一 scope に modern + legacy 混在 → per-scope は "legacy" を返す"""
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {
            "hooks": {"Stop": [_modern_hook_block(), _legacy_hook_block()]}
        })
        assert hs._get_scope_hook_status("project", tmp_path, tmp_path) == "legacy"


# ---------------------------------------------------------------------------
# get_vvread_hook_status（集約）
# ---------------------------------------------------------------------------


class TestGetVvreadHookStatus:
    def test_none_when_no_hooks(self, tmp_path):
        assert hs.get_vvread_hook_status(tmp_path, home=tmp_path) == "none"

    def test_modern_when_project_has_modern(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {"hooks": {"Stop": [_modern_hook_block()]}})
        assert hs.get_vvread_hook_status(tmp_path, home=tmp_path) == "modern"

    def test_modern_when_project_local_has_modern(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.local.json"
        _write_claude_settings(settings, {"hooks": {"Stop": [_modern_hook_block()]}})
        assert hs.get_vvread_hook_status(tmp_path, home=tmp_path) == "modern"

    def test_legacy_when_only_legacy(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(settings, {"hooks": {"Stop": [_legacy_hook_block()]}})
        assert hs.get_vvread_hook_status(tmp_path, home=tmp_path) == "legacy"

    def test_modern_wins_over_legacy_cross_scope(self, tmp_path):
        # クロス scope: project に modern、user に legacy → "modern"
        # project: modern
        proj = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(proj, {"hooks": {"Stop": [_modern_hook_block()]}})
        # user (home): legacy
        home = tmp_path / "home"
        user = home / ".claude" / "settings.json"
        _write_claude_settings(user, {"hooks": {"Stop": [_legacy_hook_block()]}})
        assert hs.get_vvread_hook_status(tmp_path, home=home) == "modern"

    def test_modern_wins_over_legacy_same_scope(self, tmp_path):
        """同一 scope に modern + legacy 混在でも、他 scope に modern があれば "modern"
        ※ per-scope は "legacy" だが、別 scope の modern が集約で勝つ"""
        # project-local: modern
        pl = tmp_path / ".claude" / "settings.local.json"
        _write_claude_settings(pl, {"hooks": {"Stop": [_modern_hook_block()]}})
        # project: modern + legacy 混在
        proj = tmp_path / ".claude" / "settings.json"
        _write_claude_settings(proj, {
            "hooks": {"Stop": [_modern_hook_block(), _legacy_hook_block()]}
        })
        assert hs.get_vvread_hook_status(tmp_path, home=tmp_path) == "modern"
