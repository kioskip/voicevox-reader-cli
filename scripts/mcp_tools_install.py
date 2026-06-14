"""scripts/mcp_tools_install.py — vvread MCP Tools 登録ヘルパー (B-148)

setup.py と hook_install.py の両方から呼べる最小限の pure helper。
Python mcp package のインストール (uv sync) は setup.py::step_mcp が担当。
本モジュールは「claude mcp add / get」操作のみを扱う。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

RunnerType = Callable[..., Any]


def _default_runner(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def check_mcp_tools_registration(runner: Optional[RunnerType] = None) -> str:
    """vvread MCP Tools の登録状態を返す。

    戻り値:
        "registered_local"       — local scope に登録済み（上書きしない）
        "not_registered"         — 未登録
        "conflicting_non_local"  — project/global scope に存在（WARN + 手動整理案内）
    """
    r = runner or _default_runner
    try:
        proc = r(
            ["claude", "mcp", "get", "vvread"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "not_registered"

    if proc.returncode != 0:
        return "not_registered"

    # 登録済み — scope を判定する
    output = (proc.stdout or "") + (proc.stderr or "")
    if "local" in output.lower():
        return "registered_local"
    # project/global scope の可能性
    return "conflicting_non_local"


def register_mcp_tools(
    repo_root: Path,
    dry_run: bool = False,
    runner: Optional[RunnerType] = None,
) -> bool:
    """vvread MCP Tools を local scope に登録する。

    Args:
        repo_root: voiceClaude のリポジトリルート (絶対パス)
        dry_run:   True の場合は何も実行しない
        runner:    サブプロセス factory (テスト用 DI)

    Returns:
        True = 登録成功 / 既登録 (no-op), False = 失敗
    """
    r = runner or _default_runner
    vvread_path = str(repo_root / "bin" / "vvread")

    if dry_run:
        return True

    try:
        proc = r(
            [
                "claude", "mcp", "add",
                "--transport", "stdio",
                "--scope", "local",
                "vvread", "--", vvread_path, "mcp",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARNING: claude mcp add failed: {e}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(
            f"WARNING: claude mcp add exit {proc.returncode}",
            file=sys.stderr,
        )
        return False

    return True


def print_mcp_tools_guide(repo_root: Optional[Path] = None) -> None:
    """MCP Tools 登録後の案内を表示する。"""
    print("vvread MCP Tools を登録しました。")
    print("Claude Code を起動すると /mcp で vvread ツールが使えます。")
