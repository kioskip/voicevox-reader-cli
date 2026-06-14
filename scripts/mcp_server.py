#!/usr/bin/env python3
"""scripts/mcp_server.py - vvread MCP server (B-110)

vvread の読み上げ機能を MCP tool として Claude に公開する。
Stop hook 経由の発話とは独立した呼び出し経路となり、Claude が応答中の
任意タイミングで読み上げ・停止・状態確認を行える。

必要パッケージ: uv sync --extra mcp  (Python >=3.10)
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP  # mcp import はこのファイルのみ
from mcp.types import ToolAnnotations

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

# バイナリ: このファイルの install 場所から解決（CLAUDE_PROJECT_DIR 非依存）
VVREAD = Path(__file__).resolve().parent.parent / "bin" / "vvread"

# cwd: ユーザーの作業プロジェクト（vvread.settings.json のカスケード起点）
CWD = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).resolve()

mcp = FastMCP("vvread")


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False,
))
def vvread_say(text: str, speaker: int | None = None) -> str:
    """Read aloud the given text via VOICEVOX and return immediately.

    Use this when Claude wants to notify the user by voice:
    - Important progress during long-running tasks (build, review, deploy)
    - Blocking errors that require user attention
    - Completion of a significant step
    - Waiting for user decision or input
    - Short summaries (not raw logs)

    Playback runs in the background. Call vvread_stop to cancel.
    """
    args = [str(VVREAD)]
    if speaker is not None:
        args += ["--speaker", str(speaker)]
    # source=mcp タグ + 要約生成時刻（VVREAD_SAY_CREATED_MS）を渡す。
    # queue モードでは、後追いの Stop hook 全文より古い要約は marker 比較で
    # drop される（hook evict 後の遅延 enqueue race を時刻比較で塞ぐ）。
    env = {
        **os.environ,
        "VVREAD_SAY_SOURCE": "mcp",
        "VVREAD_SAY_CREATED_MS": str(time.time_ns() // 1_000_000),
    }
    # 既存の `echo text | vvread` 経路を再利用（stdin 経由でテキスト渡し）
    # start_new_session=True で親プロセスのシグナルから独立させる
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(CWD),
        start_new_session=True,
        env=env,
    )
    proc.stdin.write(text.encode())
    proc.stdin.close()
    # daemon thread で wait() を呼びゾンビプロセスを回収する
    # os.fork() double-fork は macOS の asyncio との相性問題があるため使わない
    threading.Thread(target=proc.wait, daemon=True).start()
    return "started"


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True,
))
def vvread_stop() -> str:
    """Stop the current VOICEVOX playback.

    Use this when the current speech is no longer relevant:
    - Before replacing it with a more important notification
    - When the user has already seen or handled the information
    """
    result = subprocess.run(
        [str(VVREAD), "stop"],
        cwd=str(CWD),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "stop failed")
    return result.stdout or "OK"


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
))
def vvread_status() -> str:
    """Return the current VOICEVOX playback state.

    Use this to check if audio is currently playing before deciding
    whether to call vvread_say or vvread_stop.
    Returns a human-readable string such as "state: idle" or
    "state: playing (pid=1234)".
    """
    result = subprocess.run(
        [str(VVREAD), "status"],
        cwd=str(CWD),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "status failed")
    return result.stdout


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True,
))
def vvread_speakers() -> list:
    """List available VOICEVOX speakers from the configured primary engine.

    Returns a list of {"name": str, "styles": [{"id": int, "name": str}]}.
    Use this to find speaker IDs before calling vvread_say with a specific speaker,
    or to let the user choose a speaker for vvread_config_set.
    """
    r = subprocess.run(
        [str(VVREAD), "speakers", "--json"],
        capture_output=True, text=True, cwd=str(CWD), timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "speakers failed")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"speakers: invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError(f"speakers: expected list, got {type(data).__name__}")
    return data


_CONFIG_ALLOWLIST: dict = {
    "voicevox.speaker":    (int,   0,     9999),
    "voicevox.speed":      (float, 0.5,   2.0),
    "voicevox.pitch":      (float, -0.15, 0.15),
    "voicevox.intonation": (float, 0.0,   2.0),
    "voicevox.volume":     (float, 0.0,   2.0),
}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True,
))
def vvread_config_set(key: str, value: str) -> str:
    """Update one allowed project voice setting.

    Use this only when the user explicitly asks to change the voice.
    Do not change settings proactively.

    Allowed keys: voicevox.speaker, voicevox.speed, voicevox.pitch,
                  voicevox.intonation, voicevox.volume
    """
    if key not in _CONFIG_ALLOWLIST:
        allowed = ", ".join(sorted(_CONFIG_ALLOWLIST))
        raise RuntimeError(f"key {key!r} は変更できません。許可キー: {allowed}")
    typ, lo, hi = _CONFIG_ALLOWLIST[key]
    try:
        typed_value = typ(value)
    except (ValueError, TypeError):
        raise RuntimeError(f"{key} には {typ.__name__} 型の値が必要です: {value!r}")
    if not (lo <= typed_value <= hi):
        raise RuntimeError(
            f"{key} の値は {lo}〜{hi} の範囲で指定してください: {typed_value}"
        )
    r = subprocess.run(
        [str(VVREAD), "config", "--set", f"{key}={typed_value}", "--project"],
        capture_output=True, text=True, cwd=str(CWD), timeout=5,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"config --set failed (exit {r.returncode})")
    return r.stdout.strip()


if __name__ == "__main__":
    mcp.run(transport="stdio")
