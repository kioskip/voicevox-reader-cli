"""scripts/cmd_install.sh / cmd_uninstall.sh + bin/vvread 統合テスト (R-008)

hook_install.py 単体は test_hook_install.py で網羅。本テストは bash ラッパー
+ bin/vvread dispatch + CLI フラグ + 終了コード仕様(R-008 ユーザ指定)に集中。

ユーザ仕様の終了コード:
  0 = 成功(変更なしも成功)
  1 = 実行エラー(JSON 破損 / scope 不正 / legacy 検出 / 書込不可)
  2 = 使い方エラー(不正オプション、argparse default)

DI: VVREAD_PROJECT_DIR を tmp_path 配下に向け、subprocess の cwd / HOME も
分離。本物の repo の .claude/settings.json は触らない。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_INSTALL = REPO / "scripts" / "cmd" / "install.sh"
CMD_UNINSTALL = REPO / "scripts" / "cmd" / "uninstall.sh"


def _clean_env(env_extra=None) -> dict:
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def _setup_env(tmp_path: Path) -> tuple[Path, Path, dict]:
    """tmp_path 内に cwd / home / fake_repo を作って env を返す。"""
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    fake_repo = tmp_path / "fake_repo"
    cwd.mkdir()
    home.mkdir()
    fake_repo.mkdir()
    (fake_repo / "bin").mkdir()
    # bin/vvread の dummy(install で repo_root の参照のみ、実行はしない)
    (fake_repo / "bin" / "vvread").write_text("#!/bin/bash\nexit 0\n")
    (fake_repo / "bin" / "vvread").chmod(0o755)

    env = {
        "HOME": str(home),
        # VVREAD_PROJECT_DIR が hook_install.py 内で repo_root として使われる
        "VVREAD_PROJECT_DIR": str(fake_repo),
        # cmd_install.sh が VVREAD_SCRIPTS_DIR を default 解決するときに正しい
        # scripts/ を指すよう本物の repo の scripts/ を渡す
        "VVREAD_SCRIPTS_DIR": str(REPO / "scripts"),
    }
    return cwd, home, env


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_install(*args, env_extra=None, cwd=None, timeout=10):
    return subprocess.run(
        [str(CMD_INSTALL), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_uninstall(*args, env_extra=None, cwd=None, timeout=10):
    return subprocess.run(
        [str(CMD_UNINSTALL), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_vvread_install(*args, env_extra=None, cwd=None, timeout=10):
    return subprocess.run(
        [str(VVREAD), "install", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_vvread_uninstall(*args, env_extra=None, cwd=None, timeout=10):
    return subprocess.run(
        [str(VVREAD), "uninstall", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# install: 終了コード仕様
# ---------------------------------------------------------------------------


class TestInstallExitCodes:
    def test_install_new_file_exits_0(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0, f"stderr={r.stderr}"
        # default scope = project → settings.local.json
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_install_already_present_exits_0(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        # 1 回目で登録
        r1 = run_install("--yes", env_extra=env, cwd=cwd)
        assert r1.returncode == 0
        # 2 回目: skip(変更なし)で 0
        r2 = run_install("--yes", env_extra=env, cwd=cwd)
        assert r2.returncode == 0
        assert "already" in r2.stdout

    def test_install_broken_json_exits_1(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True)
        target.write_text("{ broken json", encoding="utf-8")
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 1
        assert "invalid JSON" in r.stderr

    def test_install_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--bogus", env_extra=env, cwd=cwd)
        assert r.returncode == 2

    def test_install_unknown_scope_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--scope", "weird", env_extra=env, cwd=cwd)
        assert r.returncode == 2  # argparse choices で reject


# ---------------------------------------------------------------------------
# install: scope ごとの動作
# ---------------------------------------------------------------------------


class TestInstallScopes:
    def test_default_is_project_local(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.local.json").exists()
        assert not (cwd / ".claude" / "settings.json").exists()
        assert not (home / ".claude" / "settings.json").exists()

    def test_project_scope_writes_to_settings_json(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", "--scope", "project", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.json").exists()
        assert not (cwd / ".claude" / "settings.local.json").exists()

    def test_user_scope_writes_to_home(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", "--scope", "user", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (home / ".claude" / "settings.json").exists()
        assert not (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# install: dry-run / yes
# ---------------------------------------------------------------------------


class TestInstallDryRun:
    def test_dry_run_does_not_create_file(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--dry-run", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert "[dry-run]" in r.stdout
        assert not (cwd / ".claude" / "settings.local.json").exists()


class TestInstallYesPlaceholder:
    def test_yes_flag_accepted(self, tmp_path):
        """--yes は v0.1 では受理のみ。exit 0 で動く"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# install: 既存設定 merge
