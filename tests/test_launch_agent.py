"""tests/test_launch_agent.py - scripts/launch_agent.py の単体テスト (B-156)

macOS の menubar LaunchAgent 登録/解除ロジックを、実 `launchctl` を一切呼ばずに
検証する。`runner` は必ずモック関数を注入し、`home` は tmp_path 配下に向ける
(実 `~/Library/LaunchAgents/` には一切触れない)。
"""
import plistlib
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import launch_agent as la  # noqa: E402
import doctor as _doctor  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_runner(responses=None, calls=None):
    """呼び出しを calls に記録し、responses の returncode を順に返す runner。

    responses: list[int] (returncode)。無ければ常に 0。最後の値を繰り返す。
    """
    if calls is None:
        calls = []
    idx = [0]

    def runner(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if responses:
            i = min(idx[0], len(responses) - 1)
            rc = responses[i]
            idx[0] += 1
        else:
            rc = 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")

    runner.calls = calls
    return runner


@pytest.fixture
def repo_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "vvread").write_text("#!/bin/bash\nexit 0\n")
    (repo / "bin" / "vvread").chmod(0o755)
    return repo


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def rumps_available(monkeypatch):
    """rumps 可用性チェックを常に True に固定する(register の本筋を検証するため)。"""
    monkeypatch.setattr(la, "_rumps_available", lambda repo_root: True)


# ---------------------------------------------------------------------------
# build_plist
# ---------------------------------------------------------------------------


class TestBuildPlist:
    def test_contents(self, repo_root, tmp_path):
        log_dir = tmp_path / "logs"
        data = la.build_plist(repo_root, log_dir)
        parsed = plistlib.loads(data)

        assert parsed["Label"] == "com.vvread.menubar"
        assert parsed["ProgramArguments"] == [
            str(repo_root / "bin" / "vvread"), "menubar",
        ]
        assert parsed["RunAtLoad"] is True
        assert "KeepAlive" not in parsed
        expected_log = str(log_dir / "menubar.launchagent.log")
        assert parsed["StandardOutPath"] == expected_log
        assert parsed["StandardErrorPath"] == expected_log

    def test_deterministic_bytes(self, repo_root, tmp_path):
        """同じ入力なら常に同じ bytes(register の no-op 判定が byte 比較の
        ため、安定していることを保証する)。"""
        log_dir = tmp_path / "logs"
        assert la.build_plist(repo_root, log_dir) == la.build_plist(repo_root, log_dir)

    def test_no_side_effects(self, repo_root, tmp_path):
        """純粋関数: log_dir が存在しなくてもファイル/ディレクトリを作らない。"""
        log_dir = tmp_path / "does_not_exist_yet"
        la.build_plist(repo_root, log_dir)
        assert not log_dir.exists()

    def test_menubar_python_env_included_when_given(self, repo_root, tmp_path):
        """menubar_python 指定時、EnvironmentVariables に
        VVREAD_MENUBAR_PYTHON が含まれる。"""
        log_dir = tmp_path / "logs"
        data = la.build_plist(
            repo_root, log_dir, menubar_python="/opt/homebrew/bin/python3.11",
        )
        parsed = plistlib.loads(data)
        assert parsed["EnvironmentVariables"] == {
            "VVREAD_MENUBAR_PYTHON": "/opt/homebrew/bin/python3.11",
        }

    def test_menubar_python_env_absent_when_not_given(self, repo_root, tmp_path):
        """menubar_python 未指定時、EnvironmentVariables キー自体が無い。"""
        log_dir = tmp_path / "logs"
        data = la.build_plist(repo_root, log_dir)
        parsed = plistlib.loads(data)
        assert "EnvironmentVariables" not in parsed


class TestPlistPath:
    def test_location(self, home):
        assert la.plist_path(home) == (
            home / "Library" / "LaunchAgents" / "com.vvread.menubar.plist"
        )


# ---------------------------------------------------------------------------
# register: rumps 不在
# ---------------------------------------------------------------------------


