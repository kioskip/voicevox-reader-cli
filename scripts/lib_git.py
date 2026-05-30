#!/usr/bin/env python3
"""lib_git.py - git リポジトリ判定ヘルパー (U-115)

setup.py / hook_install.py で重複していた `_in_git_repo()` を一本化。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def in_git_repo(cwd: Optional[Path] = None) -> bool:
    """cwd が git リポジトリ配下かどうかを確認する。

    以下はすべて False を返す（呼出側で安全にフォールバックできるよう、
    例外は外に漏らさない）:
      - cwd が存在しない（FileNotFoundError / NotADirectoryError → OSError 捕捉）
      - git コマンドが非0終了（returncode != 0）
      - git が PATH に無い（FileNotFoundError → OSError 捕捉）
      - git の応答が timeout（TimeoutExpired）
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            cwd=str(cwd or Path.cwd()),
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False
