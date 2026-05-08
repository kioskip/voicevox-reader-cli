#!/usr/bin/env python3
"""parse_transcript.py - Stop hook ペイロードから最後の assistant text を取り出す。

S-002 で on_stop.sh の Python heredoc(transcript_path 取得 + 最後の assistant
text 抽出)を切り出した。R-006 で `vvread on-stop` から呼ばれる。

stdin: Stop hook の JSON ペイロード(`{"transcript_path": "...", ...}`)
stdout: transcript の最後の assistant メッセージの text(無ければ空)
exit code: 常に 0(hook 文脈で fail させない)

stdin の読み取り上限と timeout を内蔵する:
  --max-bytes  (default 2097152 = 2MB)  超過時は warning + 空 stdout で exit 0
  --timeout    (default 10s)            超過時は warning + 空 stdout で exit 0

stderr に warning を出すケース:
  - timeout
  - oversize(max-bytes 超過)
  - JSON 不正
  - transcript_path 不在 / ファイル不在

bash 側で stderr を捨てれば silent、cmd_on_stop はログ用に拾う設計。
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time
from typing import Optional

DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2MB
DEFAULT_TIMEOUT_SEC = 10.0


def _read_stdin_with_timeout(max_bytes: int, timeout_sec: float) -> Optional[bytes]:
    """stdin を timeout / size cap 付きで読む。

    成功時は読み取った bytes を返す(空 bytes も成功扱い)。
    timeout / oversize 時は None を返し、stderr に warning を出す。

    select ベースの実装で macOS / Linux 両対応。timeout に小数を渡せる。
    bash の `read` には timeout が無く `timeout` コマンドも macOS は brew 必須
    のため、Python 側で実装しておくと cross-platform で安定する。
    """
    fd = sys.stdin.fileno()
    deadline = time.monotonic() + timeout_sec
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"parse_transcript: stdin read timed out "
                f"(>{timeout_sec:.1f}s, got {len(buf)} bytes)",
                file=sys.stderr,
            )
            return None
        # select は regular file では即座に ready を返す。テストや実運用での
        # pipe 入力では正しく block + timeout する。
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            # fd が無効になった等。空入力扱いで抜ける
            return bytes(buf)
        if not ready:
            print(
                f"parse_transcript: stdin read timed out "
                f"(>{timeout_sec:.1f}s, got {len(buf)} bytes)",
                file=sys.stderr,
            )
            return None
        # max_bytes + 1 byte だけ読み込んで「超過」を検知する
        try:
            chunk = os.read(fd, min(65536, max_bytes - len(buf) + 1))
        except OSError:
            return bytes(buf)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > max_bytes:
            print(
                f"parse_transcript: stdin payload exceeds max-bytes "
                f"({max_bytes} bytes); abandoned",
                file=sys.stderr,
            )
            return None


def _extract_transcript_path(payload: bytes) -> str:
    """hook JSON から transcript_path を取り出す。失敗時は空文字を返す。"""
    if not payload.strip():
        return ""
    try:
        data = json.loads(payload.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(
            f"parse_transcript: invalid JSON payload ({exc})",
            file=sys.stderr,
        )
        return ""
    if not isinstance(data, dict):
        print(
            "parse_transcript: payload is not a JSON object",
            file=sys.stderr,
        )
        return ""
    path = data.get("transcript_path", "")
    return path if isinstance(path, str) else ""


def _last_assistant_text(transcript_path: str) -> str:
    """transcript JSONL を末尾まで舐めて、最後の assistant entry の text を返す。

    `entry.message.content` は list[dict{type,text}] か str。list の場合は
    type=text の text を改行で連結する。空 text の entry は last_text を更新
    しない(直前の有効テキストを保持する)。
    """
    last_text = ""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # 1 行壊れていても他行を生かす(transcript は append-only で
                    # 末尾が中途半端なケースがあるため)
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                text = ""
                if isinstance(content, list):
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    text = "\n".join(p for p in parts if p).strip()
                elif isinstance(content, str):
                    text = content.strip()
                if text:
                    last_text = text
    except FileNotFoundError:
        print(
            f"parse_transcript: transcript not found: {transcript_path}",
            file=sys.stderr,
        )
        return ""
    except OSError as exc:
        print(
            f"parse_transcript: cannot read transcript ({exc})",
            file=sys.stderr,
        )
        return ""
    return last_text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract last assistant text from Stop hook payload"
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"stdin max bytes (default {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"stdin read timeout in seconds (default {DEFAULT_TIMEOUT_SEC})",
    )
    args = parser.parse_args()

    payload = _read_stdin_with_timeout(args.max_bytes, args.timeout)
    if payload is None:
        # timeout / oversize: warning は内部で出力済。空 stdout で正常終了
        return 0

    path = _extract_transcript_path(payload)
    if not path:
        return 0

    text = _last_assistant_text(path)
    if text:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
