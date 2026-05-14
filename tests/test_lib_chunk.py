"""lib/chunk.sh の単体テスト (S-011)

bash 関数を subprocess 経由で source して呼び出すスタイル
(test_lib_log.py / test_lib_playback.py と同じ流儀)。
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_CHUNK = REPO / "scripts" / "lib" / "chunk.sh"
_venv_python = REPO / ".venv" / "bin" / "python"
PYTHON = str(_venv_python) if _venv_python.exists() else "python3"


def run_chunk_split(text: str, speaker: str = "3", python: str = PYTHON):
    full = (
        f'source "{LIB_CHUNK}"; '
        f'vvread_chunk_split "{text}" "{speaker}" "{python}" "{REPO}/scripts"'
    )
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True)


def test_chunk_split_normal():
    """正常テキストがチャンクを返す"""
    result = run_chunk_split("テスト")
    assert result.returncode == 0
    assert result.stdout.strip() != ""


def test_chunk_split_invalid_python():
    """無効な python バイナリは空出力（|| true で吸収）"""
    result = run_chunk_split("テスト", python="/nonexistent/python")
    assert result.returncode == 0  # || true で常に0
    assert result.stdout.strip() == ""
