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
        normalized_url = voicevox_mock["url"].rstrip("/")
        assert data["voicevox"]["engines"] == [normalized_url]
        assert "engineUrl" not in data["voicevox"]

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

    def test_interactive_scope_choice_by_number(self, tmp_path, monkeypatch):
        """対話で番号入力 (U-113): SCOPES の 2 番目 'project' を選ぶと
        project scope の settings.json が書かれる。"""
        # Git 内扱いにして user 補正を抑止し、純粋に番号選択経路を検証
        monkeypatch.setattr(setup_mod, "_in_git_repo", lambda cwd=None: True)
        ctx, cwd, home, fake_repo = _make_ctx(
            tmp_path,
            yes=False,
            in_text="2\n",  # SCOPES=("project-local","project","user") の 2 番目
        )
        result = setup_mod.step_hook(ctx)
        assert result.status == setup_mod.STATUS_OK
        out = ctx.out_stream.getvalue()
        assert "Hook scope を選択してください" in out
        # project scope → cwd/.claude/settings.json
        assert (cwd / ".claude" / "settings.json").exists()

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
    def test_all_skip_returns_all_skipped(self, tmp_path):
        ctx, *_ = _make_ctx(
            tmp_path, yes=True,
            skip_engine=True, skip_e2k=True, skip_hook=True, skip_mcp=True,
        )
        results = setup_mod.run_setup(ctx)
        # receiver は --with-receiver 未指定で SKIPPED（opt-in 専用）
        assert len(results) == 5
        assert all(r.status == setup_mod.STATUS_SKIPPED for r in results)
        assert [r.step for r in results] == ["engine", "e2k", "hook", "mcp", "receiver"]

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


# ---------------------------------------------------------------------------
# step_mcp テスト
# ---------------------------------------------------------------------------


