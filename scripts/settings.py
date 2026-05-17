#!/usr/bin/env python3
"""scripts/settings.py - vvread の設定値解決層 (R-025)

優先順位: env > project > user > default

  CLI option(コンシューマー側) > 環境変数 > project (`<cwd>/vvread.settings.json`)
  > user (macOS: `~/Library/Application Support/vvread/settings.json` /
   Linux/WSL: `${XDG_CONFIG_HOME:-~/.config}/vvread/settings.json`) > default

R-025 では env 以下のレイヤー解決のみ実装する(CLI option レイヤーは consumer
= R-009 doctor / R-010 setup の責務)。既存スクリプト (cmd_say / cmd_synth /
cmd_on_stop) の env 直読みは migration スコープ外で、本モジュールは consumer
側 (doctor / setup) に新規実装で取り込まれる前提。

スキーマは `voicevox.* / log.* / notify.*` のみ。`paths.*` は R-001 の
VVREAD_*_DIR env override と二重経路になるため v0.1 では schema から外す。

JSONC は line comment (`//`) のみ対応。block comment (`/* */`) は YAGNI。

不明キー / parse error は Settings.unknown_keys / parse_errors に蓄積され、
doctor (R-009) が warning として表示する。本モジュールは fatal にせず、
解決可能な範囲は default fallback で進める(forward compat 重視)。

CLI:
  python settings.py get <dot.path> [--with-origin]
  python settings.py list [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from constants import (
    CHUNK_CHARS_DEFAULT,
    CHUNK_HARD_MAX_DEFAULT,
    INLINE_CODE_LIMIT_DEFAULT,
    MAX_CHARS_DEFAULT,
    MAX_CHUNKS_DEFAULT,
)

# ---------------------------------------------------------------------------
# スキーマ定義
# ---------------------------------------------------------------------------
# dot-path → (default value, env var name, target type)
# env_var を None にすると env override 無効(現状なし、将来用の余地)。
SCHEMA: Dict[str, Tuple[Any, Optional[str], type]] = {
    # voicevox 発話パラメータ
    "voicevox.engineUrl":   ("http://127.0.0.1:50021", "VOICEVOX_ENGINE_URL", str),
    "voicevox.speaker":     (3,    "VOICEVOX_SPEAKER", int),
    "voicevox.speed":       (1.5,  "VOICEVOX_SPEED", float),
    "voicevox.pitch":       (0.0,  "VOICEVOX_PITCH", float),
    "voicevox.intonation":  (1.0,  "VOICEVOX_INTONATION", float),
    "voicevox.volume":      (1.0,  "VOICEVOX_VOLUME", float),
    "voicevox.pauseScale":  (1.0,  "VOICEVOX_PAUSE_SCALE", float),
    "voicevox.prePhoneme":  (0.0,  "VOICEVOX_PRE_PHONEME", float),
    "voicevox.postPhoneme": (0.0,  "VOICEVOX_POST_PHONEME", float),
    "voicevox.maxChars":         (MAX_CHARS_DEFAULT,         "VOICEVOX_MAX_CHARS",         int),
    "voicevox.maxChunks":        (MAX_CHUNKS_DEFAULT,        "VOICEVOX_MAX_CHUNKS",        int),
    "voicevox.chunkChars":       (CHUNK_CHARS_DEFAULT,       "VOICEVOX_CHUNK_CHARS",       int),
    "voicevox.chunkHardMax":     (CHUNK_HARD_MAX_DEFAULT,    "VOICEVOX_CHUNK_HARD_MAX",    int),
    "voicevox.inlineCodeLimit":  (INLINE_CODE_LIMIT_DEFAULT, "VOICEVOX_INLINE_CODE_LIMIT", int),
    # ログ
    "log.level":            ("INFO",   "VOICEVOX_LOG_LEVEL", str),
    "log.maxBytes":         (10485760, "VOICEVOX_LOG_MAX_BYTES", int),
    # 通知
    "notify.cooldownSec":   (60, "VOICEVOX_NOTIFY_COOLDOWN", int),
}


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass
class Origin:
    """値の由来。doctor が「この値はどこから来たか」を表示するために使う。"""
    source: str  # "env" / "project" / "user" / "default"
    detail: Optional[str] = None  # env 名 or 設定ファイルパス


@dataclass
class ResolvedValue:
    value: Any
    origin: Origin


@dataclass
class Settings:
    """解決結果 + 副次情報。

    values        : スキーマ済キーすべての解決値(必ず default まで埋まる)
    unknown_keys  : 設定ファイルにあったがスキーマに無いキー(forward compat
                    のため値としては無視するが doctor で warning 表示)
    parse_errors  : JSON 不正 / 型変換失敗 / ファイル読み取り失敗等(doctor
                    で warning 表示し、当該レイヤーは default に fallback)
    sources       : ロード元のファイルパス(存在したもののみ。doctor 表示用)
    """
    values: Dict[str, ResolvedValue] = field(default_factory=dict)
    unknown_keys: List[Tuple[str, str]] = field(default_factory=list)
    parse_errors: List[Tuple[str, str]] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def get(self, dot_path: str) -> Optional[ResolvedValue]:
        return self.values.get(dot_path)


# ---------------------------------------------------------------------------
# JSONC ライン コメント除去
# ---------------------------------------------------------------------------


def _strip_jsonc_line_comments(text: str) -> str:
    """`//` で始まる行コメントを除去。string リテラル内の `//` は保護する。

    string 検出は単純な「直前文字 != '\\\\' のダブルクォート切替」のみ。
    JSON 文字列に含まれる本物の escape は ('\\\\"' = エスケープされた quote)。
    block comment (`/* */`) は v0.1 サポート外。
    """
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            # 直前が \ なら escape された quote(in_string 切替えない)
            if i > 0 and text[i - 1] == "\\":
                pass
            else:
                in_string = not in_string
            out.append(c)
            i += 1
            continue
        if not in_string and c == "/" and i + 1 < n and text[i + 1] == "/":
            # 行末まで comment、改行は保持(行番号合わせ)
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# パス解決
# ---------------------------------------------------------------------------


def project_settings_path(cwd: Optional[Path] = None) -> Path:
    if cwd is None:
        cwd = Path.cwd()
    return cwd / "vvread.settings.json"


def user_settings_path() -> Path:
    # OS 判定は scripts/paths.py (R-001) と完全一致させる:
    # paths.py は `platform.system() == "Darwin"` で判定し、bash 側
    # lib_paths.sh は `uname -s == "Darwin"` で判定する(同じ結果)。
    # ここで `sys.platform == "darwin"` 等の別系統を使うと doctor の
    # 表示と paths 解決が将来食い違うリスクがあるため敢えて重ねる。
    if platform.system() == "Darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "vvread"
            / "settings.json"
        )
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vvread" / "settings.json"


# ---------------------------------------------------------------------------
# JSONC ファイル読込
# ---------------------------------------------------------------------------


def _read_settings_file(
    path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """ファイルを読んで dict を返す。エラー時は (None, error_msg)、
    ファイル不在は (None, None)(エラーではない)。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"cannot read: {e}"

    stripped = _strip_jsonc_line_comments(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"

    if not isinstance(data, dict):
        return None, "top-level must be an object"
    return data, None


def _flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """ネストされた dict を dot-path に flatten する。

    list やプリミティブ値が中間ノードに来た場合はそこで打ち切り(dot-path 末端)。
    """
    flat: Dict[str, Any] = {}
    if not isinstance(obj, dict):
        return flat
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            flat.update(_flatten(v, key))
        else:
            flat[key] = v
    return flat


# ---------------------------------------------------------------------------
# 型変換
# ---------------------------------------------------------------------------


def _coerce(value: Any, target: type) -> Tuple[Any, bool]:
    """value を target 型に変換。失敗時は (None, False)。

    JSON のネイティブ型(int / float / str / bool)は基本的にそのまま受ける。
    env から来る str は int/float/str に変換する。
    """
    if isinstance(value, target):
        # int/bool 区別: target=int で value=True は許容しない(意図しない数値化)
        if target is int and isinstance(value, bool):
            return None, False
        return value, True
    if target is int:
        try:
            return int(value), True
        except (ValueError, TypeError):
            return None, False
    if target is float:
        try:
            return float(value), True
        except (ValueError, TypeError):
            return None, False
    if target is str:
        # bool は str 化を許容しない(意図しない "True" 化)
        if isinstance(value, bool):
            return None, False
        return str(value), True
    return None, False


# ---------------------------------------------------------------------------
# load 本体
# ---------------------------------------------------------------------------


def load(
    cwd: Optional[Path] = None,
    *,
    env: Optional[Dict[str, str]] = None,
    user_path: Optional[Path] = None,
    project_path: Optional[Path] = None,
) -> Settings:
    """env > project > user > default で設定を解決する。

    cwd は project settings の探索開始点。env はテストで os.environ を上書き
    したい場合に注入する用(default は os.environ)。
    user_path / project_path は単体テストで OS デフォルトを上書きする用。
    """
    if env is None:
        env = dict(os.environ)
    if cwd is None:
        cwd = Path.cwd()
    if user_path is None:
        user_path = user_settings_path()
    if project_path is None:
        project_path = project_settings_path(cwd)

    settings = Settings()

    user_data, user_err = _read_settings_file(user_path)
    if user_err:
        settings.parse_errors.append((str(user_path), user_err))
    elif user_data is not None:
        settings.sources.append(str(user_path))

    project_data, project_err = _read_settings_file(project_path)
    if project_err:
        settings.parse_errors.append((str(project_path), project_err))
    elif project_data is not None:
        settings.sources.append(str(project_path))

    user_flat = _flatten(user_data) if user_data else {}
    project_flat = _flatten(project_data) if project_data else {}

    # 不明キー収集(known キーは下の cascade で消費される)
    known_keys = set(SCHEMA.keys())
    for fname, flat in (
        (str(user_path), user_flat),
        (str(project_path), project_flat),
    ):
        for key in flat:
            if key not in known_keys:
                settings.unknown_keys.append((fname, key))

    # スキーマ各キーを優先順位で解決
    for key, (default, env_var, target_type) in SCHEMA.items():
        resolved: Optional[ResolvedValue] = None

        # env
        if env_var and env.get(env_var) is not None:
            raw = env[env_var]
            coerced, ok = _coerce(raw, target_type)
            if ok:
                resolved = ResolvedValue(coerced, Origin("env", env_var))
            else:
                settings.parse_errors.append(
                    (env_var,
                     f"value {raw!r} is not coercible to "
                     f"{target_type.__name__}")
                )
        # S-008: VOICEVOX_ENGINE (legacy) → voicevox.engineUrl フォールバック
        elif key == "voicevox.engineUrl" and env.get("VOICEVOX_ENGINE") is not None:
            raw = env["VOICEVOX_ENGINE"]
            coerced, ok = _coerce(raw, str)
            if ok:
                resolved = ResolvedValue(coerced, Origin("env", "VOICEVOX_ENGINE"))
            else:
                settings.parse_errors.append(
                    ("VOICEVOX_ENGINE",
                     f"value {raw!r} is not coercible to str")
                )

        # project
        if resolved is None and key in project_flat:
            raw = project_flat[key]
            coerced, ok = _coerce(raw, target_type)
            if ok:
                resolved = ResolvedValue(
                    coerced, Origin("project", str(project_path))
                )
            else:
                settings.parse_errors.append(
                    (str(project_path),
                     f"{key}: value {raw!r} is not "
                     f"{target_type.__name__}")
                )

        # user
        if resolved is None and key in user_flat:
            raw = user_flat[key]
            coerced, ok = _coerce(raw, target_type)
            if ok:
                resolved = ResolvedValue(
                    coerced, Origin("user", str(user_path))
                )
            else:
                settings.parse_errors.append(
                    (str(user_path),
                     f"{key}: value {raw!r} is not "
                     f"{target_type.__name__}")
                )

        # default
        if resolved is None:
            resolved = ResolvedValue(default, Origin("default"))

        settings.values[key] = resolved

    return settings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_get(args: argparse.Namespace) -> int:
    settings = load()
    rv = settings.get(args.path)
    if rv is None:
        print(f"settings: unknown key: {args.path}", file=sys.stderr)
        return 1
    if args.with_origin:
        payload = {
            "value": rv.value,
            "origin": rv.origin.source,
            "detail": rv.origin.detail,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        # bash 側で `$(settings.py get ...)` できるよう改行 1 個 + plain 値
        print(rv.value)
    return 0


def _cmd_env(args: argparse.Namespace) -> int:
    settings = load()
    for key, (default, env_var, _) in SCHEMA.items():
        if not env_var:
            continue
        rv = settings.values[key]
        # 常にシングルクォートで囲む(数値・URLでも安全、将来の特殊文字にも対応)
        val = str(rv.value).replace("'", "'\"'\"'")
        print(f"export {env_var}='{val}'")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    settings = load()
    if args.json:
        payload = {
            "values": {
                k: {
                    "value": v.value,
                    "origin": v.origin.source,
                    "detail": v.origin.detail,
                }
                for k, v in settings.values.items()
            },
            "unknown_keys": [
                {"file": f, "key": k} for f, k in settings.unknown_keys
            ],
            "parse_errors": [
                {"source": s, "message": m} for s, m in settings.parse_errors
            ],
            "sources": settings.sources,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for k in sorted(settings.values.keys()):
            rv = settings.values[k]
            detail = f" ({rv.origin.detail})" if rv.origin.detail else ""
            print(f"{k} = {rv.value!r} [{rv.origin.source}{detail}]")
        if settings.unknown_keys:
            print("\nUnknown keys:", file=sys.stderr)
            for f, k in settings.unknown_keys:
                print(f"  {k} (in {f})", file=sys.stderr)
        if settings.parse_errors:
            print("\nParse errors:", file=sys.stderr)
            for s, m in settings.parse_errors:
                print(f"  {s}: {m}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vvread settings resolver"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_get = sub.add_parser("get", help="get a single value by dot.path")
    p_get.add_argument("path", help="dot path (e.g. voicevox.engineUrl)")
    p_get.add_argument(
        "--with-origin",
        action="store_true",
        help="emit JSON with value+origin+detail instead of plain value",
    )
    p_get.set_defaults(func=_cmd_get)

    p_list = sub.add_parser("list", help="list all known keys with origin")
    p_list.add_argument(
        "--json",
        action="store_true",
        help="emit as JSON (default: plain text + warnings to stderr)",
    )
    p_list.set_defaults(func=_cmd_list)

    p_env = sub.add_parser(
        "env",
        help="emit VOICEVOX_*=value lines for bash eval (env > project > user > default)",
    )
    p_env.set_defaults(func=_cmd_env)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
