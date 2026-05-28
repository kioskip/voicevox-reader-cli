"""tests/test_cache_key.py - cache_patterns.normalize + cache_key.py CLI テスト (R-112)"""
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from cache_patterns import normalize  # noqa: E402


# ===========================================================================
# cache_patterns.normalize — 正例・負例ペア
# ===========================================================================

# 「以下省略」パターン
def test_truncation_suffix_exact_match():
    assert normalize("(以下省略)", 3) == "(以下省略)"


def test_truncation_suffix_no_partial():
    assert normalize("本文(以下省略)", 3) is None


# git commit パターン（既存）
def test_git_commit_match():
    assert normalize("abc123 で commit。", 3) is not None


def test_git_commit_no_partial():
    assert normalize("前置詞 abc123 で commit。", 3) is None


# OK / 了解パターン（既存）
def test_ok_match():
    assert normalize("OK。", 3) is not None


def test_ok_in_sentence():
    assert normalize("これはOKです。", 3) is None


# ===========================================================================
# cache_key.py CLI — ハッシュ生成テスト
# ===========================================================================

PYTHON = str(ROOT / ".venv" / "bin" / "python")
if not Path(PYTHON).exists():
    PYTHON = "python3"

CACHE_KEY_PY = str(SCRIPTS_DIR / "cache_key.py")


def _run_cache_key(
    text: str,
    speaker: int,
    env: Optional[Dict[str, str]] = None,
    extra_args: Optional[list] = None,
) -> str:
    """cache_key.py を実行して stdout の最初の行を返す（空文字はキャッシュなし）。"""
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(
        [PYTHON, CACHE_KEY_PY, "--speaker", str(speaker)] + (extra_args or []),
        input=text,
        capture_output=True,
        text=True,
        env=merged,
    )
    assert result.returncode == 0, f"cache_key.py failed: {result.stderr}"
    return result.stdout.strip()


def test_cache_key_ok_returns_key():
    key = _run_cache_key("OK。", 3)
    assert key.startswith("spk3_")
    assert len(key) == len("spk3_") + 8


def test_cache_key_non_pattern_returns_empty():
    key = _run_cache_key("これはキャッシュ対象外です。", 3)
    assert key == ""


def test_cache_key_truncation_suffix_returns_key():
    key = _run_cache_key("(以下省略)", 3)
    assert key.startswith("spk3_")


def test_cache_key_different_speed_changes_hash():
    key1 = _run_cache_key("OK。", 3, {"VOICEVOX_SPEED": "1.0"})
    key2 = _run_cache_key("OK。", 3, {"VOICEVOX_SPEED": "1.5"})
    assert key1 != key2, "speed が異なれば hash が変わるはず"


def test_cache_key_different_pitch_changes_hash():
    key1 = _run_cache_key("OK。", 3, {"VOICEVOX_PITCH": "0.0"})
    key2 = _run_cache_key("OK。", 3, {"VOICEVOX_PITCH": "0.1"})
    assert key1 != key2, "pitch が異なれば hash が変わるはず"


def test_cache_key_no_slash_or_newline():
    key = _run_cache_key("了解。", 74)
    assert "/" not in key, "key にスラッシュが含まれてはいけない"
    assert "\n" not in key, "key に改行が含まれてはいけない"
    assert " " not in key, "key にスペースが含まれてはいけない"


# ===========================================================================
# --cache-raw フラグ — 1st chunk raw キャッシュ (T-011)
# ===========================================================================

_RAW_FLAG = ["--cache-raw"]
_NON_PATTERN = "承知しました。実装を進めます。"


def test_cache_key_raw_returns_key_for_non_pattern():
    key = _run_cache_key(_NON_PATTERN, 3, extra_args=_RAW_FLAG)
    assert key.startswith("spk3_"), f"raw key の形式が不正: {key!r}"
    assert len(key) == len("spk3_") + 8


def test_cache_key_raw_no_flag_returns_empty():
    key = _run_cache_key(_NON_PATTERN, 3)
    assert key == "", "--cache-raw なし + 非定型文はキャッシュ対象外"


def test_cache_key_raw_pattern_match_unchanged():
    """normalize がマッチする文は --cache-raw 有無でキーが変わらない（normalize 優先）。"""
    key_with = _run_cache_key("OK。", 3, extra_args=_RAW_FLAG)
    key_without = _run_cache_key("OK。", 3)
    assert key_with == key_without


def test_cache_key_raw_different_text_different_hash():
    key1 = _run_cache_key("承知しました。", 3, extra_args=_RAW_FLAG)
    key2 = _run_cache_key("了解しました。", 3, extra_args=_RAW_FLAG)
    assert key1 != key2


def test_cache_key_raw_speed_changes_hash():
    key1 = _run_cache_key(_NON_PATTERN, 3, {"VOICEVOX_SPEED": "1.0"}, _RAW_FLAG)
    key2 = _run_cache_key(_NON_PATTERN, 3, {"VOICEVOX_SPEED": "1.5"}, _RAW_FLAG)
    assert key1 != key2, "speed が異なれば raw hash も変わるはず"


def test_cache_key_raw_over_max_chars_returns_empty():
    long_text = "あ" * 101
    key = _run_cache_key(long_text, 3, extra_args=["--cache-raw", "--cache-raw-max-chars", "100"])
    assert key == "", "101文字は上限 100 を超えるためキャッシュ対象外"


def test_cache_key_raw_zero_max_chars_returns_empty():
    key = _run_cache_key(_NON_PATTERN, 3, extra_args=["--cache-raw", "--cache-raw-max-chars", "0"])
    assert key == "", "max_chars=0 は対象外"


def test_cache_key_raw_negative_max_chars_returns_empty():
    key = _run_cache_key(_NON_PATTERN, 3, extra_args=["--cache-raw", "--cache-raw-max-chars", "-1"])
    assert key == "", "max_chars=-1 は対象外"


def test_cache_key_raw_synth_params_affect_hash():
    key1 = _run_cache_key(_NON_PATTERN, 3, {"VOICEVOX_PITCH": "0.0"}, _RAW_FLAG)
    key2 = _run_cache_key(_NON_PATTERN, 3, {"VOICEVOX_PITCH": "0.1"}, _RAW_FLAG)
    assert key1 != key2, "pitch が異なれば raw hash も変わるはず"


def test_cache_key_raw_no_collision_with_norm():
    """normalize 済みテキストと raw シードが同じ文字列でも異なるキーになること。"""
    from cache_patterns import normalize
    norm_text = normalize("OK。", 3)
    assert norm_text is not None
    key_norm = _run_cache_key("OK。", 3)
    key_raw = _run_cache_key(norm_text, 3, extra_args=_RAW_FLAG)
    assert key_norm != key_raw, "raw|プレフィックスにより norm キーと衝突しない"


# ===========================================================================
# ドキュメント回帰テスト
# ===========================================================================

def test_doc_05_cache_documents_raw_disable():
    doc = (ROOT / "doc" / "05-cache.md").read_text(encoding="utf-8")
    assert "VVREAD_CACHE_FIRST_CHUNK_RAW=false" in doc, \
        "doc/05-cache.md に raw cache の無効化方法が記載されていない"