# ---------------------------------------------------------------------------


class TestInstallMergeIntegration:
    def test_existing_keys_preserved(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "permissions": {"allow": ["Bash(*)"]},
            "model": "opus",
        })
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        data = _read_settings(target)
        assert data["permissions"] == {"allow": ["Bash(*)"]}
        assert data["model"] == "opus"
        assert "hooks" in data

    def test_existing_other_hooks_kept(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {"Stop": [
                {"matcher": "", "hooks": [{
                    "type": "command", "command": "/usr/local/bin/other_tool",
                }]},
            ]}
        })
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        data = _read_settings(target)
        cmds = []
        for block in data["hooks"]["Stop"]:
            for h in block["hooks"]:
                cmds.append(h["command"])
        assert "/usr/local/bin/other_tool" in cmds
        assert any("vvread" in c and "on-stop" in c for c in cmds)


# ---------------------------------------------------------------------------
# install: .bak
# ---------------------------------------------------------------------------


class TestInstallBackup:
    def test_bak_created_when_existing(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {"model": "opus"})
        original = target.read_text(encoding="utf-8")
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        bak = target.with_suffix(target.suffix + ".bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# install: 空白パス
# ---------------------------------------------------------------------------


class TestInstallSpaceInPath:
    def test_install_with_space_in_repo_path(self, tmp_path):
        # tmp_path 内に空白を含む repo を作る
        space_root = tmp_path / "with space"
        space_root.mkdir()
        cwd = space_root / "proj"
        home = space_root / "home"
        fake_repo = space_root / "repo"
        for d in (cwd, home, fake_repo / "bin"):
            d.mkdir(parents=True)
        (fake_repo / "bin" / "vvread").write_text("#!/bin/bash\nexit 0\n")
        (fake_repo / "bin" / "vvread").chmod(0o755)

        env = {
            "HOME": str(home),
            "VVREAD_PROJECT_DIR": str(fake_repo),
            "VVREAD_SCRIPTS_DIR": str(REPO / "scripts"),
        }
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        target = cwd / ".claude" / "settings.local.json"
        data = _read_settings(target)
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        # ダブルクォートで囲まれて on-stop が末尾
        assert cmd.startswith('"')
        assert cmd.endswith(' on-stop')
        # shlex で 2 引数に分割可能
        import shlex
        parts = shlex.split(cmd)
        assert parts == [str(fake_repo / "bin" / "vvread"), "on-stop"]


# ---------------------------------------------------------------------------
# uninstall: 終了コード + 動作
# ---------------------------------------------------------------------------


class TestUninstallExitCodes:
    def test_uninstall_no_file_exits_0(self, tmp_path):
        """ファイル不在は変更なしで成功"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_uninstall(env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert "no" in r.stdout.lower()

    def test_uninstall_existing_voiceclaude_exits_0(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        # 先に install して状態を作る
        r0 = run_install("--yes", env_extra=env, cwd=cwd)
        assert r0.returncode == 0
        r1 = run_uninstall(env_extra=env, cwd=cwd)
        assert r1.returncode == 0
        # 2 回目: 既に削除済み → 変更なしで 0
        r2 = run_uninstall(env_extra=env, cwd=cwd)
        assert r2.returncode == 0

    def test_uninstall_broken_json_exits_1(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True)
        target.write_text("{ broken", encoding="utf-8")
        r = run_uninstall(env_extra=env, cwd=cwd)
        assert r.returncode == 1
        assert "invalid JSON" in r.stderr

    def test_uninstall_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_uninstall("--bogus", env_extra=env, cwd=cwd)
        assert r.returncode == 2

    def test_uninstall_unknown_scope_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_uninstall("--scope", "weird", env_extra=env, cwd=cwd)
        assert r.returncode == 2


class TestUninstallOnlyVoiceClaudeIntegration:
    def test_other_hooks_preserved(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {"Stop": [
                {"matcher": "", "hooks": [{
                    "type": "command",
                    "command": f"{env['VVREAD_PROJECT_DIR']}/bin/vvread on-stop",
                }]},
                {"matcher": "", "hooks": [{
                    "type": "command", "command": "/usr/local/bin/other",
                }]},
            ]}
        })
        r = run_uninstall(env_extra=env, cwd=cwd)
        assert r.returncode == 0
        data = _read_settings(target)
        # 他 hook は残る
        cmds = []
        for block in data["hooks"]["Stop"]:
            for h in block["hooks"]:
                cmds.append(h["command"])
        assert cmds == ["/usr/local/bin/other"]


class TestUninstallDryRunIntegration:
    def test_dry_run_no_change(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        run_install("--yes", env_extra=env, cwd=cwd)
        target = cwd / ".claude" / "settings.local.json"
        original = target.read_text(encoding="utf-8")
        r = run_uninstall("--dry-run", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert "[dry-run]" in r.stdout
        # ファイル変更なし
        assert target.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# bin/vvread dispatch
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_install_dispatches(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_vvread_uninstall_dispatches(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        run_vvread_install("--yes", env_extra=env, cwd=cwd)
        r = run_vvread_uninstall(env_extra=env, cwd=cwd)
        assert r.returncode == 0
        # voiceClaude hook は消える(他キーは無いので hooks 自体が消える)
        target = cwd / ".claude" / "settings.local.json"
        if target.exists():
            data = _read_settings(target)
            # hooks 自体が消えるか、Stop に voiceClaude エントリが無い
            stop = data.get("hooks", {}).get("Stop", [])
            for block in stop:
                for h in block.get("hooks", []):
                    assert "vvread" not in h.get("command", "") or \
                           "on-stop" not in h.get("command", "")

    def test_vvread_install_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_install("--no-such-flag", env_extra=env, cwd=cwd)
        assert r.returncode == 2

    def test_vvread_uninstall_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_uninstall("--no-such-flag", env_extra=env, cwd=cwd)
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# install / uninstall ラウンドトリップ
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_install_then_uninstall_back_to_original(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        target = cwd / ".claude" / "settings.local.json"
        original_data = {
            "permissions": {"allow": ["Bash(*)"]},
            "hooks": {"Stop": [
                {"matcher": "", "hooks": [{
                    "type": "command", "command": "/usr/local/bin/keep_me",
                }]},
            ]}
        }
        _write_settings(target, original_data)
        run_install("--yes", env_extra=env, cwd=cwd)
        run_uninstall(env_extra=env, cwd=cwd)
        # voiceClaude hook が消え、他はそのまま
        data = _read_settings(target)
        assert data["permissions"] == {"allow": ["Bash(*)"]}
        cmds = []
        for block in data["hooks"]["Stop"]:
            for h in block["hooks"]:
                cmds.append(h["command"])
        assert cmds == ["/usr/local/bin/keep_me"]


# ---------------------------------------------------------------------------
# deprecated scope alias (CLI 統合)
# ---------------------------------------------------------------------------


class TestDeprecatedScopeAlias:
    def test_project_shared_warns_and_writes_settings_json(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", "--scope", "project-shared", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        # deprecated alias なので settings.json(project scope)に書かれる
        assert (cwd / ".claude" / "settings.json").exists()
        assert not (cwd / ".claude" / "settings.local.json").exists()
        # 警告が stderr に出る
        assert "deprecated" in r.stderr.lower()

    def test_project_local_is_default_scope(self, tmp_path):
        """デフォルト scope は project-local → settings.local.json"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--yes", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# B-148: --with-mcp / --with-receiver / --receiver-only
# ---------------------------------------------------------------------------


class TestIntegrationFlags:
    def test_with_receiver_receiver_only_mutual_exclusion(self, tmp_path):
        """--with-receiver と --receiver-only の同時指定は argparse エラー (exit 2)"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--with-receiver", "--receiver-only", env_extra=env, cwd=cwd)
        assert r.returncode == 2

    def test_with_mcp_dry_run_no_settings_file(self, tmp_path):
        """--with-mcp --dry-run --yes: dry-run なので settings ファイルを作らない"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--with-mcp", "--dry-run", "--yes", env_extra=env, cwd=cwd)
        # dry-run は副作用なし
        assert not (cwd / ".claude" / "settings.local.json").exists()

    def test_receiver_only_dry_run_no_stop_hook(self, tmp_path):
        """--receiver-only --dry-run: Stop hook は登録しない"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--receiver-only", "--dry-run", env_extra=env, cwd=cwd)
        # Stop hook をスキップするので settings ファイルなし
        assert not (cwd / ".claude" / "settings.local.json").exists()
        # dry-run メッセージが出る
        assert "dry-run" in r.stdout.lower()

    def test_with_mcp_yes_registers_stop_hook(self, tmp_path):
        """--with-mcp --yes: Stop hook は登録される（settings ファイルが作られる）"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_install("--with-mcp", "--yes", env_extra=env, cwd=cwd)
        # Stop hook は成功のはず
        assert (cwd / ".claude" / "settings.local.json").exists()
        # MCP registration は claude CLI 不在で失敗するが stop hook は成功
        data = json.loads(
            (cwd / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        assert "hooks" in data
