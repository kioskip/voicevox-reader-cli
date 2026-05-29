#!/usr/bin/env python3
"""scripts/hook_status.py - vvread hook 登録状態の判定 (F-112)

Claude Code settings (.claude/settings.json 等) を読んで、
vvread の Stop hook が登録されているか確認する。

config.py / hook_install.py の両方から依存されるため、
外部モジュール依存なし（標準ライブラリのみ）で実装する。

resolve_settings_path / is_voiceclaude_hook は元々 hook_install.py にあったが、
循環 import を防ぐためこのモジュールに移動した。
hook_install.py は当該関数をこのモジュールから import して使う。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# scope path 解決（移動元: hook_install.py:75-100）
# ---------------------------------------------------------------------------


def resolve_settings_path(
    scope: str,
    *,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
) -> Path:
    """scope に対応する settings.json の絶対パスを返す。

    cwd / home は test 用 DI。default は現在の Path.cwd() / Path.home()。

    scope マッピング (v0.1.2):
      project-local -> <cwd>/.claude/settings.local.json
      project       -> <cwd>/.claude/settings.json
      user          -> ~/.claude/settings.json
    """
    if cwd is None:
        cwd = Path.cwd()
    if home is None:
        home = Path.home()
    if scope == "project-local":
        return cwd / ".claude" / "settings.local.json"
    if scope == "project":
        return cwd / ".claude" / "settings.json"
    if scope == "user":
        return home / ".claude" / "settings.json"
    raise ValueError(f"unknown scope: {scope!r}")


# ---------------------------------------------------------------------------
# vvread hook 判定（移動元: hook_install.py:124-146）
# ---------------------------------------------------------------------------


def is_voiceclaude_hook(
    command: str,
    repo_root: Optional[Path] = None,  # noqa: ARG001
) -> bool:
    """command 文字列が voiceClaude の Stop hook を指しているか判定。

    判定ルール(doctor.py から移管した正本):
    - "vvread on-stop" を含む(PATH 経由 or 絶対パス、空白 or タブ区切り)
    - "/bin/vvread" を含み、引数 "on-stop" を含む(クォート有り無しの両対応)
    - "scripts/on_stop.sh" を含む(legacy)

    Note: repo_root 引数は後方互換のために残しているが、現在は参照しない。
    """
    if not isinstance(command, str):
        return False
    if "vvread on-stop" in command or "vvread\ton-stop" in command:
        return True
    if "/bin/vvread" in command and "on-stop" in command:
        return True
    if "scripts/on_stop.sh" in command or "/on_stop.sh" in command:
        return True
    return False


# ---------------------------------------------------------------------------
# 内部: settings.json 読み込み
# ---------------------------------------------------------------------------


def _read_claude_settings(path: Path) -> Optional[dict]:
    """.claude/settings.json を読み込む。存在しない・パースエラーは None。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# 内部: 1 scope の hook 状態判定
# ---------------------------------------------------------------------------


def _get_scope_hook_status(scope: str, cwd: Path, home: Path) -> str:
    """1 scope の hook 状態を返す: "registered" | "legacy" | "none"

    "registered": modern vvread on-stop hook が存在する
    "legacy"    : scripts/on_stop.sh 系 hook のみ存在する
    "none"      : vvread hook 未登録
    """
    path = resolve_settings_path(scope, cwd=cwd, home=home)
    data = _read_claude_settings(path)
    if data is None:
        return "none"
    stop_blocks: List[dict] = data.get("hooks", {}).get("Stop", []) or []
    has_vc = False
    has_legacy = False
    for block in stop_blocks:
        if not isinstance(block, dict):
            continue
        for h in (block.get("hooks", []) or []):
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if not is_voiceclaude_hook(cmd):
                continue
            has_vc = True
            if "scripts/on_stop.sh" in cmd or "/on_stop.sh" in cmd:
                has_legacy = True
    if has_vc and not has_legacy:
        return "registered"
    if has_legacy:
        return "legacy"
    return "none"


# ---------------------------------------------------------------------------
# public: 全 scope を集約した hook 状態
# ---------------------------------------------------------------------------


def get_vvread_hook_status(cwd: Path, home: Optional[Path] = None) -> str:
    """全 scope を走査し、最優先の hook 状態を返す。

    Returns:
      "modern" : いずれかの scope に vvread on-stop が登録済み
      "legacy" : modern なし、旧形式 on_stop.sh のみ検出
      "none"   : hook 未登録

    優先順位（集約ルール）:
      any "registered" → "modern"
      else any "legacy" → "legacy"
      else "none"
    """
    _home = home or Path.home()
    statuses = [
        _get_scope_hook_status(s, cwd, _home)
        for s in ("project-local", "project", "user")
    ]
    if "registered" in statuses:
        return "modern"
    if "legacy" in statuses:
        return "legacy"
    return "none"