class TestStepMcp:
    def test_skip_mcp_returns_skipped(self, tmp_path):
        ctx, *_ = _make_ctx(tmp_path, yes=True, skip_mcp=True)
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED

    def test_yes_only_returns_skipped(self, tmp_path):
        """--yes 単体では MCP は skip される"""
        ctx, *_ = _make_ctx(tmp_path, yes=True)
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED
        assert "with-mcp" in r.message

    def test_with_mcp_runs_step(self, tmp_path, monkeypatch):
        """--yes --with-mcp では step が実行される（claude 未インストール時は WARN）"""
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        ctx, *_ = _make_ctx(tmp_path, yes=True, with_mcp=True)
        r = setup_mod.step_mcp(ctx)
        # claude CLI が無いので WARN になるが step は実行される
        assert r.status == setup_mod.STATUS_WARN
        assert "claude" in r.message.lower()

    def test_skip_mcp_and_with_mcp_mutually_exclusive(self, tmp_path):
        """--skip-mcp と --with-mcp の同時指定は argparse usage error (exit 2)"""
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            setup_mod.main(["--skip-mcp", "--with-mcp", "--yes"])
        assert exc_info.value.code == 2

    def test_uv_sync_uses_repo_root_cwd(self, tmp_path, monkeypatch):
        """uv sync は ctx.repo_root で実行される"""
        calls = []

        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: False)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: "/usr/bin/uv" if cmd == "uv" else None)

        def fake_runner(cmd, **kwargs):
            calls.append((cmd, kwargs.get("cwd")))
            import types, subprocess
            r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return r

        ctx, cwd, _, fake_repo = _make_ctx(tmp_path, yes=True, with_mcp=True)
        ctx.runner = fake_runner
        setup_mod.step_mcp(ctx)

        uv_cmds = [(c, cwd_) for c, cwd_ in calls if c and c[0] == "uv"]
        assert uv_cmds, "uv sync が呼ばれなかった"
        assert str(fake_repo) in uv_cmds[0][1], (
            f"uv sync の cwd が repo_root でない: {uv_cmds[0][1]}"
        )

    def test_claude_mcp_add_uses_project_cwd(self, tmp_path, monkeypatch):
        """claude mcp add は ctx.cwd で実行される"""
        calls = []

        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            calls.append((cmd, kwargs.get("cwd")))
            import types
            # claude mcp get は exit 1（未登録）
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        ctx, cwd, _, _ = _make_ctx(tmp_path, yes=True, with_mcp=True)
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_OK

        add_cmds = [(c, cwd_) for c, cwd_ in calls if c and c[0] == "claude" and len(c) > 2 and c[2] == "add"]
        assert add_cmds, "claude mcp add が呼ばれなかった"
        assert str(cwd) in add_cmds[0][1], (
            f"claude mcp add の cwd が ctx.cwd でない: {add_cmds[0][1]}"
        )

    def test_already_registered_returns_warn(self, tmp_path, monkeypatch):
        """claude mcp get が exit 0 → WARN で上書きしない"""
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            import types
            return types.SimpleNamespace(returncode=0, stdout="vvread: ...", stderr="")

        ctx, *_ = _make_ctx(tmp_path, yes=True, with_mcp=True)
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_WARN
        assert "already registered" in r.message
        assert "not overwriting" in r.message

    # characterization tests — lock down command format before extraction to mcp_tools_install.py
    def test_mcp_add_command_format(self, tmp_path, monkeypatch):
        """claude mcp add の引数形式を固定する"""
        calls = []
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            calls.append((cmd, kwargs.get("cwd")))
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        ctx, cwd, _, fake_repo = _make_ctx(tmp_path, yes=True, with_mcp=True)
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_OK
        assert r.message == "registered vvread MCP server"

        add_cmds = [c for c, _ in calls if c and len(c) > 2 and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds, "claude mcp add が呼ばれなかった"
        cmd = add_cmds[0]
        # --transport stdio --scope local vvread -- {abs_path} mcp
        assert "--transport" in cmd
        assert "stdio" in cmd
        assert "--scope" in cmd
        assert "local" in cmd
        assert "vvread" in cmd
        assert str(fake_repo / "bin" / "vvread") in cmd
        assert "mcp" in cmd


# ---------------------------------------------------------------------------
# _check_mcp_installed テスト
# ---------------------------------------------------------------------------


class TestCheckMcpInstalled:
    """dependency gate は重い `import mcp` ではなく軽量な
    `importlib.metadata.version('mcp')` で確認する（uv sync 直後の cold import が
    timeout を超過して誤 WARN を出す回帰を防ぐ）。"""

    def _make_repo_with_venv(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".venv" / "bin").mkdir(parents=True)
        (repo / ".venv" / "bin" / "python").write_text("#!/bin/bash\nexit 0\n")
        (repo / ".venv" / "bin" / "python").chmod(0o755)
        return repo

    def test_uses_metadata_version_not_bare_import(self, tmp_path, monkeypatch):
        repo = self._make_repo_with_venv(tmp_path)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            import types
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

        assert setup_mod._check_mcp_installed(repo) is True

        cmd = captured["cmd"]
        # repo_root/.venv/bin/python を使う
        assert cmd[0] == str(repo / ".venv" / "bin" / "python")
        # python -c "<code>" の形式
        assert cmd[1] == "-c"
        code = cmd[2]
        # importlib.metadata + version("mcp") / version('mcp') を使う
        assert "importlib.metadata" in code
        assert ("version('mcp')" in code) or ('version("mcp")' in code)
        # 素の import mcp を使わない（cold compile を避ける）
        assert "import mcp" not in code
        # timeout >= 10
        assert captured["kwargs"].get("timeout", 0) >= 10

    def test_returns_false_when_venv_python_missing(self, tmp_path):
        repo = tmp_path / "no_venv"
        repo.mkdir()
        assert setup_mod._check_mcp_installed(repo) is False

    def test_returns_false_on_timeout(self, tmp_path, monkeypatch):
        repo = self._make_repo_with_venv(tmp_path)

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 10))

        monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
        assert setup_mod._check_mcp_installed(repo) is False

    def test_mcp_dry_run_message_format(self, tmp_path, monkeypatch):
        """dry-run のメッセージ形式を固定する"""
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        ctx, _, _, fake_repo = _make_ctx(tmp_path, yes=True, with_mcp=True, dry_run=True)
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_OK
        assert "[dry-run]" in r.message
        assert "claude mcp add" in r.message
        assert "--scope" in r.message
        assert "local" in r.message
        assert str(fake_repo / "bin" / "vvread") in r.message

    def test_interactive_yes_proceeds_with_mcp(self, tmp_path, monkeypatch):
        """対話で Yes → MCP 登録が実行される"""
        calls = []
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        # yes=False、with_mcp=False、入力は "y" → prompt で Yes
        ctx, *_ = _make_ctx(tmp_path, yes=False, with_mcp=False, in_text="y\n")
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_OK
        add_cmds = [c for c in calls if c and len(c) > 2 and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds, "インタラクティブ Yes 後に claude mcp add が呼ばれなかった"

    def test_interactive_no_skips_mcp(self, tmp_path, monkeypatch):
        """対話で No → MCP 登録をスキップ"""
        calls = []
        monkeypatch.setattr(setup_mod, "_check_mcp_installed", lambda _: True)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            import types
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx, *_ = _make_ctx(tmp_path, yes=False, with_mcp=False, in_text="n\n")
        ctx.runner = fake_runner
        r = setup_mod.step_mcp(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED
        add_cmds = [c for c in calls if c and len(c) > 2 and c[:3] == ["claude", "mcp", "add"]]
        assert add_cmds == [], "No 後に claude mcp add が呼ばれてしまった"


# ---------------------------------------------------------------------------
# step_receiver テスト (B-138/B-149)
# ---------------------------------------------------------------------------


class TestStepReceiver:
    def test_yes_without_with_receiver_skipped(self, tmp_path):
        """--yes 単体（--with-receiver なし）は即スキップ（opt-in 必要）"""
        ctx, *_ = _make_ctx(tmp_path, yes=True)
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED
        assert "with-receiver" in r.message

    def test_interactive_yes_proceeds_with_receiver(self, tmp_path, monkeypatch):
        """対話で Yes → with_receiver が有効化されセットアップが進む"""
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(setup_mod, "_receiver_sdk_installed", lambda _: True)

        def fake_runner(cmd, **kwargs):
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        # 1回目の "Set up external-event receiver?" → "y"
        # 2回目の "続けますか?" → "y"
        import io
        ctx, *_ = _make_ctx(tmp_path, yes=False, with_receiver=False)
        ctx.in_stream = io.StringIO("y\ny\n")
        ctx.runner = fake_runner
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_OK, r.message

    def test_interactive_no_skips_receiver(self, tmp_path):
        """対話で No → skip"""
        import io
        ctx, *_ = _make_ctx(tmp_path, yes=False, with_receiver=False)
        ctx.in_stream = io.StringIO("n\n")
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED

    def test_yes_with_receiver_skips_confirmation(self, tmp_path, monkeypatch):
        """--yes --with-receiver は確認省略でそのまま進む"""
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(setup_mod, "_receiver_sdk_installed", lambda _: True)

        def fake_runner(cmd, **kwargs):
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        ctx, *_ = _make_ctx(tmp_path, yes=True, with_receiver=True)
        ctx.runner = fake_runner
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_OK, r.message

    def test_with_mcp_only_does_not_register_receiver(self, tmp_path):
        """--with-mcp だけでは receiver 登録しない"""
        ctx, *_ = _make_ctx(tmp_path, yes=True, with_mcp=True)
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_SKIPPED

    def test_bun_absent_is_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: None)
        ctx, *_ = _make_ctx(tmp_path, yes=True, with_receiver=True)
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_WARN
        assert "bun" in r.message.lower()

    def test_registers_receiver_local(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(setup_mod, "_receiver_sdk_installed", lambda _: True)

        def fake_runner(cmd, **kwargs):
            calls.append((cmd, kwargs.get("cwd")))
            import types
            rc = 1 if (len(cmd) > 2 and cmd[2] == "get") else 0
            return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

        ctx, cwd, _, fake_repo = _make_ctx(tmp_path, yes=True, with_receiver=True)
        ctx.runner = fake_runner
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_OK, r.message
        add_cmds = [(c, cwd_) for c, cwd_ in calls if c and c[1:3] == ["mcp", "add"]]
        assert add_cmds, "claude mcp add が呼ばれなかった"
        cmd, _ = add_cmds[0]
        assert "vvread-receiver" in cmd
        assert "bun" in cmd
        assert "--scope" in cmd and "local" in cmd
        assert str(fake_repo / "receiver" / "server.ts") in cmd

    def test_already_registered_no_overwrite(self, tmp_path, monkeypatch):
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        monkeypatch.setattr(setup_mod, "_receiver_sdk_installed", lambda _: True)

        def fake_runner(cmd, **kwargs):
            import types
            # get → exit 0 with "local" → registered_local
            return types.SimpleNamespace(returncode=0, stdout="scope: local", stderr="")

        ctx, *_ = _make_ctx(tmp_path, yes=True, with_receiver=True)
        ctx.runner = fake_runner
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_WARN
        assert "already registered" in r.message

    def test_dry_run_no_side_effects(self, tmp_path, monkeypatch):
        """--with-receiver --dry-run は bun install も登録も実行しない"""
        calls = []
        monkeypatch.setattr(setup_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            import types
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx, *_ = _make_ctx(tmp_path, yes=True, with_receiver=True, dry_run=True)
        ctx.runner = fake_runner
        r = setup_mod.step_receiver(ctx)
        assert r.status == setup_mod.STATUS_OK
        assert "dry-run" in r.message
        assert calls == [], f"dry-run で runner が呼ばれた: {calls}"
