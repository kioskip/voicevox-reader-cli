#!/usr/bin/env python3
"""scripts/json_file.py - JSON ファイルの読み書き共通ユーティリティ

hook_install.py (Claude hook 設定) と config.py (vvread.settings.json) の
両方が必要とする atomic write / backup を共通化する。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def load_json_file(
    path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """JSON ファイルを読み込んで dict を返す。

    戻り値:
      (data, None)   成功
      (None, None)   ファイル不在 or 空ファイル（新規作成扱い）
      (None, errmsg) 読み取りエラー / JSON 破損 / top-level が非 object
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"cannot read: {e}"
    if not text.strip():
        return None, None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(data, dict):
        return None, "top-level must be an object"
    return data, None


def backup_file(path: Path) -> Optional[Path]:
    """path.bak にコピーして bak パスを返す。元ファイル不在なら None。

    毎回上書き（世代管理なし）。git 管理前提。
    """
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    return bak


def write_json_atomic(
    path: Path,
    data: Dict[str, Any],
    *,
    indent: int = 2,
) -> None:
    """JSON を atomic に書き出す。

    .tmp に書いてから os.replace() でアトミック置換する。
    書込中の kill / disk full でファイルが壊れない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
