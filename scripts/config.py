#!/usr/bin/env python3
"""scripts/config.py - vvread config / edit: 設定ファイル編集 (B-014, B-107, B-109)

`vvread config` / `vvread edit` で vvread.settings.json を編集する。

対話モード (TTY 必須):
  設定ファイルの各フィールドを順番に入力して保存する。

非対話モード (--set / --json):
  TTY 不要。スクリプト・CI・Claude Code Stop hook から直接値を書き込める。
  設定ファイルが存在しない場合は {} から自動作成する（--create 不要）。

CLI:
  config.py [--dry-run] [--create] [--user-setting]
            [--set KEY=VALUE ...] [--json JSON]

Exit code:
  0 = 成功 / 変更なし / dry-run 完了
  1 = 入力エラー / 型エラー / JSON エラー / 書込失敗 / 非 TTY（対話モード）
  2 = 使い方エラー (argparse default)
"""
from __future__ import annotations

import argparse
import json
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
    (
        "voicevox.maxChunks",
        "Max chunks",
        int,
        0,
        (
            "# 最大チャンク数\n"
            "1回の入力で生成するチャンク数の上限です。\n"
            "この数を超えると最後に「以下省略」を付けて読み上げを打ち切ります。\n"
            "0 を指定すると上限なし。"
        ),
    ),
]

# 既知のトップレベルセクション（SCHEMA から導出）
_KNOWN_SECTIONS: frozenset = frozenset(k.split(".")[0] for k in _stg.SCHEMA)

# ---------------------------------------------------------------------------
# DI コンテナ
# ---------------------------------------------------------------------------


@dataclass
class ConfigContext:
    """config 実行時のオプション + 環境を保持する DI コンテナ。"""
    dry_run: bool = False
    create: bool = False
    user_setting: bool = False
    set_pairs: List[str] = field(default_factory=list)
    json_patch: Optional[str] = None
    list_mode: bool = False
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
            "エラー: vvread config は対話型端末が必要です。\n"
            "このコマンドは非対話的に実行できません。"
        )
    return None


# ---------------------------------------------------------------------------
# 設定ファイル探索
# ---------------------------------------------------------------------------


def _find_settings_file(
    cwd: Path,
    *,
    user_setting: bool = False,
) -> Optional[Tuple[str, Path]]:
    """編集する vvread.settings.json を探す。

    user_setting=True の場合はユーザー設定ファイルのみを対象とする。
    それ以外は project → user の優先順位で探す。
    どちらも存在しなければ None。
    """
    if user_setting:
        user = _stg.user_settings_path()
        return ("user", user) if user.exists() else None
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
# 非対話モード: バリデーション / パッチ適用ヘルパー
# ---------------------------------------------------------------------------


def _validate_key(key: str) -> None:
    """--set のキーを検証する。不正な場合は ValueError を raise。"""
    if not key:
        raise ValueError("キーが空です")
    if key.startswith(".") or key.endswith("."):
        raise ValueError(f"キーの先頭・末尾にドットは使えません: {key!r}")
    if ".." in key:
        raise ValueError(f"キーに連続したドットは使えません: {key!r}")


def _coerce_str_value(key: str, raw: str) -> Any:
    """--set の値文字列を SCHEMA の型に変換する。unknown key は str として保存。"""
    if not raw:
        raise ValueError(f"値が空です (key: {key!r})")
    if key not in _stg.SCHEMA:
        return raw
    _, _, typ = _stg.SCHEMA[key]
    try:
        if typ is int:
            return int(raw)
        if typ is float:
            return float(raw)
        return raw
    except (ValueError, TypeError):
        raise ValueError(f"{key}: {typ.__name__} に変換できません: {raw!r}") from None


def _validate_typed_value(key: str, value: Any) -> Any:
    """--json の型済み値を SCHEMA に対して検証する。unknown key はそのまま通す。"""
    if value is None:
        raise ValueError(f"null は許可されていません (key: {key!r})")
    if key not in _stg.SCHEMA:
        return value
    _, _, expected_type = _stg.SCHEMA[key]
    # bool は int のサブクラスなので先にチェック
    if isinstance(value, bool):
        raise TypeError(f"{key}: bool は使用できません")
    if expected_type is float:
        if not isinstance(value, (int, float)):
            raise TypeError(
                f"{key}: number が必要ですが {type(value).__name__} が指定されました"
            )
        return float(value)
    if not isinstance(value, expected_type):
        raise TypeError(
            f"{key}: {expected_type.__name__} が必要ですが {type(value).__name__} が指定されました"
        )
    return value


def _validate_json_patch(patch: Any) -> Dict[str, Any]:
    """--json の入力を検証しフラット dict を返す。"""
    if not isinstance(patch, dict):
        raise ValueError(
            f"--json: トップレベルは object である必要があります (got {type(patch).__name__})"
        )
    for sec_key, sec_val in patch.items():
        if sec_key in _KNOWN_SECTIONS and not isinstance(sec_val, dict):
            raise ValueError(
                f"--json: '{sec_key}' は object である必要があります (got {type(sec_val).__name__})"
            )
    flat = _flatten(patch)
    result: Dict[str, Any] = {}
    for key, value in flat.items():
        result[key] = _validate_typed_value(key, value)
    return result


