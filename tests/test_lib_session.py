"""lib/session.sh の単体テスト (S-011)

bash 関数を subprocess 経由で source して呼び出すスタイル
(test_lib_log.py / test_lib_playback.py と同じ流儀)。
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_SESSION = REPO / "scripts" / "lib" / "session.sh"
LIB_LOG = REPO / "scripts" / "lib" / "log.sh"


def run_bash(script: str, tmp_path):
    """log.sh + session.sh を source してスクリプトを実行"""
    full = (
        f'set -euo pipefail; '
        f'LOG_DIR="{tmp_path}"; '
        f'source "{LIB_LOG}"; '
        f'source "{LIB_SESSION}"; '
        f'{script}'
    )
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True)


def test_session_start_stdout_nonempty(tmp_path):
    """vvread_session_start がstdoutにIDを返す"""
    result = run_bash(f'vvread_session_start "{tmp_path}/session.id"', tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() != ""


def test_session_start_writes_file(tmp_path):
    """vvread_session_start がファイルに書き込み、stdoutと一致する"""
    session_file = tmp_path / "session.id"
    result = run_bash(f'vvread_session_start "{session_file}"', tmp_path)
    assert result.returncode == 0
    stdout_id = result.stdout.strip()
    assert session_file.read_text().strip() == stdout_id


def test_session_is_current_match(tmp_path):
    """一致するIDはreturn 0"""
    session_file = tmp_path / "session.id"
    result = run_bash(
        f'sid=$(vvread_session_start "{session_file}"); '
        f'vvread_session_is_current "{session_file}" "${{sid}}"',
        tmp_path,
    )
    assert result.returncode == 0


def test_session_is_current_mismatch(tmp_path):
    """不一致のIDはreturn 1"""
    session_file = tmp_path / "session.id"
    result = run_bash(
        f'vvread_session_start "{session_file}" >/dev/null; '
        f'vvread_session_is_current "{session_file}" "DIFFERENT_ID"',
        tmp_path,
    )
    assert result.returncode != 0


def test_session_is_current_missing_file(tmp_path):
    """ファイル不存在はreturn 1"""
    result = run_bash(
        f'vvread_session_is_current "{tmp_path}/nonexistent.id" "ANY_ID"',
        tmp_path,
    )
    assert result.returncode != 0
