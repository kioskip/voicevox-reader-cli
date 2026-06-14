"""lib/duration.sh の単体テスト (FB-4 / B-145)

vvread_parse_duration: 30s/10m/2h/7d を秒に変換、不正値は非ゼロ。
voice.sh の旧 _parse_duration（s/m/h）を一般化し d を追加したもの。
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_DURATION = REPO / "scripts" / "lib" / "duration.sh"


def parse(value: str):
    full = f'source "{LIB_DURATION}"; vvread_parse_duration "{value}"; echo "rc=$?"'
    r = subprocess.run(["bash", "-c", full], capture_output=True, text=True, timeout=10)
    return r.stdout


@pytest.mark.parametrize("value,seconds", [
    ("30s", 30),
    ("10m", 600),
    ("2h", 7200),
    ("7d", 604800),
    ("0s", 0),
])
def test_accept(value, seconds):
    out = parse(value)
    assert f"{seconds}\n" in out, out
    assert "rc=0" in out


@pytest.mark.parametrize("value", ["", "abc", "-1d", "1.5h", "10x", "5", "s", "1 0s"])
def test_reject(value):
    out = parse(value)
    assert "rc=1" in out, out