class TestRegisterRumpsUnavailable:
    def test_returns_warning_without_writing_or_calling(
        self, repo_root, home, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(la, "_rumps_available", lambda repo_root: False)
        calls = []
        runner = _fake_runner(calls=calls)

        result = la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )

        assert result.ok is False
        assert result.rumps_available is False
        assert result.changed is False
        assert result.action == "rumps_unavailable"
        assert not la.plist_path(home).exists()
        assert calls == []


# ---------------------------------------------------------------------------
# register: 初回登録
# ---------------------------------------------------------------------------


class TestRegisterFreshInstall:
    def test_creates_plist_and_calls_bootstrap_only(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        calls = []
        runner = _fake_runner(calls=calls)
        log_dir = tmp_path / "logs"

        result = la.register(
            repo_root=repo_root, log_dir=log_dir, home=home,
            uid=501, runner=runner,
        )

        assert result.ok is True
        assert result.changed is True
        assert result.action == "install"
        assert result.dry_run is False

        plist = la.plist_path(home)
        assert plist.exists()
        # bootout は呼ばれず、bootstrap のみ呼ばれる(初回は既存定義が無いため)
        assert len(calls) == 1
        assert calls[0][0] == ["launchctl", "bootstrap", "gui/501", str(plist)]

        # ログディレクトリが 0700 で作成されている
        assert log_dir.exists()
        assert (log_dir.stat().st_mode & 0o777) == 0o700

    def test_no_leftover_tmp_file(self, repo_root, home, tmp_path, rumps_available):
        runner = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )
        plist = la.plist_path(home)
        # Path.with_suffix() は最後のドット区切りだけを置換するため、多分割
        # basename (com.vvread.menubar.plist) でも意図通りの名前になることを
        # リテラルで固定する(with_suffix の挙動そのものに依存した自己参照的
        # assertion を避けるため)。
        assert plist.name == "com.vvread.menubar.plist"
        tmp_file = plist.parent / "com.vvread.menubar.plist.tmp"
        assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# register: VVREAD_MENUBAR_PYTHON の永続化 (B-156 追補)
# ---------------------------------------------------------------------------


class TestRegisterMenubarPythonEnv:
    def test_register_persists_menubar_python_env_var(
        self, repo_root, home, tmp_path, rumps_available, monkeypatch,
    ):
        """register() 実行時の VVREAD_MENUBAR_PYTHON 環境変数が、生成される
        plist の EnvironmentVariables に記録される(ログイン時の LaunchAgent
        起動ではシェル環境を引き継がないため)。"""
        monkeypatch.setenv("VVREAD_MENUBAR_PYTHON", "/opt/homebrew/bin/python3.11")
        runner = _fake_runner()

        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )

        plist = la.plist_path(home)
        parsed = plistlib.loads(plist.read_bytes())
        assert parsed["EnvironmentVariables"] == {
            "VVREAD_MENUBAR_PYTHON": "/opt/homebrew/bin/python3.11",
        }

    def test_register_omits_env_key_when_var_unset(
        self, repo_root, home, tmp_path, rumps_available, monkeypatch,
    ):
        """VVREAD_MENUBAR_PYTHON が未設定なら、plist は EnvironmentVariables
        キー自体を持たない。"""
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        runner = _fake_runner()

        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )

        plist = la.plist_path(home)
        parsed = plistlib.loads(plist.read_bytes())
        assert "EnvironmentVariables" not in parsed


# ---------------------------------------------------------------------------
# register: 同一内容での再登録 → no-op
# ---------------------------------------------------------------------------


class TestRegisterNoop:
    def test_second_register_with_same_content_is_noop(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        log_dir = tmp_path / "logs"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir, home=home,
            uid=501, runner=runner1,
        )

        calls = []
        runner2 = _fake_runner(calls=calls)
        result = la.register(
            repo_root=repo_root, log_dir=log_dir, home=home,
            uid=501, runner=runner2,
        )

        assert result.ok is True
        assert result.changed is False
        assert result.action == "noop"
        assert calls == []  # launchctl は一切呼ばれない


