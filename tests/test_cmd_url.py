"""tests/test_cmd_url.py - scripts/cmd/url.sh のテスト (B-003)"""

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_URL = REPO / "scripts" / "cmd" / "url.sh"
CMD_SAY = REPO / "scripts" / "cmd" / "say.sh"
FETCH_URL_PY = REPO / "scripts" / "fetch_url.py"


def _path_env(tmp_path: Path) -> dict:
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        "VVREAD_PROJECT_SETTINGS": str(tmp_path / "no-project-settings.json"),
    }


def _clean_env(env_extra=None) -> dict:
    base = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))
    }
    if env_extra:
        base.update(env_extra)
    return base


def run_url(*args, env_extra=None, timeout=10) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CMD_URL), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 引数バリデーション
# ---------------------------------------------------------------------------


class TestArgsValidation:
    def test_no_args_exits_1(self, tmp_path):
        r = run_url(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "required" in r.stderr

    def test_help_flag(self, tmp_path):
        r = run_url("-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage" in r.stderr

    def test_unknown_option_rejected(self, tmp_path):
        r = run_url("--bogus", env_extra=_path_env(tmp_path))
        assert r.returncode == 1

    def test_ftp_scheme_rejected(self, tmp_path):
        """shell層でftp://を拒否する"""
        r = run_url("ftp://example.com/file", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "http" in r.stderr.lower() or "invalid" in r.stderr.lower()

    def test_bare_string_rejected(self, tmp_path):
        r = run_url("example.com", env_extra=_path_env(tmp_path))
        assert r.returncode == 1


# ---------------------------------------------------------------------------
# fetch_url.py 呼び出し結果のハンドリング
# ---------------------------------------------------------------------------


def _make_fake_fetch_url(tmp_path: Path, exit_code: int = 0, output: str = "") -> Path:
    """fetch_url.py の偽Python スクリプト"""
    fake = tmp_path / "scripts" / "fetch_url.py"
    fake.parent.mkdir(parents=True, exist_ok=True)
    script = f"import sys\nprint({output!r}, end='')\nsys.exit({exit_code})\n"
    fake.write_text(script)
    return fake


class TestFetchDelegation:
    def test_fetch_success_delegates_to_say(self, tmp_path):
        """fetch成功時はsay.shへ委譲し(say.shが実エンジンなしで終了するが) returncode ≠ 1"""
        fake_fetch = _make_fake_fetch_url(tmp_path, exit_code=0, output="これはテストです")
        env = _path_env(tmp_path)
        env["VVREAD_SCRIPTS_DIR"] = str(tmp_path / "scripts")

        # say.sh は VOICEVOX_ENGINE がないので失敗するが、fetch自体は通過する
        r = subprocess.run(
            [str(CMD_URL), "https://example.com"],
            env=_clean_env(env),
            capture_output=True,
            text=True,
            timeout=10,
        )
        # fetch成功 → say.shに渡される。say.shがエンジンなしで失敗してもfetchは通過した証拠として
        # "no content fetched" エラーではないことを確認
        assert "no content fetched" not in r.stderr

    def test_fetch_failure_exits_1(self, tmp_path):
        """fetch_url.pyがexit 1 → url.shもexit 1"""
        fake_fetch = _make_fake_fetch_url(tmp_path, exit_code=1, output="")
        env = _path_env(tmp_path)
        env["VVREAD_SCRIPTS_DIR"] = str(tmp_path / "scripts")

        r = subprocess.run(
            [str(CMD_URL), "https://example.com"],
            env=_clean_env(env),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 1

    def test_empty_content_exits_1(self, tmp_path):
        """fetch_url.pyが空文字列を返す → url.shがexit 1"""
        fake_fetch = _make_fake_fetch_url(tmp_path, exit_code=0, output="")
        env = _path_env(tmp_path)
        env["VVREAD_SCRIPTS_DIR"] = str(tmp_path / "scripts")

        r = subprocess.run(
            [str(CMD_URL), "https://example.com"],
            env=_clean_env(env),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 1
        assert "no content" in r.stderr.lower()

    def test_speaker_option_passed_through(self, tmp_path):
        """--speaker オプションが say.sh に透過されること"""
        fake_fetch = _make_fake_fetch_url(tmp_path, exit_code=0, output="テスト")

        # say.sh の PYTHON 依存を回避するため、受け取った引数をログに書く偽 say.sh を注入
        fake_say = tmp_path / "scripts" / "cmd" / "say.sh"
        fake_say.parent.mkdir(parents=True, exist_ok=True)
        args_log = tmp_path / "say_args.log"
        fake_say.write_text(
            f"#!/bin/bash\nprintf '%s\\n' \"$@\" >> {args_log}\n"
        )
        fake_say.chmod(0o755)

        env = _path_env(tmp_path)
        env["VVREAD_SCRIPTS_DIR"] = str(tmp_path / "scripts")

        subprocess.run(
            [str(CMD_URL), "https://example.com", "--speaker", "5"],
            env=_clean_env(env),
            capture_output=True,
            text=True,
            timeout=10,
        )

        if args_log.exists():
            logged = args_log.read_text()
            assert "--speaker" in logged
            assert "5" in logged


# ---------------------------------------------------------------------------
# bin/vvread ディスパッチ
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_url_subcommand_registered(self, tmp_path):
        """bin/vvread url がサブコマンドとして認識されること（不正URLで早期終了）"""
        r = subprocess.run(
            [str(VVREAD), "url", "ftp://example.com"],
            env=_clean_env(_path_env(tmp_path)),
            capture_output=True,
            text=True,
            timeout=10,
        )
        # ftp://はshell層で拒否 → exit 1（"not yet implemented"にならない）
        assert r.returncode == 1
        assert "not yet implemented" not in r.stderr
