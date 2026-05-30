"""tests/test_lib_prompt.py - lib_prompt.prompt_speaker_id のテスト (F-101)"""
import io
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import lib_prompt as lp  # noqa: E402


class TestPromptSpeakerId:
    def test_display_uses_speaker_id_not_index(self):
        """左端の番号が style ID であること（連番ではないこと）を確認"""
        out = io.StringIO()
        in_ = io.StringIO("\n")  # Enter = current_id を維持
        ids = [2, 3]
        opts = ["四国めたん", "ずんだもん"]
        lp.prompt_speaker_id("Speaker:", opts, ids, current_id=2,
                             in_stream=in_, out_stream=out)
        rendered = out.getvalue()
        assert "  2) 四国めたん" in rendered
        assert "  3) ずんだもん" in rendered
        assert "  1) " not in rendered  # 連番は出ない

    def test_input_style_id_returns_correct_speaker(self):
        """`3` 入力で speaker_id=3 が返ること（3番目の項目ではなく ID=3）"""
        out = io.StringIO()
        in_ = io.StringIO("3\n")
        ids = [2, 3]
        opts = ["四国めたん", "ずんだもん"]
        result = lp.prompt_speaker_id("Speaker:", opts, ids, current_id=2,
                                      in_stream=in_, out_stream=out)
        assert result == 3

    def test_raises_on_length_mismatch(self):
        """speaker_ids と speaker_options の長さ不一致で RuntimeError"""
        with pytest.raises(RuntimeError, match="長さ不一致"):
            lp.prompt_speaker_id("Speaker:", ["四国めたん"], [2, 3], current_id=2)


class TestPromptYn:
    def test_enter_returns_default_true(self):
        """Enter のみ → default(True) を返す (U-114)"""
        out = io.StringIO()
        in_ = io.StringIO("\n")
        assert lp.prompt_yn("OK?", default=True, in_stream=in_, out_stream=out) is True

    def test_enter_returns_default_false(self):
        """Enter のみ → default(False) を返す (U-114)"""
        out = io.StringIO()
        in_ = io.StringIO("\n")
        assert lp.prompt_yn("OK?", default=False, in_stream=in_, out_stream=out) is False

    def test_yes_input_returns_true(self):
        out = io.StringIO()
        in_ = io.StringIO("y\n")
        assert lp.prompt_yn("OK?", default=False, in_stream=in_, out_stream=out) is True

    def test_no_input_returns_false(self):
        out = io.StringIO()
        in_ = io.StringIO("n\n")
        assert lp.prompt_yn("OK?", default=True, in_stream=in_, out_stream=out) is False


class TestPromptChoice:
    def test_number_selects_choice(self):
        """番号入力でその選択肢を返す (U-113)"""
        out = io.StringIO()
        in_ = io.StringIO("2\n")
        result = lp.prompt_choice(
            "scope:", ["project-local", "project", "user"], "project-local",
            in_stream=in_, out_stream=out,
        )
        assert result == "project"

    def test_enter_returns_default(self):
        """Enter のみ → default を返す (U-113)"""
        out = io.StringIO()
        in_ = io.StringIO("\n")
        result = lp.prompt_choice(
            "scope:", ["project-local", "project", "user"], "user",
            in_stream=in_, out_stream=out,
        )
        assert result == "user"
        # default 行に [default] マーカーが付くこと
        assert "user" in out.getvalue()
        assert "[default]" in out.getvalue()

    def test_invalid_then_valid(self):
        """範囲外 → 再 prompt → 有効な番号で確定 (U-113)"""
        out = io.StringIO()
        in_ = io.StringIO("9\n1\n")
        result = lp.prompt_choice(
            "scope:", ["project-local", "project", "user"], "user",
            in_stream=in_, out_stream=out,
        )
        assert result == "project-local"
