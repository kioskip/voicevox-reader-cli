"""scripts/receiver_install.py — vvread-receiver 登録ヘルパー (B-148/B-149)

setup.py と hook_install.py の両方から呼べる独立モジュール。
MCP Tools (vvread) の登録は含めない — mcp_tools_install.py が担当。

登録モデル:
    Claude Code local scope のみ。.mcp.json は変更しない。
    絶対パスで bun /abs/path/receiver/server.ts を指定する。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

RunnerType = Callable[..., Any]


def _default_runner(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


def check_bun(runner: Optional[RunnerType] = None) -> bool:
    """bun が PATH 上に存在するか確認する。"""
    return shutil.which("bun") is not None


def ensure_receiver_dependencies(
    receiver_dir: Path,
    dry_run: bool = False,
    runner: Optional[RunnerType] = None,
) -> bool:
    """receiver/node_modules に MCP SDK が bun install 済みか確認し、なければインストール。

    Returns:
        True = 依存OK / False = インストール失敗
    """
    r = runner or _default_runner
    sdk_path = receiver_dir / "node_modules" / "@modelcontextprotocol" / "sdk"

    if sdk_path.exists():
        return True

    if dry_run:
        return True

    try:
        proc = r(
            ["bun", "install", "--frozen-lockfile"],
            capture_output=True, text=True,
            timeout=180,
            cwd=str(receiver_dir),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"WARNING: bun install failed: {e}", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(
            f"WARNING: bun install exit {proc.returncode}",
            file=sys.stderr,
        )
        return False

    return True


def get_receiver_registration_status(
    runner: Optional[RunnerType] = None,
) -> str:
    """vvread-receiver の登録状態を返す。

    戻り値:
        "registered_local"       — local scope に登録済み（上書きしない）
        "not_registered"         — 未登録
        "conflicting_non_local"  — project/global scope に存在
                                   WARN + 手動整理案内、自動登録しない
    """
    r = runner or _default_runner
    try:
        proc = r(
            ["claude", "mcp", "get", "vvread-receiver"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "not_registered"

    if proc.returncode != 0:
        return "not_registered"

    output = (proc.stdout or "") + (proc.stderr or "")
    if "local" in output.lower():
        return "registered_local"
    return "conflicting_non_local"


def register_receiver_mcp(
    repo_root: Path,
    dry_run: bool = False,
    runner: Optional[RunnerType] = None,
) -> bool:
    """vvread-receiver を local scope に登録する。

    絶対パスで `bun /abs/path/receiver/server.ts` を指定する。

    Returns:
        True = 登録成功 / dry_run, False = 失敗 / conflicting
    """
    r = runner or _default_runner
    receiver_path = str(repo_root / "receiver" / "server.ts")

    status = get_receiver_registration_status(r)
    if status == "registered_local":
        return True  # no-op
    if status == "conflicting_non_local":
        print(
            "WARNING: vvread-receiver は project/global scope で登録済みです。\n"
            "  自動上書きしません。手動で整理してください:\n"
            "    claude mcp remove vvread-receiver\n"
            "  その後 vvread install --with-receiver を再実行してください。",
            file=sys.stderr,
        )
        return False

    if dry_run:
        return True

    try:
        proc = r(
            [
                "claude", "mcp", "add",
                "--transport", "stdio",
                "--scope", "local",
                "vvread-receiver", "--", "bun", receiver_path,
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


def print_receiver_activation_guide() -> None:
    """receiver 登録後の案内を表示する。"""
    print("receiver を登録しました。")
    print()
    print("Claude Code を以下で起動してください:")
    print("  claude --dangerously-load-development-channels server:vvread-receiver")
    print()
    print("外部通知と Stop hook の割り込みを避ける場合:")
    print("  vvread queue on")
    print()
    print("外部通知テスト:")
    print('  curl -X POST localhost:8788 -d "CI が完了しました"')
