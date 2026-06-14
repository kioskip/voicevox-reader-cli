#!/usr/bin/env python3
"""scripts/speakers.py - vvread speakers: 話者一覧表示 (B-017)

VOICEVOX Engine の /speakers API から利用可能なキャラクター一覧を取得し、
style ID と名前を表示する。表示する ID は voicevox.speaker に指定できる値。

表示形式:
  ずんだもん: 3: ノーマル, 1: あまあま, 7: ツンツン
  四国めたん: 2: ノーマル, 0: あまあま

Engine 未接続時:
  Warning: ... (stderr) + exit 1

CLI:
  speakers.py [--engine-url URL]

Exit code:
  0 = 表示成功
  1 = Engine 未接続 / レスポンス異常 / 有効エントリ 0 件
  2 = 使い方エラー(argparse default)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import settings as _settings
from lib_http import http_get as _http_get


# ---------------------------------------------------------------------------
# speakers API
# ---------------------------------------------------------------------------


def _fetch_speakers(
    engine_url: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """GET /speakers して JSON をパース。

    戻り値:
      (speakers_list, None) 成功
      (None, error_msg)     失敗
    """
    url = engine_url.rstrip("/") + "/speakers"
    text, err = _http_get(url)
    if err:
        return None, err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON from /speakers: {e}"
    if not isinstance(data, list):
        return None, f"unexpected /speakers response type: {type(data).__name__}"
    return data, None


def _speakers_dto(speakers: List[Any]) -> List[Dict[str, Any]]:
    """VOICEVOX /speakers レスポンスを安定 DTO に変換。

    戻り値: [{"name": str, "styles": [{"id": int, "name": str}]}]
    top-level が list でない場合は呼び出し元が RuntimeError に変換する。
    list 内の不正要素はスキップして継続する。
    """
    result: List[Dict[str, Any]] = []
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        name = sp.get("name")
        styles = sp.get("styles")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(styles, list):
            continue
        valid_styles: List[Dict[str, Any]] = []
        for st in styles:
            if not isinstance(st, dict):
                continue
            st_id = st.get("id")
            st_name = st.get("name")
            if not isinstance(st_id, int) or not isinstance(st_name, str):
                continue
            valid_styles.append({"id": st_id, "name": st_name})
        if valid_styles:
            result.append({"name": name, "styles": valid_styles})
    return result


def _format_speakers(speakers: List[Any]) -> List[str]:
    """VOICEVOX /speakers レスポンスを表示行リストに変換。

    各キャラクターを 1 行にまとめる:
      ずんだもん: 3: ノーマル, 1: あまあま, 7: ツンツン

    - styles は API 返却順を保持（ソートしない）
    - speaker_uuid は非表示
    - 不正なエントリはスキップ（全体を落とさない）
    """
    lines = []
    for sp_item in _speakers_dto(speakers):
        style_parts = [f"{st['id']}: {st['name']}" for st in sp_item["styles"]]
        lines.append(f"{sp_item['name']}: {', '.join(style_parts)}")
    return lines


# ---------------------------------------------------------------------------
# コマンド本体
# ---------------------------------------------------------------------------


def fetch_and_display(
    engine_url: str,
    *,
    json_mode: bool = False,
    out=None,
    err=None,
) -> int:
    """engine_url から話者一覧を取得して表示する。

    json_mode=True のとき stable DTO を JSON で出力し、空配列も正常値として exit 0 を返す。
    戻り値は exit code (0/1)。
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    speakers, fetch_err = _fetch_speakers(engine_url)
    if fetch_err:
        err.write(
            "Warning: VOICEVOXと連携されていません。起動状況または設定を確認してください。\n"
            "参考: vvread doctor\n"
        )
        return 1

    if json_mode:
        out.write(json.dumps(_speakers_dto(speakers), ensure_ascii=False))
        return 0

    lines = _format_speakers(speakers)
    if not lines:
        err.write(
            "Warning: /speakers から有効な話者情報が取得できませんでした。\n"
            "参考: vvread doctor\n"
        )
        return 1

    for line in lines:
        out.write(line + "\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="VOICEVOX の利用可能な speaker 一覧を表示する"
    )
    parser.add_argument(
        "--engine-url",
        metavar="URL",
        help="VOICEVOX Engine URL (default: settings から解決)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="stable DTO を JSON 形式で出力 (MCP tool 向け)",
    )
    args = parser.parse_args(argv)

    engine_url = args.engine_url
    if not engine_url:
        loaded = _settings.load()
        val = loaded.get("voicevox.engineUrl")
        engine_url = val.value if val is not None else "http://127.0.0.1:50021"

    return fetch_and_display(engine_url, json_mode=args.json)


if __name__ == "__main__":
    sys.exit(main())
