#!/usr/bin/env python3
"""chunk_split.py - 整形済テキストを chunk に分割する CLI

stdin で sanitize.py の整形済出力(改行はチャンク境界のヒント)を受け取り、
sanitize.split_into_chunks で chunk 単位の行に分解して stdout に出す。

`--speaker N` を渡すと cache_patterns.normalize による cache 対象判定が走り、
キャッシュ対象の文は独立 chunk として切り出される(cache-aware split)。
sanitize モジュールが cache_patterns に依存せず済むよう、cache_patterns の
import は本ファイル側に局所化されている(S-008 の DIP)。

呼び出し例:
    echo "整形済テキスト" | chunk_split.py
    echo "整形済テキスト" | chunk_split.py --speaker 3
"""
import argparse
import os
import sys
from typing import Callable, Optional

# 同ディレクトリの sanitize / cache_patterns を import 可能にする
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sanitize import split_into_chunks  # noqa: E402


def _build_is_cacheable(speaker_id: int) -> Callable[[str], bool]:
    """cache_patterns.normalize から is_cacheable predicate を組み立てる。

    cache_patterns 依存を本関数の中に閉じ込めることで、`--speaker` 不在時には
    cache_patterns が一切ロードされない(オプション依存の局所化)。
    """
    from cache_patterns import normalize  # noqa: WPS433
    return lambda sentence: normalize(sentence, speaker_id) is not None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--speaker", type=int, default=None,
        help="cache_patterns 判定用の話者 ID。指定すると cache-aware split が走る。",
    )
    args = parser.parse_args()

    text = sys.stdin.read()

    is_cacheable: Optional[Callable[[str], bool]] = None
    if args.speaker is not None:
        is_cacheable = _build_is_cacheable(args.speaker)

    for chunk in split_into_chunks(text, is_cacheable=is_cacheable):
        sys.stdout.write(chunk + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
