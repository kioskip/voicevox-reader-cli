"""scripts/cmd_setup.sh + bin/vvread setup の統合テスト (R-010 / R-011)

setup.py 単体は test_setup.py で網羅。本テストは bash ラッパー + bin/vvread
dispatch + CLI フラグ + 終了コード仕様(R-010/R-011 ユーザ指定)に集中する。

ユーザ仕様の終了コード:
  0 = 成功(全 step OK / WARN / SKIPPED)
  1 = いずれかの step ERROR
  2 = 使い方エラー(argparse default、不正オプション)
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_SETUP = REPO / "scripts" / "cmd" / "setup.sh"


def _clean_env(env_extra=None) -> dict:
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def _setup_env(tmp_path: Path) -> tuple[Path, Path, dict]:
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    fake_repo = tmp_path / "fake_repo"
    cwd.mkdir()
    home.mkdir()
    fake_repo.mkdir()
    (fake_repo / "bin").mkdir()
    (fake_repo / "bin" / "vvread").write_text("#!/bin/bash\nexit 0\n")
    (fake_repo / "bin" / "vvread").chmod(0o755)
    env = {
        "HOME": str(home),
        "VVREAD_PROJECT_DIR": str(fake_repo),
        "VVREAD_SCRIPTS_DIR": str(REPO / "scripts"),
    }
    return cwd, home, env


def run_setup(*args, env_extra=None, cwd=None, timeout=15):
    return subprocess.run(
        [str(CMD_SETUP), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_vvread_setup(*args, env_extra=None, cwd=None, timeout=15):
    return subprocess.run(
        [str(VVREAD), "setup", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 終了コード仕様
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_yes_skip_all_exits_0(self, tmp_path):
        """--yes + 全 skip で何も実行されないが exit 0(SKIPPED は成功)"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup("--yes", "--skip-engine", "--skip-e2k", "--skip-hook",
                      env_extra=env, cwd=cwd)
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

    def test_engine_error_exits_1(self, tmp_path):
        """unreachable URL で engine ERROR → exit 1"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes",
            "--engine-url", "http://127.0.0.1:1",
            "--skip-e2k", "--skip-hook",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 1
        assert "ERROR" in r.stdout

    def test_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup("--bogus", env_extra=env, cwd=cwd)
        assert r.returncode == 2

    def test_install_e2k_and_no_install_e2k_mutually_exclusive(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--install-e2k", "--no-install-e2k",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 2
        assert "mutually exclusive" in r.stderr


# ---------------------------------------------------------------------------
# tty / --yes ガード
# ---------------------------------------------------------------------------


class TestTtyGuard:
    def test_non_tty_without_yes_exits_1(self, tmp_path):
        """subprocess.run の stdin は pipe = non-tty。--yes 無しで起動すると
        ERROR で exit 1。"""
        cwd, home, env = _setup_env(tmp_path)
        r = subprocess.run(
            [str(CMD_SETUP)],
            env=_clean_env(env),
            input="",
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=10,
        )
        assert r.returncode == 1
        assert "non-interactive" in r.stdout or "non-interactive" in r.stderr


# ---------------------------------------------------------------------------
# happy path: --yes で全 step を回す(voicevox_mock + skip-e2k)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_yes_full_run_with_mock(self, voicevox_mock, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes",
            "--engine-url", voicevox_mock["url"],
            "--no-install-e2k",  # e2k は明示 skip
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        # engine OK
        assert "engine" in r.stdout
        assert "OK" in r.stdout
        # e2k SKIPPED
        assert "SKIPPED" in r.stdout
        # hook OK + settings 作成
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_engine_url_writes_to_project_settings(self, voicevox_mock, tmp_path):
        """default 以外の URL → vvread.settings.json に書込"""
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes",
            "--engine-url", voicevox_mock["url"],
            "--no-install-e2k",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0
        settings = cwd / "vvread.settings.json"
        assert settings.exists()
        data = json.loads(settings.read_text(encoding="utf-8"))
        normalized_url = voicevox_mock["url"].rstrip("/")
        assert data["voicevox"]["engines"] == [normalized_url]
        assert "engineUrl" not in data["voicevox"]


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_creates_no_files(self, voicevox_mock, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--dry-run",
            "--engine-url", voicevox_mock["url"],
            "--no-install-e2k",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0
        assert "[dry-run]" in r.stdout
        assert not (cwd / "vvread.settings.json").exists()
        assert not (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_output_has_all_steps(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--json",
            "--skip-engine", "--skip-e2k", "--skip-hook", "--skip-mcp",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert isinstance(payload, list)
        # receiver / menubar は --with-* 未指定で SKIPPED（opt-in 専用）
        assert len(payload) == 6
        steps = [item["step"] for item in payload]
        assert steps == ["engine", "e2k", "hook", "mcp", "receiver", "menubar"]
        for item in payload:
            assert item["status"] == "SKIPPED"


# ---------------------------------------------------------------------------
# --skip-* 部分実行
# ---------------------------------------------------------------------------


class TestSkipFlags:
    def test_skip_engine_only(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--skip-engine",
            "--no-install-e2k",
            env_extra=env, cwd=cwd,
        )
        # engine SKIPPED、e2k SKIPPED、hook OK で全体 0
        assert r.returncode == 0
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_skip_hook_only(self, voicevox_mock, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes",
            "--engine-url", voicevox_mock["url"],
            "--no-install-e2k",
            "--skip-hook",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0
        # hook はスキップされたので settings 作成なし
        assert not (cwd / ".claude" / "settings.local.json").exists()

    def test_skip_menubar_only(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--skip-engine", "--skip-e2k", "--skip-hook",
            "--skip-menubar",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0
        assert "SKIPPED" in r.stdout


# ---------------------------------------------------------------------------
# --with-menubar (B-156)
# ---------------------------------------------------------------------------
#
# fake_repo には .venv が無いため rumps 判定は常に False になり、register()
# は launchctl を一切呼ばずに WARN を返す(実 launchctl 非依存で安全に検証
# できる)。


class TestMenubarFlag:
    def test_with_menubar_no_rumps_is_warn_not_error(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--skip-engine", "--skip-e2k", "--skip-hook",
            "--with-menubar",
            env_extra=env, cwd=cwd,
        )
        # rumps 不在は WARN 扱いなので exit 0 のまま
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert "menubar" in r.stdout
        # 実 LaunchAgent は登録されない(fake HOME に plist が無いことを確認)
        assert not (
            home / "Library" / "LaunchAgents" / "com.vvread.menubar.plist"
        ).exists()

    def test_skip_menubar_and_with_menubar_mutually_exclusive(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_setup(
            "--yes", "--skip-menubar", "--with-menubar",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 2


# ---------------------------------------------------------------------------
# bin/vvread setup dispatch
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_setup_dispatches(self, voicevox_mock, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_setup(
            "--yes",
            "--engine-url", voicevox_mock["url"],
            "--no-install-e2k",
            env_extra=env, cwd=cwd,
        )
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_vvread_setup_help_via_help_flag(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_setup("--help", env_extra=env, cwd=cwd)
        assert r.returncode == 0
        assert "setup" in r.stdout.lower() or "engine-url" in r.stdout.lower()

    def test_vvread_setup_unknown_flag_exits_2(self, tmp_path):
        cwd, home, env = _setup_env(tmp_path)
        r = run_vvread_setup("--no-such-flag", env_extra=env, cwd=cwd)
        assert r.returncode == 2
