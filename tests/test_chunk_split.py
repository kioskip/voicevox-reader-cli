"""chunk_split.py のテスト

sanitize.split_into_chunks と cache_patterns.normalize を組み合わせる薄い CLI。
sanitize モジュールが cache_patterns を知らない構造(S-008 / DIP)に対応する。
"""
import io
import sys

import pytest


def _run_main(text: str, argv: list[str]) -> str:
    import chunk_split
    saved_stdin, saved_stdout, saved_argv = sys.stdin, sys.stdout, sys.argv
    try:
        sys.stdin = io.StringIO(text)
        sys.stdout = io.StringIO()
        sys.argv = argv
        chunk_split.main()
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.argv = saved_stdin, saved_stdout, saved_argv


# ---------- デフォルト(traditional split)----------


class TestChunkSplitDefault:
    def test_short_text_yields_single_chunk(self):
        out = _run_main("短いテストです。", ["chunk_split.py"])
        chunks = [c for c in out.split("\n") if c]
        assert chunks == ["短いテストです。"]

    def test_newlines_are_split_hints_when_within_target(self):
        # 短い改行入り 1 chunk: 改行は空白に変換されて統合される
        out = _run_main("段落1\n段落2", ["chunk_split.py"])
        chunks = [c for c in out.split("\n") if c]
        assert chunks == ["段落1 段落2"]

    def test_no_speaker_does_not_apply_cache_aware_split(self):
        # OK。 を含む文字列でも --speaker 無しなら独立 chunk にならない
        text = "前の文。OK。次の文。"
        out = _run_main(text, ["chunk_split.py"])
        chunks = [c for c in out.split("\n") if c]
        assert chunks == ["前の文。OK。次の文。"]


# ---------- --speaker N(cache-aware split)----------


class TestChunkSplitWithSpeaker:
    """--speaker N を渡すと cache_patterns 経由で cache-aware split が走る"""

    def test_template_emitted_as_independent_chunk(self):
        text = "前の文。OK。次の文。"
        out = _run_main(text, ["chunk_split.py", "--speaker", "3"])
        chunks = [c for c in out.split("\n") if c]
        assert "OK。" in chunks, f"OK。 が独立 chunk になっていない: {chunks}"

    def test_consecutive_templates_each_independent(self):
        # OK と 了解 はどちらも cache_patterns の正例
        text = "OK。完了しました。了解。"
        out = _run_main(text, ["chunk_split.py", "--speaker", "3"])
        chunks = [c for c in out.split("\n") if c]
        # それぞれ独立 chunk
        assert "OK。" in chunks
        assert "完了しました。" in chunks
        assert "了解。" in chunks

    def test_invalid_speaker_value_raises_argparse_error(self):
        # `--speaker abc` は int 変換に失敗 → argparse が SystemExit を送る
        with pytest.raises(SystemExit):
            _run_main("テキスト", ["chunk_split.py", "--speaker", "abc"])


# ---------- DIP の境界(sanitize 側に cache_patterns 依存が残っていないこと)----------


class TestSanitizeIsFreeFromCachePatterns:
    """sanitize モジュール本体が cache_patterns に依存していないことを確認(S-008)。

    `import sanitize` した結果 sys.modules に cache_patterns が現れていたら、
    sanitize 経由で間接 import が起きている = 依存方向の逆転失敗。
    """

    def test_sanitize_import_does_not_pull_cache_patterns(self):
        import importlib
        import sys as _sys

        # 既にロード済みなら一旦削除して再ロードする
        for mod in ("sanitize", "cache_patterns"):
            _sys.modules.pop(mod, None)

        importlib.import_module("sanitize")
        assert "cache_patterns" not in _sys.modules, \
            "sanitize import で cache_patterns が引きずられている(DIP 違反)"
