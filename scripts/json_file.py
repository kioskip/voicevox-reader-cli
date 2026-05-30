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


# ---------------------------------------------------------------------------
# JSONC サポート (B-119)
# ---------------------------------------------------------------------------


def _strip_jsonc_comments(text: str) -> str:
    """`//` 行コメントを除去。string リテラル内の `//` は保護する。

    settings.py の _strip_jsonc_line_comments() と同一ロジック。
    json_file.py は低レベルユーティリティのため settings.py からは import せず自己完結する。
    block comment (`/* */`) は v0.1 サポート外。
    """
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            if i > 0 and text[i - 1] == "\\":
                pass
            else:
                in_string = not in_string
            out.append(c)
            i += 1
            continue
        if not in_string and c == "/" and i + 1 < n and text[i + 1] == "/":
            # 行末まで comment、改行は保持（行番号合わせ）
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc_file(
    path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """JSONC ファイルを読み込んで dict を返す。

    load_json_file() と同じ I/F だが、`//` 行コメントを strip してから parse する。
    vvread config の対話書き込みで生成したコメント付きファイルを次回読み込む用途。

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
    stripped = _strip_jsonc_comments(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(data, dict):
        return None, "top-level must be an object"
    return data, None


def _build_jsonc_text(
    data: Dict[str, Any],
    commented_flat: Dict[str, Any],
    *,
    indent: int = 2,
) -> str:
    """JSONC テキストを生成する。commented_flat のキーは // コメント行として出力。

    NOTE: 2 階層 dot-path (例: "voicevox.speaker") のみ対応。
    CONFIG_FIELDS に 3 階層以上のキーを追加する場合は本関数の拡張が必要。

    コメント行はオブジェクト先頭に配置する（末尾カンマ問題の回避）。
    コメント行の値はカスケード解決済みの「参考値」であり、ファイル保存値とは限らない。
    """
    pad = " " * indent

    def _val(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    # commented_flat を名前空間別にグループ化
    commented_by_ns: Dict[str, Dict[str, Any]] = {}
    for dot_path, val in commented_flat.items():
        parts = dot_path.split(".", 1)
        ns = parts[0]
        key = parts[1] if len(parts) > 1 else dot_path
        commented_by_ns.setdefault(ns, {})[key] = val

    # 全トップレベルキー（挿入順を保持）
    all_ns = list(dict.fromkeys(list(data.keys()) + list(commented_by_ns.keys())))

    lines = ["{"]
    for i, ns in enumerate(all_ns):
        is_last_ns = i == len(all_ns) - 1
        ns_comma = "" if is_last_ns else ","

        inner = data.get(ns, {})
        commented_ns = commented_by_ns.get(ns, {})

        if isinstance(inner, dict) or commented_ns:
            if not isinstance(inner, dict):
                inner = {}
            lines.append(f'{pad}"{ns}": {{')

            # コメント行を先頭に配置（常にカンマ付き）
            # 行ごと strip されるため、strip 後の残りアクティブキーに末尾カンマが残らない
            for k, v in commented_ns.items():
                lines.append(f'{pad}{pad}// "{k}": {_val(v)},')

            # アクティブキー（最後のみカンマなし）
            active = list(inner.items())
            for j, (k, v) in enumerate(active):
                comma = "" if j == len(active) - 1 else ","
                lines.append(f'{pad}{pad}"{k}": {_val(v)}{comma}')

            lines.append(f'{pad}}}{ns_comma}')
        else:
            lines.append(f'{pad}"{ns}": {_val(inner)}{ns_comma}')

    lines.append("}")
    return "\n".join(lines) + "\n"


def write_jsonc_atomic(
    path: Path,
    data: Dict[str, Any],
    commented_flat: Dict[str, Any],
    *,
    indent: int = 2,
) -> None:
    """JSONC を atomic に書き出す。commented_flat のキーは // コメント行として出力。

    .tmp に書いてから os.replace() でアトミック置換する。
    書込中の kill / disk full でファイルが壊れない。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _build_jsonc_text(data, commented_flat, indent=indent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
