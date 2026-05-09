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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加（settings 等を直 import するため）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import settings as _settings  # noqa: E402

# ---------------------------------------------------------------------------
# HTTP helper（doctor.py と同じシグネチャ。共有ヘルパー化は v0.1.2 では行わない）
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 3.0) -> Tuple[Optional[str], Optional[str]]:
    """簡易 HTTP GET。成功時は (text, None)、失敗時は (None, error_msg)"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.URLError as e:
        return None, f"URL error: {e}"
    except (TimeoutError, OSError) as e:
        return None, f"connection error: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"unexpected: {e}"


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


def _format_speakers(speakers: List[Any]) -> List[str]:
    """VOICEVOX /speakers レスポンスを表示行リストに変換。

    各キャラクターを 1 行にまとめる:
      ずんだもん: 3: ノーマル, 1: あまあま, 7: ツンツン

    - styles は API 返却順を保持（ソートしない）
    - speaker_uuid は非表示
    - 不正なエントリはスキップ（全体を落とさない）
    """
    lines = []
    for sp in speakers:
        if not isinstance(sp, dict):
            continue
        name = sp.get("name")
        styles = sp.get("styles")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(styles, list):
            continue
        style_parts = []
        for st in styles:
            if not isinstance(st, dict):
                continue
            st_id = st.get("id")
            st_name = st.get("name")
            if not isinstance(st_id, int) or not isinstance(st_name, str):
                continue
            style_parts.append(f"{st_id}: {st_name}")
        if not style_parts:
            continue
        lines.append(f"{name}: {', '.join(style_parts)}")
    return lines


# ---------------------------------------------------------------------------
# コマンド本体
# ---------------------------------------------------------------------------


def fetch_and_display(
    engine_url: str,
    *,
    out=None,
    err=None,
) -> int:
    """engine_url から話者一覧を取得して表示する。

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
    args = parser.parse_args(argv)

    engine_url = args.engine_url
    if not engine_url:
        loaded = _settings.load()
        val = loaded.get("voicevox.engineUrl")
        engine_url = val.value if val is not None else "http://127.0.0.1:50021"

    return fetch_and_display(engine_url)


if __name__ == "__main__":
    sys.exit(main())
