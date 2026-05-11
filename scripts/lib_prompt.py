#!/usr/bin/env python3
"""lib_prompt.py - 対話 prompt ヘルパー (R-102)

hook_install.py に分散していた prompt 系ヘルパーを一本化。
setup.py の SetupContext ベースの prompt 関数とは分離（依存関係が異なるため）。

提供関数:
  is_tty(stream)              - stream が TTY かどうか判定
  prompt_yn(question, ...)    - Y/n プロンプト
  prompt_choice(question, ...) - 番号付き選択肢プロンプト
  prompt_speaker_id(...)      - Speaker style ID プロンプト
"""

from __future__ import annotations

import sys
from typing import Any, List


def is_tty(stream: Any = None) -> bool:
    """stream（default: sys.stdin）が TTY かどうか判定する。"""
    s = stream or sys.stdin
    isatty = getattr(s, "isatty", lambda: False)
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001
        return False


def prompt_yn(
    question: str,
    default: bool = True,
    *,
    in_stream: Any = None,
    out_stream: Any = None,
) -> bool:
    """Y/n プロンプト。"""
    suffix = "[Y/n]" if default else "[y/N]"
    out = out_stream or sys.stdout
    in_ = in_stream or sys.stdin
    out.write(f"{question} {suffix}: ")
    out.flush()
    line = in_.readline()
    if not line:
        return default
    line = line.strip().lower()
    if not line:
        return default
    return line in ("y", "yes", "1", "true")


def prompt_choice(
    question: str,
    choices: List[str],
    default: str,
    *,
    in_stream: Any = None,
    out_stream: Any = None,
) -> str:
    """番号付き選択肢プロンプト。Enter でデフォルト。"""
    out = out_stream or sys.stdout
    in_ = in_stream or sys.stdin
    out.write(f"{question}\n")
    for i, choice in enumerate(choices, 1):
        marker = "  [default]" if choice == default else ""
        out.write(f"  {i}) {choice}{marker}\n")
    while True:
        out.write(f"選択 [1-{len(choices)}] (Enter で {default!r}): ")
        out.flush()
        line = in_.readline()
        if not line or not line.strip():
            return default
        try:
            idx = int(line.strip())
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        except ValueError:
            pass
        out.write(f"  1 から {len(choices)} の数字を入力してください。\n")


def prompt_speaker_id(
    question: str,
    speaker_options: List[str],
    speaker_ids: List[int],
    current_id: int,
    *,
    in_stream: Any = None,
    out_stream: Any = None,
) -> int:
    """Speaker を style ID で選択するプロンプト。

    一覧は番号付きで表示するが、入力は style ID（数字）で行う。
    Enter のみで current_id を返す。
    """
    out = out_stream or sys.stdout
    in_ = in_stream or sys.stdin
    out.write(f"{question}\n")
    for i, opt in enumerate(speaker_options, 1):
        out.write(f"  {i}) {opt}\n")
    while True:
        out.write(f"Style ID を入力 (Enter で現在値 {current_id} を維持): ")
        out.flush()
        line = in_.readline()
        if not line or not line.strip():
            return current_id
        try:
            entered = int(line.strip())
            if entered in speaker_ids:
                return entered
            out.write(f"  ID {entered} は存在しません。上のリストの ID を入力してください。\n")
        except ValueError:
            out.write("  数字（style ID）を入力してください。\n")
