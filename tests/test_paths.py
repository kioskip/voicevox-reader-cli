"""scripts/paths.py + scripts/lib_paths.sh のテスト (R-001)

Python 単体: macOS / Linux 既定 + 環境変数 override。
両者一致: 現在 OS で bash を起動して Python と同じ出力になることを fix。

bash と Python が divergence しないようにするのが本テストの主眼。
"""
import os
import platform
import subprocess
from pathlib import Path

import pytest

import paths  # via conftest.py sys.path

REPO = Path(__file__).resolve().parent.parent
LIB_PATHS = REPO / "scripts" / "lib" / "paths.sh"


# -------------------- helpers --------------------

_PATH_ENV_KEYS = (
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "VVREAD_STATE_DIR",
    "VVREAD_LOG_DIR",
    "VVREAD_CACHE_DIR",
)


def run_bash(env_overrides: dict, fn: str) -> str:
    """env を export した上で lib_paths.sh を source し、関数 fn を呼ぶ。

    親プロセスが既に持っている VVREAD_* / XDG_* は意図的にクリアして
    渡された env_overrides だけを反映する。
    """
    base = os.environ.copy()
    for key in _PATH_ENV_KEYS:
        base.pop(key, None)
    base.update(env_overrides)
    cmd = f'source "{LIB_PATHS}"; {fn}'
    result = subprocess.run(
        ["bash", "-c", cmd],
        env=base,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def clean_env(monkeypatch):
    """resolver 関連の env を全部クリア。各テストが必要なものだけ setenv する。"""
    for key in _PATH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# -------------------- Python 単体: macOS --------------------


class TestPythonMacos:
    @pytest.fixture(autouse=True)
    def _macos(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setenv("HOME", "/Users/foo")

    def test_state_dir(self, clean_env):
        assert paths.state_dir() == Path("/Users/foo/Library/Application Support/vvread")

    def test_log_dir(self, clean_env):
        assert paths.log_dir() == Path("/Users/foo/Library/Logs/vvread")

    def test_cache_dir(self, clean_env):
        assert paths.cache_dir() == Path("/Users/foo/Library/Caches/vvread")

    def test_xdg_ignored_on_macos(self, clean_env, monkeypatch):
        # macOS では XDG_STATE_HOME を読まない
        monkeypatch.setenv("XDG_STATE_HOME", "/var/state")
        assert paths.state_dir() == Path("/Users/foo/Library/Application Support/vvread")


# -------------------- Python 単体: Linux --------------------


class TestPythonLinux:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("HOME", "/home/foo")

    def test_state_default(self, clean_env):
        assert paths.state_dir() == Path("/home/foo/.local/state/vvread")

    def test_state_xdg(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "/var/state")
        assert paths.state_dir() == Path("/var/state/vvread")

    def test_log_default(self, clean_env):
        # log は state 配下に置く(2026/05/06 確定、XDG_STATE_HOME を動かせばまるごと移動できる)
        assert paths.log_dir() == Path("/home/foo/.local/state/vvread/logs")

    def test_log_xdg(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", "/var/state")
        assert paths.log_dir() == Path("/var/state/vvread/logs")

    def test_cache_default(self, clean_env):
        assert paths.cache_dir() == Path("/home/foo/.cache/vvread")

    def test_cache_xdg(self, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", "/var/cache")
        assert paths.cache_dir() == Path("/var/cache/vvread")

    def test_xdg_state_does_not_affect_cache(self, clean_env, monkeypatch):
        # state と cache は別 env で切る(独立性)
        monkeypatch.setenv("XDG_STATE_HOME", "/var/state")
        assert paths.cache_dir() == Path("/home/foo/.cache/vvread")


# -------------------- Python 単体: env override --------------------


class TestPythonOverride:
    def test_state_override_absolute(self, clean_env, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", "/custom/state")
        assert paths.state_dir() == Path("/custom/state")

    def test_log_override_absolute(self, clean_env, monkeypatch):
        monkeypatch.setenv("VVREAD_LOG_DIR", "/custom/log")
        assert paths.log_dir() == Path("/custom/log")

    def test_cache_override_absolute(self, clean_env, monkeypatch):
        monkeypatch.setenv("VVREAD_CACHE_DIR", "/custom/cache")
        assert paths.cache_dir() == Path("/custom/cache")

    def test_override_with_tilde(self, clean_env, monkeypatch):
        monkeypatch.setenv("HOME", "/home/foo")
        monkeypatch.setenv("VVREAD_STATE_DIR", "~/myvvread")
        assert paths.state_dir() == Path("/home/foo/myvvread")

    def test_override_trailing_slash_stripped(self, clean_env, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", "/custom/state/")
        # Path() が末尾スラッシュを正規化する
        assert paths.state_dir() == Path("/custom/state")

    def test_empty_override_falls_back_to_default(self, clean_env, monkeypatch):
        # 空文字は未設定扱い(bash の `[ -n "${var:-}" ]` 挙動と整合)
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("HOME", "/home/foo")
        monkeypatch.setenv("VVREAD_STATE_DIR", "")
        assert paths.state_dir() == Path("/home/foo/.local/state/vvread")

    def test_override_takes_precedence_over_xdg(self, clean_env, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("HOME", "/home/foo")
        monkeypatch.setenv("XDG_STATE_HOME", "/var/state")
        monkeypatch.setenv("VVREAD_STATE_DIR", "/custom")
        assert paths.state_dir() == Path("/custom")


# -------------------- Python CLI --------------------


class TestPythonCli:
    def test_cli_state(self, clean_env, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", "/cli/state")
        result = subprocess.run(
            ["python3", str(REPO / "scripts" / "paths.py"), "state"],
            env={**os.environ, "VVREAD_STATE_DIR": "/cli/state"},
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "/cli/state"

    def test_cli_invalid_arg(self, clean_env):
        result = subprocess.run(
            ["python3", str(REPO / "scripts" / "paths.py"), "bogus"],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "usage" in result.stderr


# -------------------- bash と Python の一致 --------------------
# 現在 OS で実行: macOS なら macOS パス、Linux なら Linux パス。
# uname -s は subprocess で実 OS を返すため、Python 側も実 OS で計算したものと比較する。


def _expected_with_clean_env(fn, env_overrides):
    """Python 側の期待値を、clean な VVREAD/XDG 状態で計算する"""
    saved = {key: os.environ.get(key) for key in _PATH_ENV_KEYS}
    saved["HOME"] = os.environ.get("HOME")
    try:
        for key in _PATH_ENV_KEYS:
            os.environ.pop(key, None)
        for k, v in env_overrides.items():
            os.environ[k] = v
        return str(fn())
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class TestBashPythonParity:
    """bash と Python が同一入力で同一出力を返すことを fix。"""

    @pytest.mark.parametrize("fn_name,py_fn", [
        ("vvread_state_dir", paths.state_dir),
        ("vvread_log_dir", paths.log_dir),
        ("vvread_cache_dir", paths.cache_dir),
    ])
    def test_default(self, fn_name, py_fn):
        env = {"HOME": "/tmp/parity_home"}
        py_out = _expected_with_clean_env(py_fn, env)
        bash_out = run_bash(env, fn_name)
        assert py_out == bash_out

    @pytest.mark.parametrize("fn_name,env_var,py_fn", [
        ("vvread_state_dir", "VVREAD_STATE_DIR", paths.state_dir),
        ("vvread_log_dir",   "VVREAD_LOG_DIR",   paths.log_dir),
        ("vvread_cache_dir", "VVREAD_CACHE_DIR", paths.cache_dir),
    ])
    def test_override_absolute(self, fn_name, env_var, py_fn):
        env = {"HOME": "/tmp/parity_home", env_var: "/custom/path"}
        py_out = _expected_with_clean_env(py_fn, env)
        bash_out = run_bash(env, fn_name)
        assert py_out == bash_out

    @pytest.mark.parametrize("fn_name,env_var,py_fn", [
        ("vvread_state_dir", "VVREAD_STATE_DIR", paths.state_dir),
        ("vvread_log_dir",   "VVREAD_LOG_DIR",   paths.log_dir),
        ("vvread_cache_dir", "VVREAD_CACHE_DIR", paths.cache_dir),
    ])
    def test_override_tilde(self, fn_name, env_var, py_fn):
        env = {"HOME": "/tmp/parity_home", env_var: "~/custom"}
        py_out = _expected_with_clean_env(py_fn, env)
        bash_out = run_bash(env, fn_name)
        assert py_out == bash_out
        assert "/tmp/parity_home/custom" == bash_out  # sanity

    @pytest.mark.parametrize("fn_name,env_var,py_fn", [
        ("vvread_state_dir", "VVREAD_STATE_DIR", paths.state_dir),
        ("vvread_log_dir",   "VVREAD_LOG_DIR",   paths.log_dir),
        ("vvread_cache_dir", "VVREAD_CACHE_DIR", paths.cache_dir),
    ])
    def test_override_trailing_slash(self, fn_name, env_var, py_fn):
        env = {"HOME": "/tmp/parity_home", env_var: "/custom/path/"}
        py_out = _expected_with_clean_env(py_fn, env)
        bash_out = run_bash(env, fn_name)
        assert py_out == bash_out
        assert not bash_out.endswith("/")  # sanity: 末尾スラッシュ無し

    # XDG_STATE_HOME / XDG_CACHE_HOME は macOS では無視されるので Linux 系のみ
    @pytest.mark.skipif(platform.system() == "Darwin",
                        reason="macOS は XDG を読まない")
    @pytest.mark.parametrize("fn_name,xdg_var,py_fn", [
        ("vvread_state_dir", "XDG_STATE_HOME", paths.state_dir),
        ("vvread_log_dir",   "XDG_STATE_HOME", paths.log_dir),
        ("vvread_cache_dir", "XDG_CACHE_HOME", paths.cache_dir),
    ])
    def test_xdg_set(self, fn_name, xdg_var, py_fn):
        env = {"HOME": "/tmp/parity_home", xdg_var: "/var/xdg"}
        py_out = _expected_with_clean_env(py_fn, env)
        bash_out = run_bash(env, fn_name)
        assert py_out == bash_out

    def test_empty_override_treated_as_unset(self):
        env = {"HOME": "/tmp/parity_home", "VVREAD_STATE_DIR": ""}
        py_out = _expected_with_clean_env(paths.state_dir, env)
        bash_out = run_bash(env, "vvread_state_dir")
        assert py_out == bash_out


# -------------------- vvread_migrate_legacy_tmp (R-003) --------------------
# 旧 ${PROJECT_DIR}/tmp/ から OS 別 dir への移行を検証する。
# bash 関数なので subprocess 経由で呼ぶ。


def run_migrate(env_overrides: dict, legacy_dir: str) -> subprocess.CompletedProcess:
    """vvread_migrate_legacy_tmp を呼んだ subprocess の結果を返す"""
    base = os.environ.copy()
    for key in _PATH_ENV_KEYS:
        base.pop(key, None)
    base.update(env_overrides)
    cmd = (
        f'source "{LIB_PATHS}"; '
        f'vvread_migrate_legacy_tmp "{legacy_dir}"'
    )
    return subprocess.run(
        ["bash", "-c", cmd],
        env=base,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def migrate_setup(tmp_path):
    """legacy / state / log / cache の 4 ディレクトリを用意する fixture。
    各テストは legacy 配下に旧ファイルを置いて移行を検証する。"""
    legacy = tmp_path / "legacy_tmp"
    state = tmp_path / "state"
    log = tmp_path / "log"
    cache = tmp_path / "cache"
    legacy.mkdir()
    # state / log / cache は migration 関数が必要な時に作る前提なので事前 mkdir しない
    return {
        "legacy": legacy,
        "state": state,
        "log": log,
        "cache": cache,
        "env": {
            "VVREAD_STATE_DIR": str(state),
            "VVREAD_LOG_DIR": str(log),
            "VVREAD_CACHE_DIR": str(cache),
        },
    }


class TestMigrateLegacyTmp:
    def test_no_legacy_dir_is_noop(self, migrate_setup, tmp_path):
        """legacy ディレクトリが無ければ何もしない(エラーにならない)"""
        result = run_migrate(migrate_setup["env"], str(tmp_path / "nonexistent"))
        assert result.returncode == 0
        # state も作られない
        assert not migrate_setup["state"].exists()

    def test_migrates_state_files(self, migrate_setup):
        """disabled / mute_until / last_notify が state へコピーされる"""
        legacy = migrate_setup["legacy"]
        (legacy / "disabled").touch()
        (legacy / "mute_until").write_text("9999999999")
        (legacy / "last_notify").write_text("100")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        state = migrate_setup["state"]
        assert (state / "disabled").exists()
        assert (state / "mute_until").read_text() == "9999999999"
        assert (state / "last_notify").read_text() == "100"

        # 旧側は残る(ロールバック余地)
        assert (legacy / "disabled").exists()
        assert (legacy / "mute_until").exists()
        assert (legacy / "last_notify").exists()

    def test_does_not_overwrite_existing_state_file(self, migrate_setup):
        """新側に既存があれば上書きしない(後勝ちの事故を防ぐ)"""
        legacy = migrate_setup["legacy"]
        state = migrate_setup["state"]
        state.mkdir()
        (legacy / "disabled").touch()
        (legacy / "mute_until").write_text("OLD")
        (state / "mute_until").write_text("NEW")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        # disabled は新側に無かったので migrate される
        assert (state / "disabled").exists()
        # mute_until は既に新側にあったので保持
        assert (state / "mute_until").read_text() == "NEW"

    def test_migrates_cache_wav(self, migrate_setup):
        """cache/*.wav が cache_dir にコピーされる"""
        legacy = migrate_setup["legacy"]
        (legacy / "cache").mkdir()
        (legacy / "cache" / "id74_a.wav").write_bytes(b"AAA")
        (legacy / "cache" / "id74_b.wav").write_bytes(b"BBB")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        cache = migrate_setup["cache"]
        assert (cache / "id74_a.wav").read_bytes() == b"AAA"
        assert (cache / "id74_b.wav").read_bytes() == b"BBB"
        # 旧側は残る
        assert (legacy / "cache" / "id74_a.wav").exists()

    def test_partial_cache_migration(self, migrate_setup):
        """新 cache に既存ファイルがあれば上書きしない、無いものだけ移行"""
        legacy = migrate_setup["legacy"]
        cache = migrate_setup["cache"]
        cache.mkdir()
        (legacy / "cache").mkdir()
        (legacy / "cache" / "old.wav").write_bytes(b"OLD")
        (legacy / "cache" / "common.wav").write_bytes(b"FROM_LEGACY")
        (cache / "common.wav").write_bytes(b"FROM_NEW")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        # 新側に無かった old.wav は移行
        assert (cache / "old.wav").read_bytes() == b"OLD"
        # 既存は保持
        assert (cache / "common.wav").read_bytes() == b"FROM_NEW"

    def test_migrates_log(self, migrate_setup):
        """logs/speak.log が log_dir/speak.log にコピーされる"""
        legacy = migrate_setup["legacy"]
        (legacy / "logs").mkdir()
        (legacy / "logs" / "speak.log").write_text("[..] old log line\n")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        log = migrate_setup["log"] / "speak.log"
        assert log.exists()
        assert "old log line" in log.read_text()

    def test_does_not_overwrite_existing_log(self, migrate_setup):
        """新 log に既存があれば legacy log を移行しない(append 状態を保護)"""
        legacy = migrate_setup["legacy"]
        log_dir = migrate_setup["log"]
        log_dir.mkdir()
        (log_dir / "speak.log").write_text("NEW log\n")
        (legacy / "logs").mkdir()
        (legacy / "logs" / "speak.log").write_text("OLD log\n")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        # 新側保持
        assert (log_dir / "speak.log").read_text() == "NEW log\n"
        # 旧側残存
        assert (legacy / "logs" / "speak.log").exists()

    def test_idempotent_second_run(self, migrate_setup):
        """2 回呼び出しても破壊しない(冪等性)"""
        legacy = migrate_setup["legacy"]
        (legacy / "disabled").touch()
        (legacy / "cache").mkdir()
        (legacy / "cache" / "x.wav").write_bytes(b"X")

        for _ in range(2):
            result = run_migrate(migrate_setup["env"], str(legacy))
            assert result.returncode == 0, result.stderr

        assert (migrate_setup["state"] / "disabled").exists()
        assert (migrate_setup["cache"] / "x.wav").read_bytes() == b"X"

    def test_session_files_are_not_migrated(self, migrate_setup):
        """session.id / playing.pid / voice_*.wav は per-session で移行対象外"""
        legacy = migrate_setup["legacy"]
        (legacy / "session.id").write_text("OLD_SESSION")
        (legacy / "playing.pid").write_text("12345")
        (legacy / "voice_OLD_0.wav").write_bytes(b"chunk")

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr

        state = migrate_setup["state"]
        # state ディレクトリは作られない可能性もあるので、ファイル単位で確認
        assert not (state / "session.id").exists()
        assert not (state / "playing.pid").exists()
        assert not (state / "voice_OLD_0.wav").exists()

    def test_empty_cache_dir_is_noop(self, migrate_setup):
        """legacy/cache/ が空ディレクトリの場合、新 cache を作らない"""
        legacy = migrate_setup["legacy"]
        (legacy / "cache").mkdir()  # 空のまま

        result = run_migrate(migrate_setup["env"], str(legacy))
        assert result.returncode == 0, result.stderr
        # cache_dir は作られない(コピー対象が無いため)
        assert not migrate_setup["cache"].exists()
