"""scripts/setup.py の単体テスト (R-011)

各 step を Python module として直接 import + DI で検証する。CLI / bin/vvread
経由の統合テストは tests/test_cmd_setup.py 側。

カバー範囲:
- step_engine: 疎通 OK / unreachable / URL 上書き + settings.json 書込 /
  default URL はスキップ / dry-run
- step_e2k: 既に installed → no-op / ユーザ承諾 → 模擬 install OK / 模擬
  install 失敗 → WARN / --yes default skip
- step_hook: hook_install を import で再利用、scope ごと、dry-run
- run_setup: 連鎖実行、ERROR 連鎖防止、tty + --yes 判定、--skip-*
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import setup as setup_mod  # noqa: E402
import hook_install as hi  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeTTYStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeNonTTYStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


def _make_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """cwd / home / fake_repo のディレクトリ群を作って返す"""
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    fake_repo = tmp_path / "fake_repo"
    cwd.mkdir()
    home.mkdir()
    fake_repo.mkdir()
    (fake_repo / "bin").mkdir()
    (fake_repo / "bin" / "vvread").write_text("#!/bin/bash\nexit 0\n")
    (fake_repo / "bin" / "vvread").chmod(0o755)
    return cwd, home, fake_repo


def _make_ctx(
    tmp_path: Path,
    *,
    yes: bool = True,  # default は --yes 経路(stdin がないテスト環境向け)
    in_text: str = "",
    **overrides,
):
    cwd, home, fake_repo = _make_dirs(tmp_path)
    ctx = setup_mod.SetupContext(
        yes=yes,
        cwd=cwd,
        home=home,
        repo_root=fake_repo,
        in_stream=_FakeTTYStdin(in_text),
        out_stream=io.StringIO(),
        err_stream=io.StringIO(),
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx, cwd, home, fake_repo


# ---------------------------------------------------------------------------
# tty + --yes ガード
# ---------------------------------------------------------------------------


class TestTtyOrYesGuard:
    def test_non_tty_without_yes_errors(self, tmp_path):
        cwd, home, fake_repo = _make_dirs(tmp_path)
        ctx = setup_mod.SetupContext(
            yes=False,
            cwd=cwd, home=home, repo_root=fake_repo,
            in_stream=_FakeNonTTYStdin(""),
        )
        results = setup_mod.run_setup(ctx)
        assert len(results) == 1
        assert results[0].status == setup_mod.STATUS_ERROR
        assert "non-interactive" in results[0].message
        assert "--yes" in (results[0].hint or "")

    def test_yes_flag_bypasses_tty_check(self, tmp_path, monkeypatch):
        # voicevox_mock を立てるのが面倒なので skip-* で全 step をスキップ
        cwd, home, fake_repo = _make_dirs(tmp_path)
        ctx = setup_mod.SetupContext(
            yes=True,
            cwd=cwd, home=home, repo_root=fake_repo,
            in_stream=_FakeNonTTYStdin(""),
            skip_engine=True, skip_e2k=True, skip_hook=True,
        )
        results = setup_mod.run_setup(ctx)
        # ERROR にならず 3 件 SKIPPED で帰る
        assert all(r.status == setup_mod.STATUS_SKIPPED for r in results)


# ---------------------------------------------------------------------------
# step_engine: existing
# ---------------------------------------------------------------------------


class TestStepEngine:
    def test_reachable_url(self, voicevox_mock, tmp_path):
        ctx, cwd, *_ = _make_ctx(
            tmp_path, yes=True,
            engine_url=voicevox_mock["url"],
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert "connected to VOICEVOX" in result.message

    def test_unreachable_url_errors(self, tmp_path):
        ctx, *_ = _make_ctx(
            tmp_path, yes=True,
            engine_url="http://127.0.0.1:1",
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_ERROR
        assert "not reachable" in result.message

    def test_non_default_url_writes_settings(self, voicevox_mock, tmp_path):
        """default 以外の URL → vvread.settings.json に書き込まれる"""
        ctx, cwd, *_ = _make_ctx(
            tmp_path, yes=True,
            engine_url=voicevox_mock["url"],
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_OK
        settings_path = cwd / "vvread.settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["voicevox"]["engineUrl"] == voicevox_mock["url"]

    def test_default_url_no_settings_write(self, tmp_path, monkeypatch):
        """default URL なら settings は書かない。"""
        monkeypatch.setattr(
            setup_mod, "DEFAULT_ENGINE_URL", "http://nowhere.invalid:1",
        )
        ctx, cwd, *_ = _make_ctx(
            tmp_path, yes=True,
            engine_url="http://nowhere.invalid:1",
        )
        monkeypatch.setattr(
            setup_mod, "_engine_reachable",
            lambda url: {"version": "0.14.0", "speakers_count": 4},
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert not (cwd / "vvread.settings.json").exists()

    def test_dry_run_does_not_write_settings(self, voicevox_mock, tmp_path):
        ctx, cwd, *_ = _make_ctx(
            tmp_path, yes=True, dry_run=True,
            engine_url=voicevox_mock["url"],
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert "[dry-run]" in result.message
        assert not (cwd / "vvread.settings.json").exists()


# ---------------------------------------------------------------------------
# step_e2k
# ---------------------------------------------------------------------------


class TestStepE2k:
    def test_already_installed_returns_ok(self, tmp_path, monkeypatch):
        # e2k がインストール済みと判定するよう monkeypatch
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: True,
        )
        ctx, *_ = _make_ctx(tmp_path, install_e2k=True)
        result = setup_mod.step_e2k(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert "already installed" in result.message

    def test_yes_default_skips(self, tmp_path, monkeypatch):
        """--yes 時のデフォルトは skip(重い依存を勝手に入れない)"""
        # e2k がインストール済みでないことをシミュレート
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: False,
        )
        ctx, *_ = _make_ctx(tmp_path, yes=True)
        result = setup_mod.step_e2k(ctx)
        assert result.status == setup_mod.STATUS_SKIPPED

    def test_install_e2k_explicit_runs(self, tmp_path, monkeypatch):
        """--install-e2k で実行(fake runner で成功)"""
        # e2k がインストール済みでないことをシミュレート
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: False,
        )
        # uv が無くても python3 がある経路で動くよう shutil.which を制御
        monkeypatch.setattr(
            setup_mod.shutil, "which",
            lambda name: None if name == "uv" else "/usr/bin/" + name,
        )
        runs: list = []

        def fake_runner(cmd, **kwargs):
            runs.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr="",
            )
        ctx, cwd, home, fake_repo = _make_ctx(
            tmp_path, yes=True, install_e2k=True,
        )
        ctx.runner = fake_runner
        result = setup_mod.step_e2k(ctx)
        assert result.status == setup_mod.STATUS_OK
        # e2k インストールコマンドが走った
        assert any("e2k" in c for cmd in runs for c in cmd)

    def test_install_failure_is_warn_not_error(self, tmp_path, monkeypatch):
        # e2k がインストール済みでないことをシミュレート
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: False,
        )
        monkeypatch.setattr(
            setup_mod.shutil, "which",
            lambda name: None if name == "uv" else "/usr/bin/" + name,
        )

        def fake_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="network down",
            )
        ctx, *_ = _make_ctx(
            tmp_path, yes=True, install_e2k=True,
        )
        ctx.runner = fake_runner
        result = setup_mod.step_e2k(ctx)
        # install 失敗は WARN(setup 全体は止めない)
        assert result.status == setup_mod.STATUS_WARN

    def test_no_install_e2k_explicit_skips(self, tmp_path, monkeypatch):
        # e2k がインストール済みでないことをシミュレート
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: False,
        )
        ctx, *_ = _make_ctx(
            tmp_path, yes=False,
            install_e2k=False,
            in_text="",  # 対話 prompt を呼ばないはず
        )
        result = setup_mod.step_e2k(ctx)
        assert result.status == setup_mod.STATUS_SKIPPED


# ---------------------------------------------------------------------------
# step_hook
# ---------------------------------------------------------------------------


class TestStepHook:
    def test_creates_settings_at_project_scope(self, tmp_path):
        ctx, cwd, home, fake_repo = _make_ctx(tmp_path, yes=True)
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_already_present_is_ok(self, tmp_path):
        ctx, cwd, home, fake_repo = _make_ctx(tmp_path, yes=True)
        # 1 回目
        setup_mod.step_hook(ctx)
        # 2 回目
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert "already registered" in result.message

    def test_dry_run_does_not_create(self, tmp_path):
        ctx, cwd, home, fake_repo = _make_ctx(tmp_path, yes=True, dry_run=True)
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert "[dry-run]" in result.message
        assert not (cwd / ".claude" / "settings.local.json").exists()

    def test_user_scope_writes_to_home(self, tmp_path):
        ctx, cwd, home, fake_repo = _make_ctx(
            tmp_path, yes=True, hook_scope="user",
        )
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        assert (home / ".claude" / "settings.json").exists()

    def test_git_check_recommends_user_scope_outside_git(self, tmp_path, monkeypatch):
        """Git 外・対話モードで project-local デフォルト → user scope に変更して警告を出す"""
        monkeypatch.setattr(setup_mod, "_in_git_repo", lambda cwd=None: False)
        ctx, cwd, home, fake_repo = _make_ctx(
            tmp_path,
            yes=False,
            # Enter を押してデフォルト scope をそのまま受け入れる
            in_text="\n",
        )
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        out = ctx.out_stream.getvalue()
        assert "Git リポジトリ外" in out
        assert "user" in out
        # user scope のファイルに書き込まれること
        assert (home / ".claude" / "settings.json").exists()

    def test_git_check_not_applied_in_yes_mode(self, tmp_path, monkeypatch):
        """--yes モードでは Git チェックを適用しない（project-local のまま）"""
        monkeypatch.setattr(setup_mod, "_in_git_repo", lambda cwd=None: False)
        ctx, cwd, home, fake_repo = _make_ctx(tmp_path, yes=True)
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        # user scope ではなく project-local に書き込まれること
        assert (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# run_setup: 連鎖実行 / ERROR 連鎖防止 / --skip-*
# ---------------------------------------------------------------------------


class TestRunSetup:
    def test_all_skip_returns_three_skipped(self, tmp_path):
        ctx, *_ = _make_ctx(
            tmp_path, yes=True,
            skip_engine=True, skip_e2k=True, skip_hook=True,
        )
        results = setup_mod.run_setup(ctx)
        assert len(results) == 3
        assert all(r.status == setup_mod.STATUS_SKIPPED for r in results)
        assert [r.step for r in results] == ["engine", "e2k", "hook"]

    def test_engine_error_skips_following_steps(self, tmp_path):
        """engine が ERROR(unreachable URL)→ e2k/hook は連鎖防止で SKIPPED"""
        ctx, *_ = _make_ctx(
            tmp_path, yes=True,
            engine_url="http://127.0.0.1:1",  # 確実に unreachable
        )
        results = setup_mod.run_setup(ctx)
        assert results[0].step == "engine"
        assert results[0].status == setup_mod.STATUS_ERROR
        assert results[1].step == "e2k"
        assert results[1].status == setup_mod.STATUS_SKIPPED
        assert "earlier ERROR" in results[1].message
        assert results[2].step == "hook"
        assert results[2].status == setup_mod.STATUS_SKIPPED

    def test_engine_warn_does_not_block_following(self, voicevox_mock, tmp_path, monkeypatch):
        """engine OK + e2k SKIPPED → hook は実行される"""
        # e2k がインストール済みでないことをシミュレート
        monkeypatch.setattr(
            setup_mod, "_check_e2k_installed",
            lambda venv_python: False,
        )
        ctx, cwd, home, fake_repo = _make_ctx(
            tmp_path, yes=True,
            engine_url=voicevox_mock["url"],
            install_e2k=False,  # e2k は SKIPPED
        )
        results = setup_mod.run_setup(ctx)
        assert results[0].status == setup_mod.STATUS_OK         # engine
        assert results[1].status == setup_mod.STATUS_SKIPPED    # e2k
        assert results[2].status == setup_mod.STATUS_OK         # hook
        # hook が走って settings が作られている
        assert (cwd / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# 対話 prompt 動作確認(基本系)
# ---------------------------------------------------------------------------


class TestInteractive:
    def test_engine_url_prompt_enter_uses_default(self, voicevox_mock, tmp_path):
        """対話で URL プロンプトに mock URL を入力 → 接続 OK"""
        cwd, home, fake_repo = _make_dirs(tmp_path)
        ctx = setup_mod.SetupContext(
            yes=False,
            cwd=cwd, home=home, repo_root=fake_repo,
            in_stream=_FakeTTYStdin(voicevox_mock["url"] + "\n"),
            out_stream=io.StringIO(),
        )
        result = setup_mod.step_engine(ctx)
        assert result.status == setup_mod.STATUS_OK
