#!/usr/bin/env python3
"""cache_key.py - stdin テキストのキャッシュキーを計算する CLI

Usage: echo "OK。" | python cache_key.py --speaker 3
出力形式: spk{speaker}_{hash8} (8 桁は衝突率と可読性のバランスから選択)
キャッシュ対象外テキストは何も出力せず exit 0。
"""
import argparse
import hashlib
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute wav cache key for a text chunk")
    parser.add_argument("--speaker", type=int, required=True)
    parser.add_argument("--cache-raw", action="store_true",
                        help="normalize() が None のとき raw テキストでキーを計算する")
    parser.add_argument("--cache-raw-max-chars", type=int, default=100,
                        help="--cache-raw 有効時のテキスト文字数上限（0以下は対象外）")
    args = parser.parse_args()

    from cache_patterns import normalize  # scripts/ に同居

    text = sys.stdin.read().rstrip("\n")
    result = normalize(text, args.speaker)

    if not result:
        if not args.cache_raw:
            return 0
        max_chars = args.cache_raw_max_chars
        if max_chars <= 0 or len(text) > max_chars:
            return 0
        seed_text = f"raw|{text}"
    else:
        seed_text = result

    speed      = os.environ.get("VOICEVOX_SPEED",       "1.5")
    pitch      = os.environ.get("VOICEVOX_PITCH",       "0")
    intonation = os.environ.get("VOICEVOX_INTONATION",  "1.0")
    volume     = os.environ.get("VOICEVOX_VOLUME",      "1.0")
    pause      = os.environ.get("VOICEVOX_PAUSE_SCALE", "1.0")
    pre        = os.environ.get("VOICEVOX_PRE_PHONEME", "0")
    post       = os.environ.get("VOICEVOX_POST_PHONEME","0")

    seed = (
        f"{seed_text}|spk={args.speaker}|speed={speed}|pitch={pitch}"
        f"|intonation={intonation}|volume={volume}|pause={pause}"
        f"|pre={pre}|post={post}"
    )
    h = hashlib.sha256(seed.encode()).hexdigest()[:8]
    print(f"spk{args.speaker}_{h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
