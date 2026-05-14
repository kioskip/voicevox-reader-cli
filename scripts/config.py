#!/usr/bin/env python3
"""scripts/config.py - vvread config / edit: 設定ファイル対話編集 (B-014)

`vvread config` / `vvread edit` で vvread.settings.json を対話式に編集する。

- TTY 必須。非 TTY → ERROR exit 1
- $EDITOR は開かない。対話で値を聞いてその場で JSON に反映する
- 保存前に .bak を作成（1世代のみ）、atomic write
- project settings が存在すれば project を編集、なければ user settings を編集
- どちらも存在しなければ `vvread install` を案内して exit 1
- --yes は持たない（config は TTY 専用の対話コマンド）
- --dry-run のみ（書き込みをスキップして変更内容を表示するだけ）
- unknown keys は消さない（既存の設定を保持する）

編集対象フィールド (v0.1.2):
  基本設定: engineUrl, speaker, volume, speed, pauseScale, pitch, intonation
  Chars & Chunk: inlineCodeLimit, chunkChars, chunkHardMax, maxChars

CLI:
  config.py [--dry-run]

Exit code:
  0 = 保存成功 / 変更なし
  1 = 設定ファイル不在 / JSON 破損 / 非 TTY / 書込失敗
  2 = 使い方エラー (argparse default)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json_file as _jf
import settings as _stg

# ---------------------------------------------------------------------------
# 編集対象フィールド定義
# ---------------------------------------------------------------------------
# (dot_path, label, type, default, description)
# description は入力プロンプトの前に表示する説明文（複数行可、"\n" 区切り）

CONFIG_FIELDS: List[Tuple[str, str, type, Any, str]] = [
    # --- 基本設定 ---
    (
        "voicevox.engineUrl",
        "VOICEVOX Engine URL",
        str,
        "http://127.0.0.1:50021",
        (
            "# VOICEVOX Engine URL\n"
            "VOICEVOX Engine の接続先URLです。\n"
            "通常は http://127.0.0.1:50021 のままで問題ありません。"
        ),
    ),
    (
        "voicevox.speaker",
        "Speaker ID",
        int,
        3,
        (
            "# 話者ID\n"
            "VOICEVOX の speaker/style ID を指定します。\n"
            "例: 3=ずんだもん/ノーマル, 8=春日部つむぎ/ノーマル "
            "（`vvread speakers` で一覧出来ます）"
        ),
    ),
    (
        "voicevox.volume",
        "Volume",
        float,
        1.0,
        (
            "# 音量\n"
            "読み上げ音量です。\n"
            "1.0 が標準、0.8 は少し小さめです。目安: 0.0〜2.0"
        ),
    ),
    (
        "voicevox.speed",
        "Speed",
        float,
        1.5,
        (
            "# 速度\n"
            "読み上げ速度です。\n"
            "1.0 が等速、1.3〜1.6 はやや速めです。"
        ),
    ),
    (
        "voicevox.pauseScale",
        "Pause scale",
        float,
        1.0,
        (
            "# 句読点ポーズ長\n"
            "句読点などの間の長さです。\n"
            "小さいほどテンポよく読み上げます。目安: 0.0〜2.0"
        ),
    ),
    (
        "voicevox.pitch",
        "Pitch",
        float,
        0.0,
        (
            "# 音高\n"
            "声の高さです。\n"
            "少しだけ調整するのがおすすめです。目安: -0.15〜0.15。"
        ),
    ),
    (
        "voicevox.intonation",
        "Intonation",
        float,
        1.0,
        (
            "# 抑揚\n"
            "声の抑揚の強さです。\n"
            "0 に近いほど棒読み、1.0 が標準です。"
        ),
    ),
    # --- Chars & Chunk ---
    (
        "voicevox.inlineCodeLimit",
        "Inline code limit",
        int,
        25,
        (
            "# インラインコード読み上げ制限\n"
            "短いコードは読み上げ、長いコードは「コマンド」などに置き換えます。\n"
            "この文字数を超える inline code は省略対象になります。"
        ),
    ),
    (
        "voicevox.chunkChars",
        "Chunk chars",
        int,
        200,
        (
            "# チャンク分割の目安文字数\n"
            "長文を読み上げるとき、合成・再生しやすい単位に分割する目安です。\n"
            "小さいほど細かく分割され、再生開始が早くなる場合があります。"
        ),
    ),
    (
        "voicevox.chunkHardMax",
        "Chunk hard max",
        int,
        400,
        (
            "# チャンク分割の強制上限\n"
            "1チャンクの最大文字数です。\n"
            "この値を超えないように強制的に分割します。"
        ),
    ),
    (
        "voicevox.maxChars",
        "Max chars",
        int,
        500,
        (
            "# 読み上げ最大文字数\n"
            "1回の入力で読み上げる最大文字数です。\n"
            "この文字数を超えると「以下省略」と言って切り上げます。"
        ),
    ),
]

# ---------------------------------------------------------------------------
# DI コンテナ
# ---------------------------------------------------------------------------


@dataclass
class ConfigContext:
    """config 実行時のオプション + 環境を保持する DI コンテナ。"""
    dry_run: bool = False
    cwd: Path = field(default_factory=Path.cwd)
    in_stream: Any = None
    out_stream: Any = None
    err_stream: Any = None


# ---------------------------------------------------------------------------
# TTY チェック
# ---------------------------------------------------------------------------


def _require_tty(ctx: ConfigContext) -> Optional[str]:
    """stdin が TTY でなければ error_msg を返す。

    config には --yes がないため、非 TTY は常に ERROR。
    """
    in_stream = ctx.in_stream or sys.stdin
    isatty = getattr(in_stream, "isatty", lambda: False)
    try:
        is_tty = bool(isatty())
    except Exception:  # noqa: BLE001
        is_tty = False
    if not is_tty:
        return (
            "ERROR: vvread config requires an interactive terminal.\n"
            "This command cannot be run non-interactively."
        )
    return None


# ---------------------------------------------------------------------------
# 設定ファイル探索
# ---------------------------------------------------------------------------


def _find_settings_file(
    cwd: Path,
) -> Optional[Tuple[str, Path]]:
    """編集する vvread.settings.json を探す。

    優先順位:
      1. <cwd>/vvread.settings.json  → ("project", path)
      2. user settings               → ("user", path)
    どちらも存在しなければ None。
    """
    project = _stg.project_settings_path(cwd)
    if project.exists():
        return "project", project
    user = _stg.user_settings_path()
    if user.exists():
        return "user", user
    return None


# ---------------------------------------------------------------------------
# JSON 読み込み / 書き込み
# ---------------------------------------------------------------------------


def _load_vvread_settings(
    path: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """vvread.settings.json を読み込む。

    戻り値:
      (data, None)   成功（data は {} の可能性あり）
      (None, errmsg) 読み取りエラー / JSON 破損
    """
    data, err = _jf.load_json_file(path)
    if err:
        return None, err
    if data is None:
        return {}, None
    return data, None


def _unflatten(flat: Dict[str, Any]) -> Dict[str, Any]:
    """dot-path → ネスト dict に変換する。

    例: {"voicevox.speaker": 3} → {"voicevox": {"speaker": 3}}
    """
    result: Dict[str, Any] = {}
    for dot_path, value in flat.items():
        parts = dot_path.split(".")
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


def _flatten(nested: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """ネスト dict を dot-path flat dict に変換する。"""
    result: Dict[str, Any] = {}
    for key, value in nested.items():
        dot_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten(value, dot_path))
        else:
            result[dot_path] = value
    return result


def _save_vvread_settings(
    path: Path,
    data: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    """bak を作ってから atomic write する。"""
    if dry_run:
        return
    _jf.backup_file(path)
    _jf.write_json_atomic(path, data)


# ---------------------------------------------------------------------------
# 対話 helper
# ---------------------------------------------------------------------------


def _prompt(ctx: ConfigContext, question: str) -> str:
    """1 行入力を受け取る。EOF / 空行は "" を返す。"""
    out = ctx.out_stream or sys.stdout
    in_ = ctx.in_stream or sys.stdin
    out.write(question)
    out.flush()
    line = in_.readline()
    if not line:
        return ""
    return line.strip()


def _prompt_field(
    dot_path: str,
    label: str,
    typ: type,
    current: Any,
    description: str,
    *,
    ctx: ConfigContext,
) -> Any:
    """1 フィールドを対話で入力する。

    - 入力前に description を表示する
    - Enter のみ → 現在値を返す
    - 入力値は typ に型変換する
    - 型変換失敗 → エラーを表示して再入力
    """
    out = ctx.out_stream or sys.stdout
    out.write(f"\n{description}\n")
    while True:
        raw = _prompt(ctx, f"{label} [現在: {current}]: ")
        if raw == "":
            return current
        try:
            if typ is int:
                converted = int(raw)
            elif typ is float:
                converted = float(raw)
            else:
                converted = raw
            return converted
        except (ValueError, TypeError):
            out.write(f"  Invalid value (expected {typ.__name__}). Try again.\n")


def _prompt_yes_no(ctx: ConfigContext, question: str, default: bool = True) -> bool:
    """Y/n プロンプト。"""
    suffix = "[Y/n]" if default else "[y/N]"
    raw = _prompt(ctx, f"{question} {suffix}: ")
    if not raw:
        return default
    return raw.lower() in ("y", "yes", "1", "true")


# ---------------------------------------------------------------------------
# run_config: メイン処理
# ---------------------------------------------------------------------------


def run_config(ctx: ConfigContext) -> int:
    """設定ファイルを対話編集して保存する。

    戻り値は exit code (0/1)。
    """
    out = ctx.out_stream or sys.stdout
    err = ctx.err_stream or sys.stderr

    # TTY チェック
    tty_err = _require_tty(ctx)
    if tty_err:
        err.write(tty_err + "\n")
        return 1

    # 設定ファイルを探す
    found = _find_settings_file(ctx.cwd)
    if found is None:
        err.write(
            "No vvread settings file found.\n"
            "Run `vvread install` or `vvread setup` first to create settings.\n"
        )
        return 1

    scope_label, settings_path = found
    out.write(f"設定ファイル: {settings_path}\n")

    # 読み込み
    data, load_err = _load_vvread_settings(settings_path)
    if load_err:
        err.write(f"ERROR: {settings_path}: {load_err}\n")
        return 1

    # 現在の flat dict を作成（unknown keys も保持）
    flat_current = _flatten(data)

    # 各フィールドを対話編集
    new_flat: Dict[str, Any] = {}
    for dot_path, label, typ, default, description in CONFIG_FIELDS:
        current = flat_current.get(dot_path, default)
        new_val = _prompt_field(dot_path, label, typ, current, description, ctx=ctx)
        new_flat[dot_path] = new_val

    # 変更サマリ
    _field_defaults = {f[0]: f[3] for f in CONFIG_FIELDS}
    changes: Dict[str, Tuple[Any, Any]] = {
        k: (flat_current.get(k, _field_defaults[k]), v)
        for k, v in new_flat.items()
        if v != flat_current.get(k, _field_defaults[k])
    }

    if not changes:
        out.write("\n変更なし。\n")
        return 0

    out.write("\n変更内容:\n")
    for k, (old, new) in changes.items():
        out.write(f"  {k}: {old} → {new}\n")

    # 保存確認
    if not _prompt_yes_no(ctx, f"\n{settings_path} に保存しますか？"):
        out.write("キャンセルしました。\n")
        return 0

    # 既存の data（unknown keys 含む）に変更を上書きマージ
    merged_flat = flat_current.copy()
    merged_flat.update(new_flat)
    new_data = _unflatten(merged_flat)

    if ctx.dry_run:
        out.write("[dry-run] 保存はスキップしました。\n")
        return 0

    try:
        _save_vvread_settings(settings_path, new_data)
    except OSError as e:
        err.write(f"ERROR: {settings_path}: cannot write: {e}\n")
        return 1

    out.write(f"保存しました: {settings_path}\n")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="vvread.settings.json を対話式に編集する"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="変更内容を表示するだけで保存しない",
    )
    args = parser.parse_args(argv)

    ctx = ConfigContext(dry_run=args.dry_run)
    return run_config(ctx)


if __name__ == "__main__":
    sys.exit(main())