def _apply_patches(
    flat_current: Dict[str, Any],
    set_pairs: List[str],
    json_patch: Optional[str],
) -> Dict[str, Any]:
    """--json → --set の順でパッチを適用する。既存キーは保持する。"""
    result = flat_current.copy()

    if json_patch is not None:
        try:
            patch_obj = json.loads(json_patch)
        except json.JSONDecodeError as e:
            raise ValueError(f"--json: JSON パースエラー: {e}") from e
        patch_flat = _validate_json_patch(patch_obj)
        result.update(patch_flat)

    for pair in set_pairs:
        if "=" not in pair:
            raise ValueError(f"--set: 'KEY=VALUE' の形式で指定してください: {pair!r}")
        key, _, raw_value = pair.partition("=")
        _validate_key(key)
        result[key] = _coerce_str_value(key, raw_value)

    return result


def _show_dry_run_diff(
    out: Any,
    settings_path: Path,
    flat_before: Dict[str, Any],
    flat_after: Dict[str, Any],
    *,
    was_missing: bool,
) -> None:
    """dry-run 時の変更予定を表示する。"""
    if was_missing:
        out.write(f"Would create: {settings_path}\n")
    for key, new_val in flat_after.items():
        old_val = flat_before.get(key)
        if old_val != new_val:
            old_display = "<unset>" if key not in flat_before else old_val
            out.write(f"{key}: {old_display} -> {new_val}\n")


# ---------------------------------------------------------------------------
# run_config: メイン処理
# ---------------------------------------------------------------------------


def run_config(ctx: ConfigContext) -> int:
    """設定ファイルを編集して保存する。

    --set / --json が指定された場合は非対話モード（TTY 不要）。
    それ以外は TTY 必須の対話モード。
    戻り値は exit code (0/1)。
    """
    out = ctx.out_stream or sys.stdout
    err = ctx.err_stream or sys.stderr

    # -----------------------------------------------------------------------
    # --list モード: 全設定キーと cascade 解決値を表示
    # -----------------------------------------------------------------------
    if ctx.list_mode:
        settings = _stg.load(ctx.cwd)
        for key in _stg.SCHEMA:
            rv = settings.get(key)
            val = rv.value if rv else ""
            out.write(f"{key}\t{val}\n")
        return 0

    non_interactive = bool(ctx.set_pairs or ctx.json_patch)

    # -----------------------------------------------------------------------
    # 非対話モード (B-109)
    # -----------------------------------------------------------------------
    if non_interactive:
        if ctx.user_setting:
            settings_path = _stg.user_settings_path()
        else:
            found = _find_settings_file(ctx.cwd)
            settings_path = found[1] if found is not None else ctx.cwd / "vvread.settings.json"

        was_missing = not settings_path.exists()

        if not was_missing:
            data, load_err = _load_vvread_settings(settings_path)
            if load_err:
                err.write(f"ERROR: {settings_path}: {load_err}\n")
                return 1
        else:
            data = {}

        flat_current = _flatten(data)

        try:
            merged_flat = _apply_patches(flat_current, ctx.set_pairs, ctx.json_patch)
        except (ValueError, TypeError) as e:
            err.write(f"ERROR: {e}\n")
            return 1

        if ctx.dry_run:
            _show_dry_run_diff(out, settings_path, flat_current, merged_flat,
                               was_missing=was_missing)
            return 0

        new_data = _unflatten(merged_flat)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _save_vvread_settings(settings_path, new_data)
        except OSError as e:
            err.write(f"ERROR: {settings_path}: cannot write: {e}\n")
            return 1

        out.write(f"Updated: {settings_path}\n")
        return 0

    # -----------------------------------------------------------------------
    # 対話モード (B-014, B-107)
    # -----------------------------------------------------------------------

    # TTY チェック
    tty_err = _require_tty(ctx)
    if tty_err:
        err.write(tty_err + "\n")
        return 1

    # 設定ファイルを探す
    found = _find_settings_file(ctx.cwd, user_setting=ctx.user_setting)
    if found is None:
        if ctx.create:
            path = (
                _stg.user_settings_path() if ctx.user_setting
                else ctx.cwd / "vvread.settings.json"
            )
            if ctx.dry_run:
                out.write(f"DRY-RUN: {path} を新規作成します\n")
                return 0
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            found = ("user" if ctx.user_setting else "project", path)
        else:
            err.write(
                "設定ファイルが見つかりません。\n"
                "以下のいずれかで作成してください:\n"
                "  vvread config --create   # 設定ファイルを新規作成して編集を開始\n"
                "  vvread setup             # 対話式セットアップで設定ファイルを作成\n"
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
        description="vvread.settings.json を編集する"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="変更内容を表示するだけで保存しない",
    )
    parser.add_argument(
        "--create", action="store_true",
        help="設定ファイルが存在しない場合は新規作成してから編集を開始（対話モード専用）",
    )
    parser.add_argument(
        "--user-setting", action="store_true",
        help="プロジェクト設定の代わりにユーザー設定ファイルを対象にする",
    )
    parser.add_argument(
        "--set", dest="set_pairs", action="append", metavar="KEY=VALUE",
        default=[],
        help="非対話モード: 単一キーを設定する（複数指定可、後勝ち）",
    )
    parser.add_argument(
        "--json", dest="json_patch", metavar="JSON",
        help="非対話モード: JSON オブジェクトで複数キーを一括設定する",
    )
    parser.add_argument(
        "--list", dest="list_mode", action="store_true",
        help="設定可能な全キーと現在値を表示する（非対話、非TTYでも実行可能）",
    )
    args = parser.parse_args(argv)

    ctx = ConfigContext(
        dry_run=args.dry_run,
        create=args.create,
        user_setting=args.user_setting,
        set_pairs=args.set_pairs or [],
        json_patch=args.json_patch,
        list_mode=args.list_mode,
    )
    return run_config(ctx)


if __name__ == "__main__":
    sys.exit(main())