# ---------------------------------------------------------------------------
# register: 内容変更 → bootout → 置換 → bootstrap
# ---------------------------------------------------------------------------


class TestRegisterContentChange:
    def test_bootout_then_replace_then_bootstrap(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        log_dir1 = tmp_path / "logs1"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir1, home=home,
            uid=501, runner=runner1,
        )

        calls = []
        runner2 = _fake_runner(calls=calls)
        log_dir2 = tmp_path / "logs2"  # log_dir を変えて plist 内容を変える
        result = la.register(
            repo_root=repo_root, log_dir=log_dir2, home=home,
            uid=501, runner=runner2,
        )

        assert result.ok is True
        assert result.changed is True
        assert result.action == "update"

        plist = la.plist_path(home)
        assert len(calls) == 2
        assert calls[0][0] == ["launchctl", "bootout", f"gui/501/{la.LABEL}"]
        assert calls[1][0] == ["launchctl", "bootstrap", "gui/501", str(plist)]

        parsed = plistlib.loads(plist.read_bytes())
        assert str(log_dir2) in parsed["StandardOutPath"]


# ---------------------------------------------------------------------------
# register: bootstrap 失敗時のロールバック
# ---------------------------------------------------------------------------


class TestRegisterBootstrapFailureRollback:
    def test_rollback_to_previous_content_on_update_failure(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        """update 失敗時: plist はロールバックされ、直前に bootout した旧定義の
        再ロード(recovery bootstrap)も試みられる。recovery も失敗する場合は
        その旨がメッセージ/error に明示される(サイレントに「無害」扱いしない)。
        """
        log_dir1 = tmp_path / "logs1"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir1, home=home,
            uid=501, runner=runner1,
        )
        plist = la.plist_path(home)
        original_bytes = plist.read_bytes()

        bootstrap_calls = []

        def failing_runner(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                bootstrap_calls.append(cmd)
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        log_dir2 = tmp_path / "logs2"
        result = la.register(
            repo_root=repo_root, log_dir=log_dir2, home=home,
            uid=501, runner=failing_runner,
        )

        assert result.ok is False
        assert result.action == "error"
        assert result.error
        # plist は書き込み前の内容へロールバックされている
        assert plist.read_bytes() == original_bytes
        # 新規内容での bootstrap 失敗 + ロールバック後の再ロード試行の 2 回
        assert len(bootstrap_calls) == 2
        # 再ロードも失敗しているので、その旨が明示されている(silent 「無害」
        # 扱いにしない)
        assert "could not be reloaded" in result.error
        assert "launchctl bootstrap" in result.error

    def test_recovery_hint_quotes_path_with_spaces(
        self, repo_root, tmp_path, rumps_available,
    ):
        """home ディレクトリにスペースが含まれる場合(実環境でも起こりうる)、
        recovery 失敗時の「run manually: launchctl bootstrap ...」案内文の
        パス部分が shlex.quote で引用符付きになる(そのままコピペ実行しても
        word-split しない)。"""
        home = tmp_path / "home dir with spaces"
        home.mkdir()
        log_dir1 = tmp_path / "logs1"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir1, home=home,
            uid=501, runner=runner1,
        )
        plist = la.plist_path(home)

        def failing_runner(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        log_dir2 = tmp_path / "logs2"
        result = la.register(
            repo_root=repo_root, log_dir=log_dir2, home=home,
            uid=501, runner=failing_runner,
        )

        assert result.ok is False
        assert "could not be reloaded" in result.error
        expected_quoted = la.shlex.quote(str(plist))
        # 前提: このテストのパスは実際にスペースを含み、クォートが必要
        assert expected_quoted != str(plist)
        assert expected_quoted in result.error

    def test_recovery_bootstrap_succeeds_after_update_failure(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        """update 失敗時、ロールバック後の再ロードが成功すれば警告文は付かない。"""
        log_dir1 = tmp_path / "logs1"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir1, home=home,
            uid=501, runner=runner1,
        )
        plist = la.plist_path(home)
        original_bytes = plist.read_bytes()

        bootstrap_call_count = {"n": 0}

        def flaky_runner(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                bootstrap_call_count["n"] += 1
                if bootstrap_call_count["n"] == 1:
                    # 新しい内容での bootstrap は失敗
                    return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
                # ロールバック後の再ロードは成功
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        log_dir2 = tmp_path / "logs2"
        result = la.register(
            repo_root=repo_root, log_dir=log_dir2, home=home,
            uid=501, runner=flaky_runner,
        )

        assert result.ok is False  # 新しい内容への更新自体は失敗のまま
        assert plist.read_bytes() == original_bytes  # ロールバック済み
        assert bootstrap_call_count["n"] == 2
        assert "could not be reloaded" not in (result.error or "")

    def test_rollback_removes_plist_when_no_previous_content(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        bootstrap_calls = []

        def failing_runner(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                bootstrap_calls.append(cmd)
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        result = la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=failing_runner,
        )

        assert result.ok is False
        # 初回登録の失敗 → 元々存在しなかったので plist は残らない
        assert not la.plist_path(home).exists()
        # 新規登録の失敗では直前に bootout していないため、再ロード試行はしない
        # (recovery bootstrap は発生しない、失敗した 1 回のみ)
        assert len(bootstrap_calls) == 1
        assert "could not be reloaded" not in (result.error or "")

    def test_rollback_on_bootstrap_exception(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        import subprocess as _sp

        def raising_runner(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                raise _sp.TimeoutExpired(cmd, 10)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        result = la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=raising_runner,
        )
        assert result.ok is False
        assert result.error
        assert not la.plist_path(home).exists()


# ---------------------------------------------------------------------------
# register: ファイル書き込み失敗のエラーハンドリング (B-156 追補)
#
# ログディレクトリ作成・chmod・plist 書き込みは、以前は bootstrap 呼び出し
# の try/except の外側にあり、OSError が捕捉されずに register() 全体から
# 送出されていた(呼び出し元の setup.py::step_menubar() がクラッシュし、
# setup 全体が中断する)。ここでは OSError が例外として伝播せず、
# RegisterResult(ok=False, action="error") として返ることを確認する。
# ---------------------------------------------------------------------------


class TestRegisterFileWriteFailure:
    def test_log_dir_chmod_failure_returns_error_result_without_raising(
        self, repo_root, home, tmp_path, rumps_available, monkeypatch,
    ):
        """新規登録(bootout 未実施)時、ログディレクトリの chmod が OSError
        を送出しても register() は例外を送出せず error 結果を返す。"""
        def raising_chmod(path, mode):
            raise OSError("permission denied (test)")

        monkeypatch.setattr(la.os, "chmod", raising_chmod)
        runner = _fake_runner()

        result = la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )

        assert result.ok is False
        assert result.action == "error"
        assert "permission denied" in (result.error or "")
        # bootstrap 自体は呼ばれていない(ログディレクトリ確保の時点で中断)
        assert runner.calls == []

    def test_atomic_write_failure_returns_error_result_without_raising(
        self, repo_root, home, tmp_path, rumps_available, monkeypatch,
    ):
        """内容変更(update, 既に bootout 済み)時、plist 書き込みが OSError
        を送出しても register() は例外を送出せず error 結果を返す。"""
        log_dir1 = tmp_path / "logs1"
        runner1 = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=log_dir1, home=home,
            uid=501, runner=runner1,
        )
        plist = la.plist_path(home)
        original_bytes = plist.read_bytes()

        def raising_atomic_write(path, data):
            raise OSError("disk full (test)")

        monkeypatch.setattr(la, "_atomic_write_bytes", raising_atomic_write)
        bootstrap_calls = []

        def runner2(cmd, **kwargs):
            if cmd[1] == "bootstrap":
                bootstrap_calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        log_dir2 = tmp_path / "logs2"
        result = la.register(
            repo_root=repo_root, log_dir=log_dir2, home=home,
            uid=501, runner=runner2,
        )

        assert result.ok is False
        assert result.action == "error"
        assert "disk full" in (result.error or "")
        # 書き込みが常に失敗するモックのため、ロールバック用の再書き込みも
        # 失敗するが、例外は伝播しない。元の plist ファイルは変更されない。
        assert plist.read_bytes() == original_bytes
        # ベストエフォートで旧定義の再ロード(recovery bootstrap)を試みている
        assert len(bootstrap_calls) == 1


# ---------------------------------------------------------------------------
# register: dry-run
# ---------------------------------------------------------------------------


class TestRegisterDryRun:
    def test_dry_run_writes_nothing_and_calls_nothing(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        calls = []
        runner = _fake_runner(calls=calls)
        log_dir = tmp_path / "logs"

        result = la.register(
            repo_root=repo_root, log_dir=log_dir, home=home,
            uid=501, runner=runner, dry_run=True,
        )

        assert result.ok is True
        assert result.dry_run is True
        assert result.action == "install"
        assert not la.plist_path(home).exists()
        assert not log_dir.exists()
        assert calls == []


# ---------------------------------------------------------------------------
# unregister
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_noop_when_plist_missing(self, home):
        calls = []
        runner = _fake_runner(calls=calls)

        result = la.unregister(home=home, uid=501, runner=runner)

        assert result.ok is True
        assert result.changed is False
        assert result.action == "noop"
        assert calls == []

    def test_removes_plist_and_calls_bootout(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        runner = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )
        plist = la.plist_path(home)
        assert plist.exists()

        calls = []
        runner2 = _fake_runner(calls=calls)
        result = la.unregister(home=home, uid=501, runner=runner2)

        assert result.ok is True
        assert result.changed is True
        assert result.action == "removed"
        assert not plist.exists()
        assert calls == [(["launchctl", "bootout", f"gui/501/{la.LABEL}"], calls[0][1])]

    def test_dry_run_does_not_remove_or_call(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        runner = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )
        plist = la.plist_path(home)

        calls = []
        runner2 = _fake_runner(calls=calls)
        result = la.unregister(home=home, uid=501, runner=runner2, dry_run=True)

        assert result.ok is True
        assert result.dry_run is True
        assert plist.exists()
        assert calls == []

    def test_ignores_bootout_failure_and_still_removes_file(
        self, repo_root, home, tmp_path, rumps_available,
    ):
        runner = _fake_runner()
        la.register(
            repo_root=repo_root, log_dir=tmp_path / "logs", home=home,
            uid=501, runner=runner,
        )
        plist = la.plist_path(home)

        def failing_bootout(cmd, **kwargs):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="not loaded")

        result = la.unregister(home=home, uid=501, runner=failing_bootout)

        assert result.ok is True  # 未ロード等は無視して冪等に成功扱い
        assert not plist.exists()


# ---------------------------------------------------------------------------
# rumps 可用性チェックが doctor._resolve_rumps_check を再利用していること
# ---------------------------------------------------------------------------


class TestRumpsAvailabilityDelegation:
    def test_delegates_to_doctor_resolve_rumps_check(self, repo_root, monkeypatch):
        called = {}

        def fake_resolve(project_dir):
            called["project_dir"] = project_dir
            return _doctor._deps.CheckResult(name="rumps", found=True)

        monkeypatch.setattr(_doctor, "_resolve_rumps_check", fake_resolve)

        assert la._rumps_available(repo_root) is True
        assert called["project_dir"] == repo_root

    def test_false_when_doctor_reports_not_found(self, repo_root, monkeypatch):
        monkeypatch.setattr(
            _doctor, "_resolve_rumps_check",
            lambda project_dir: _doctor._deps.CheckResult(name="rumps", found=False),
        )
        assert la._rumps_available(repo_root) is False
