#!/usr/bin/env python3
"""scripts/hook_install.py - vvread install / uninstall (R-008)

Claude Code の Stop hook (`<scope>/.claude/settings.json` 等)に voiceClaude
の hook entry を追加 / 削除する。

設計方針(R-008 ユーザ仕様確定):
  - jq には依存しない(R-024 で jq は optional setup 扱い、install を必須に
    すると矛盾)。Python `json` モジュールのみで read/write
  - install は新規追加だけ責務とする(legacy `scripts/on_stop.sh` の自動
    置換は v0.1 では実装しない、ERROR + uninstall 案内方式)
  - uninstall は `is_voiceclaude_hook` で判定した hook のみ削除、他は保持
  - JSON 破損は ERROR で停止(自動修復しない、ユーザに修正させる)
  - `.bak` は毎回上書き(世代管理は v0.1 では不要、git 管理前提)
  - `--yes` は v0.1 では受理のみ(setup R-011 で対話 prompt が来る将来用)
  - `--dry-run` で書き込み一切なし(`.bak` も作らない)
  - doctor は診断のみ、install/uninstall が変更担当 → 責務分離

`is_voiceclaude_hook` は doctor.py の hook 検出と同一ロジック。本ファイルに
正本を置き、doctor.py は import で再利用する(drift 防止)。

CLI:
  hook_install.py install [--scope project-local|project|user] [--dry-run] [--yes]
  hook_install.py uninstall [--scope project-local|project|user] [--dry-run]

終了コード:
  0 = 成功(変更なしも成功)
  1 = 実行エラー(JSON 破損 / 書込不可 / legacy 検出 / scope 不正等)
  2 = 使い方エラー(不正オプション、argparse default)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Tuple

import json_file as _jf
from hook_status import is_voiceclaude_hook, resolve_settings_path
from lib_prompt import (
    is_tty as _is_tty,
    prompt_choice as _prompt_choice,
    prompt_speaker_id as _prompt_speaker_id,
    prompt_yn as _prompt_yn,
)

# ---------------------------------------------------------------------------
# 定数 / 仕様
# ---------------------------------------------------------------------------

SCOPES = ("project-local", "project", "user")
DEFAULT_SCOPE = "project-local"

# v0.1.2 で旧 scope 名を deprecated alias として受け付ける。
# 旧 "project" → settings.local.json は新 "project-local" に対応する（CHANGELOG 参照）。
_DEPRECATED_SCOPE_ALIASES: Dict[str, str] = {
    "project-shared": "project",
}

# Claude Code 2.1.110+ で async hook を使うための既定値(Backlog 確定事項)。
HOOK_TIMEOUT_DEFAULT = 600
HOOK_ASYNC_DEFAULT = True


# ---------------------------------------------------------------------------
# scope path 解決（hook_status.py に移動済み、ここからは re-export）
# ---------------------------------------------------------------------------

# resolve_settings_path は hook_status から import 済み。


def _in_git_repo(cwd: Optional[Path] = None) -> bool:
    """cwd が git リポジトリ配下かどうかを確認する。"""
    import subprocess  # noqa: PLC0415
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


def _resolve_scope_alias(scope: str) -> Tuple[str, Optional[str]]:
    """deprecated scope alias を解決する。

    戻り値: (resolved_scope, warning_msg | None)
    alias でなければ (scope, None) をそのまま返す。
    """
    if scope in _DEPRECATED_SCOPE_ALIASES:
        resolved = _DEPRECATED_SCOPE_ALIASES[scope]
        msg = (
            f"WARNING: --scope {scope!r} is deprecated. "
            f"Use --scope {resolved!r} instead."
        )
        return resolved, msg
    return scope, None


# ---------------------------------------------------------------------------
# vvread hook 判定（hook_status.py に移動済み、ここからは re-export）
# ---------------------------------------------------------------------------

# is_voiceclaude_hook は hook_status から import 済み。


# ---------------------------------------------------------------------------
# hook command 文字列の組み立て
# ---------------------------------------------------------------------------


def build_hook_command(repo_root: Path) -> str:
    """`<repo>/bin/vvread on-stop` を絶対パス + 空白対応で組み立てる。

    Claude Code は hook の `command` field を shell 評価する想定なので、
    空白を含むパスはダブルクォートで囲む。空白を含まなければプレーン形式。

    `shlex.quote` を使うとシングルクォートで囲まれて読みにくいため、
    手動でダブルクォートで囲む方針(設定ファイル可読性優先)。
    """
    bin_path = repo_root / "bin" / "vvread"
    s = str(bin_path)
    # ダブルクォート / バックスラッシュを含むパスは shlex.quote にフォール
    # バックする(エッジケース、通常の絶対パスでは出てこない想定)
    if '"' in s or "\\" in s:
        return f"{shlex.quote(s)} on-stop"
    if " " in s or "\t" in s:
        return f'"{s}" on-stop'
    return f"{s} on-stop"


# ---------------------------------------------------------------------------
# JSON read / write
# ---------------------------------------------------------------------------


def _read_settings(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """ファイルを読んで dict を返す。json_file.load_json_file に委譲。"""
    return _jf.load_json_file(path)


def _write_settings(path: Path, data: Dict[str, Any]) -> None:
    """JSON を atomic に書き出す。json_file.write_json_atomic に委譲。"""
    _jf.write_json_atomic(path, data)


def _backup_settings(path: Path) -> Optional[Path]:
    """`.bak` を作って返す。json_file.backup_file に委譲。"""
    return _jf.backup_file(path)


# ---------------------------------------------------------------------------
# 結果データ型
# ---------------------------------------------------------------------------


@dataclass
class InstallResult:
    """install の結果。CLI 出力 / test 検証で使う。"""
    settings_path: Path
    backup_path: Optional[Path] = None
    changed: bool = False
    skipped_already_present: bool = False
    legacy_detected: bool = False
    legacy_commands: List[str] = field(default_factory=list)
    dry_run: bool = False
    hook_command: Optional[str] = None
    error: Optional[str] = None


@dataclass
class UninstallResult:
    settings_path: Path
    backup_path: Optional[Path] = None
    changed: bool = False
    removed_count: int = 0
    dry_run: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Stop hook の構造操作
# ---------------------------------------------------------------------------


def _ensure_stop_hooks(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """`data["hooks"]["Stop"]` を確実に list として確保し、参照を返す。"""
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        # hooks が dict でない = 構造異常。ERROR で止めるのは caller 責務、
        # ここでは raise しておく
        raise RuntimeError("hooks field is not an object")
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        raise RuntimeError("hooks.Stop is not a list")
    return stop


def _scan_for_voiceclaude(
    stop_blocks: List[Dict[str, Any]],
    repo_root: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """Stop hook 配列を walk して、voiceClaude / legacy hook の存在を確認。

    戻り値: (has_voiceclaude, legacy_commands)
      has_voiceclaude : 既に voiceClaude 系(legacy 含む)hook があるか
      legacy_commands : legacy `scripts/on_stop.sh` 系の command 文字列リスト
    """
    has_vc = False
    legacy_cmds: List[str] = []
    for block in stop_blocks:
        if not isinstance(block, dict):
            continue
        for h in block.get("hooks", []) or []:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            if not is_voiceclaude_hook(cmd, repo_root):
                continue
            has_vc = True
            if "scripts/on_stop.sh" in cmd or "/on_stop.sh" in cmd:
                legacy_cmds.append(cmd)
    return has_vc, legacy_cmds


def _new_hook_block(command: str) -> Dict[str, Any]:
    """voiceClaude 用の Stop hook block を 1 件作る。"""
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": HOOK_TIMEOUT_DEFAULT,
                "async": HOOK_ASYNC_DEFAULT,
            }
        ],
    }


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def install(
    *,
    scope: str = DEFAULT_SCOPE,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    hook_command: Optional[str] = None,
    dry_run: bool = False,
    yes: bool = False,  # noqa: ARG001 (v0.1 では受理のみ、placeholder)
) -> InstallResult:
    """voiceClaude の Stop hook を scope の settings に追加する。

    戻り値の InstallResult.error が None なら成功(変更なしも成功)、非 None
    なら失敗。caller (CLI) が exit code に変換する。

    ユーザ仕様(R-008):
      - 既存 hook と重複しない(skipped_already_present=True で報告)
      - legacy `scripts/on_stop.sh` 系を検出したら ERROR、自動置換しない
      - 既存設定は壊さず merge
      - .bak は変更前(write 直前)に作成、dry_run なら作らない
      - JSON 破損は ERROR で停止
    """
    if scope not in SCOPES:
        return InstallResult(
            settings_path=Path("."),
            error=f"unknown scope: {scope!r} (must be one of {SCOPES})",
        )

    if repo_root is None:
        # cmd_install.sh から VVREAD_PROJECT_DIR が export されているはず
        rr_env = os.environ.get("VVREAD_PROJECT_DIR")
        if rr_env:
            repo_root = Path(rr_env)
        else:
            # __file__ ベース: scripts/hook_install.py の親
            repo_root = Path(__file__).resolve().parent.parent

    if hook_command is None:
        hook_command = build_hook_command(repo_root)

    settings_path = resolve_settings_path(scope, cwd=cwd, home=home)
    result = InstallResult(
        settings_path=settings_path,
        hook_command=hook_command,
        dry_run=dry_run,
    )

    data, err = _read_settings(settings_path)
    if err:
        result.error = f"{settings_path}: {err}"
        return result

    if data is None:
        # 新規作成
        data = {}

    # Stop hook 配列を確保(構造異常は ERROR)
    try:
        stop_blocks = _ensure_stop_hooks(data)
    except RuntimeError as e:
        result.error = f"{settings_path}: {e}"
        return result

    has_vc, legacy_cmds = _scan_for_voiceclaude(stop_blocks, repo_root)
    if legacy_cmds:
        result.legacy_detected = True
        result.legacy_commands = legacy_cmds
        result.error = (
            f"{settings_path}: legacy vvread hook (on_stop.sh) が登録されています。\n"
            "今回は変更していません。\n"
            f"`vvread uninstall --scope {scope}` を実行してから、改めて install してください。"
        )
        return result

    if has_vc:
        # 既に voiceClaude 系 (modern path) が登録されている → no-op
        result.skipped_already_present = True
        return result

    # 追加(変更前に .bak を取る)
    if not dry_run:
        result.backup_path = _backup_settings(settings_path)

    stop_blocks.append(_new_hook_block(hook_command))

    if dry_run:
        return result

    try:
        _write_settings(settings_path, data)
    except OSError as e:
        result.error = f"{settings_path}: cannot write: {e}"
        return result

    result.changed = True
    return result


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def uninstall(
    *,
    scope: str = DEFAULT_SCOPE,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    dry_run: bool = False,
) -> UninstallResult:
    """voiceClaude 管理の Stop hook のみ削除。他は保持。

    削除ルール:
    - is_voiceclaude_hook(command, repo_root) が True の hook entry を削除
    - hook entry を削除した結果、block.hooks が空になれば block 自体も削除
    - block を削除した結果、Stop が空になれば hooks.Stop を削除
    - hooks が空になれば hooks 自体を削除(他キーは保持)
    - 元ファイル不在 / 該当 hook 無し → 変更なしで成功
    """
    if scope not in SCOPES:
        return UninstallResult(
            settings_path=Path("."),
            error=f"unknown scope: {scope!r} (must be one of {SCOPES})",
        )

    if repo_root is None:
        rr_env = os.environ.get("VVREAD_PROJECT_DIR")
        if rr_env:
            repo_root = Path(rr_env)
        else:
            repo_root = Path(__file__).resolve().parent.parent

    settings_path = resolve_settings_path(scope, cwd=cwd, home=home)
    result = UninstallResult(
        settings_path=settings_path,
        dry_run=dry_run,
    )

    data, err = _read_settings(settings_path)
    if err:
        result.error = f"{settings_path}: {err}"
        return result

    if data is None:
        # ファイル不在 / 空 → 変更なし
        return result

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        # hook field が dict でない → 異常だが他データ保護のため触らない
        return result

    stop_blocks = hooks.get("Stop", [])
    if not isinstance(stop_blocks, list):
        return result

    new_blocks: List[Dict[str, Any]] = []
    removed = 0
    for block in stop_blocks:
        if not isinstance(block, dict):
            new_blocks.append(block)
            continue
        block_hooks = block.get("hooks", [])
        if not isinstance(block_hooks, list):
            new_blocks.append(block)
            continue
        kept: List[Any] = []
        for h in block_hooks:
            if not isinstance(h, dict):
                kept.append(h)
                continue
            cmd = h.get("command", "")
            if is_voiceclaude_hook(cmd, repo_root):
                removed += 1
                continue
            kept.append(h)
        if kept:
            new_block = dict(block)
            new_block["hooks"] = kept
            new_blocks.append(new_block)
        # else: block 全体が voiceClaude のみだったので削除

    result.removed_count = removed
    if removed == 0:
        return result

    # 後続クリーンアップ: 空構造を畳む
    if new_blocks:
        hooks["Stop"] = new_blocks
    else:
        hooks.pop("Stop", None)
    if not hooks:
        data.pop("hooks", None)

    # write(dry_run のときは書かない、changed は False のまま、removed_count
    # は実際の検出数で残す)
    if not dry_run:
        result.backup_path = _backup_settings(settings_path)
        try:
            _write_settings(settings_path, data)
        except OSError as e:
            result.error = f"{settings_path}: cannot write: {e}"
            return result
        result.changed = True

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit_install(result: InstallResult) -> None:
    if result.error:
        print(f"ERROR: {result.error}", file=sys.stderr)
        return
    if result.dry_run:
        if result.skipped_already_present:
            print(
                f"[dry-run] {result.settings_path}: "
                f"vvread hook is already registered, no change needed"
            )
        else:
            print(
                f"[dry-run] {result.settings_path}: would add hook "
                f"command={result.hook_command!r}"
            )
        return
    if result.skipped_already_present:
        print(
            f"{result.settings_path}: "
            f"vvread hook is already registered, no change made"
        )
        print("✅ vvread install: no action needed (already registered)")
        return
    if result.changed:
        bak = f" (backup: {result.backup_path})" if result.backup_path else ""
        print(
            f"{result.settings_path}: registered hook "
            f"command={result.hook_command}{bak}"
        )
        print("✅ vvread install completed successfully")


def _emit_uninstall(result: UninstallResult) -> None:
    if result.error:
        print(f"ERROR: {result.error}", file=sys.stderr)
        return
    if result.dry_run:
        if result.removed_count == 0:
            print(
                f"[dry-run] {result.settings_path}: "
                f"no vvread hook found, nothing to remove"
            )
        else:
            print(
                f"[dry-run] {result.settings_path}: "
                f"would remove {result.removed_count} vvread hook(s)"
            )
        return
    if result.removed_count == 0:
        print(
            f"{result.settings_path}: "
            f"no vvread hook found, no change made"
        )
        return
    bak = f" (backup: {result.backup_path})" if result.backup_path else ""
    print(
        f"{result.settings_path}: removed "
        f"{result.removed_count} vvread hook(s){bak}"
    )



# ---------------------------------------------------------------------------
# 対話 helper（R-102: lib_prompt.py に集約、import で参照）
# ---------------------------------------------------------------------------


def _fetch_speakers_for_install(
    engine_url: str,
    timeout: float = 3.0,
) -> Optional[List[Dict[str, Any]]]:
    """install 用の簡易 /speakers 取得。

    失敗 / malformed → None（install は止めない）。
    """
    url = engine_url.rstrip("/") + "/speakers"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", errors="replace")
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def _ensure_vvread_settings_file(
    cwd: Path,
    *,
    dry_run: bool = False,
    out: Optional[IO[str]] = None,
) -> Path:
    """cwd/vvread.settings.json が存在しなければ空の {} で作成する。"""
    path = cwd / "vvread.settings.json"
    if dry_run:
        if not path.exists() and out is not None:
            out.write(f"DRY-RUN: would create {path}\n")
    elif not path.exists():
        _jf.write_json_atomic(path, {})
    return path


def _write_vvread_settings_speaker(
    cwd: Path,
    speaker_id: int,
) -> None:
    """cwd/vvread.settings.json に voicevox.speaker を書き込む。"""
    settings_path = cwd / "vvread.settings.json"
    data, err = _jf.load_json_file(settings_path)
    if err:
        return  # 破損ファイルには触らない
    if data is None:
        data = {}
    voicevox = data.setdefault("voicevox", {})
    if not isinstance(voicevox, dict):
        data["voicevox"] = {}
        voicevox = data["voicevox"]
    voicevox["speaker"] = speaker_id
    _jf.backup_file(settings_path)
    _jf.write_json_atomic(settings_path, data)


# ---------------------------------------------------------------------------
# interactive_install helpers
# ---------------------------------------------------------------------------


def _print_hook_status_table(
    cwd: Path,
    home: Path,
    repo_root: Path,
    out,
) -> Dict[str, str]:
    """全 scope の hook 登録状況を表示し {scope: status} を返す。
    status: "registered" | "legacy" | "none"
    表示順: user → project → project-local
    """
    scope_defs = [
        ("user",          resolve_settings_path("user",          cwd=cwd, home=home)),
        ("project",       resolve_settings_path("project",       cwd=cwd, home=home)),
        ("project-local", resolve_settings_path("project-local", cwd=cwd, home=home)),
    ]
    out.write("Claude Code hook 状況:\n")
    statuses: Dict[str, str] = {}
    for label, path in scope_defs:
        data, err = _read_settings(path)
        if err or data is None:
            status = "none"
            display = "-  未登録"
        else:
            try:
                stop_blocks = _ensure_stop_hooks(data)
                has_vc, legacy = _scan_for_voiceclaude(stop_blocks, repo_root)
            except RuntimeError:
                has_vc, legacy = False, []
            if legacy:
                status = "legacy"
                display = "⚠  legacy hook あり (要移行)"
            elif has_vc:
                status = "registered"
                display = "✓  vvread hook 登録済"
            else:
                status = "none"
                display = "-  未登録"
        statuses[label] = status
        out.write(f"  {label:<16}{display}\n")
    out.write("\n")
    return statuses


# ---------------------------------------------------------------------------
# interactive_install
# ---------------------------------------------------------------------------


def interactive_install(
    *,
    scope: Optional[str] = None,
    dry_run: bool = False,
    cwd: Optional[Path] = None,
    home: Optional[Path] = None,
    repo_root: Optional[Path] = None,
    in_stream: Any = None,
    out_stream: Any = None,
    err_stream: Any = None,
) -> int:
    """全 scope hook 状況表示 → 登録 → settings 確認の2段階対話 install。

    戻り値は exit code (0/1)。
    """
    if cwd is None:
        cwd = Path.cwd()
    if home is None:
        home = Path.home()
    out = out_stream or sys.stdout
    err = err_stream or sys.stderr

    # 1. TTY チェック
    if not _is_tty(in_stream):
        err.write(
            "ERROR: interactive install requires a TTY.\n"
            "Run with --yes for non-interactive install.\n"
        )
        return 1

    # repo_root 解決（Step 1-b / 1-c で共用）
    if repo_root is None:
        rr_env = os.environ.get("VVREAD_PROJECT_DIR")
        repo_root = Path(rr_env) if rr_env else Path(__file__).resolve().parent.parent

    # Step 1-a. 全 scope の hook 状況テーブルを表示
    statuses = _print_hook_status_table(cwd, home, repo_root, out)

    # Step 1-b. hook 登録判定
    # いずれかに modern hook があれば有効とみなし追加登録しない
    registered_scope: Optional[str] = None
    for _s in ("user", "project", "project-local"):
        if statuses.get(_s) == "registered":
            registered_scope = _s
            break

    if registered_scope is not None:
        if registered_scope == "user":
            out.write("Claude hook は既に有効です。\n\n")
        elif registered_scope == "project":
            out.write(
                "Claude hook は有効です。\n"
                "注意: project スコープは共有設定の可能性があります。"
                "意図的に登録した場合は問題ありません。\n\n"
            )
        else:  # project-local
            out.write("このプロジェクトでは Claude hook は有効です。\n\n")

        # 複数スコープに登録されている場合は注意を表示
        registered_scopes = [
            s for s in ("user", "project", "project-local")
            if statuses.get(s) == "registered"
        ]
        if len(registered_scopes) >= 2:
            out.write(
                "  ※ 注意: 複数のスコープ（"
                + "・".join(registered_scopes)
                + "）に hook が登録されています。\n"
                "          もし二重で再生される場合には、いずれか一方を削除してください\n"
                "    コマンド：\n"
                "          vvread uninstall --scope user\n"
                "          vvread uninstall --scope project\n\n"
            )
    else:
        # legacy のない scope のみ選択肢にする（推奨順: user → project-local → project）
        available = [
            s for s in ("user", "project-local", "project")
            if statuses.get(s) != "legacy"
        ]

        if not available:
            # 全 scope が legacy
            out.write(
                "今回は変更していません。\n"
                "全スコープに legacy hook があります。先に `vvread uninstall` を実行してください。\n\n"
            )
        else:
            # Step 1-c. scope 選択（デフォルト: user を先頭）
            _label_map = {
                "user":          f"user           →  {home}/.claude/settings.json",
                "project-local": f"project-local  →  {cwd}/.claude/settings.local.json",
                "project":       f"project        →  {cwd}/.claude/settings.json",
            }
            scope_labels = [_label_map[s] for s in available]

            # Git 配下チェック（U-105）: Git 外では user scope を先頭に固定
            if not _in_git_repo(cwd):
                out.write(
                    "\n⚠  Git リポジトリ外で実行しています。\n"
                    "   project-local / project scope は .claude/ ディレクトリを必要とします。\n"
                    "   user scope を推奨します。\n\n"
                )
                user_label = _label_map.get("user")
                if user_label and user_label in scope_labels and scope_labels[0] != user_label:
                    scope_labels = [user_label] + [l for l in scope_labels if l != user_label]

            chosen_label = _prompt_choice(
                "登録先を選択してください:",
                scope_labels,
                scope_labels[0],
                in_stream=in_stream,
                out_stream=out,
            )
            chosen_scope = available[scope_labels.index(chosen_label)]

            if chosen_scope == "project":
                out.write(
                    "注意: project スコープは git 管理下のファイルです。\n"
                    "チームで共有する場合は意図的に選択してください。\n\n"
                )

            # .claude/ 存在確認（project / project-local のみ）
            if chosen_scope in ("project", "project-local"):
                claude_dir = cwd / ".claude"
                if not claude_dir.exists():
                    if not _prompt_yn(
                        f".claude/ が存在しません。{claude_dir} を作成しますか？",
                        default=True,
                        in_stream=in_stream,
                        out_stream=out,
                    ):
                        out.write("インストールをキャンセルしました。\n")
                        return 0

            hook_command = build_hook_command(repo_root)
            out.write(f"\nHook command:\n  {hook_command}\n\n")

            result = install(
                scope=chosen_scope,
                cwd=cwd,
                home=home,
                repo_root=repo_root,
                hook_command=hook_command,
                dry_run=dry_run,
                yes=True,
            )
            _emit_install(result)
            if result.error:
                return 1

    # Step 2. vvread.settings.json 確認・作成
    vvread_settings_path = cwd / "vvread.settings.json"
    out.write("vvread project settings:\n")
    if vvread_settings_path.exists():
        out.write(
            "  ./vvread.settings.json  ✓ 作成済\n"
            "  `vvread config` でプロジェクト設定を変更できます。\n\n"
        )
        return 0

    out.write("  ./vvread.settings.json  - 未作成\n\n")

    if dry_run:
        out.write("DRY-RUN: would create ./vvread.settings.json\n")
        return 0

    out.write(
        "作成すると、このプロジェクト専用にスピーカーの選択、音量の調整などが設定できます。\n"
        "作成しない場合でも vvread は動作しますが、プロジェクト専用の設定を保存できません。\n"
    )
    if not _prompt_yn(
        "このプロジェクト用の vvread.settings.json を作成しますか？",
        default=True,
        in_stream=in_stream,
        out_stream=out,
    ):
        out.write("プロジェクト専用の設定を保存できません。\n")
        return 0

    # Speaker 選択
    import settings as _stg  # noqa: PLC0415 (遅延 import: 循環防止)
    loaded = _stg.load(cwd=cwd)
    rv = loaded.get("voicevox.engineUrl")
    engine_url = rv.value if rv is not None else "http://127.0.0.1:50021"

    speakers_data = _fetch_speakers_for_install(engine_url)
    selected_speaker: Optional[int] = None

    if speakers_data is None:
        out.write(
            "Warning: VOICEVOXと連携されていません。起動状況または設定を確認してください。\n"
            "参考: vvread doctor\n"
            "Speaker 選択をスキップします。後で `vvread config` で設定できます。\n\n"
        )
    else:
        speaker_options: List[str] = []
        speaker_ids: List[int] = []
        for sp in speakers_data:
            if not isinstance(sp, dict):
                continue
            sp_name = sp.get("name", "")
            for st in sp.get("styles", []) or []:
                if not isinstance(st, dict):
                    continue
                st_id = st.get("id")
                st_name = st.get("name", "")
                if not (isinstance(st_id, int) and isinstance(st_name, str)):
                    continue
                if st_name != "ノーマル":
                    continue
                speaker_options.append(sp_name)
                speaker_ids.append(st_id)

        if speaker_options:
            rv_speaker = loaded.get("voicevox.speaker")
            current_id = rv_speaker.value if rv_speaker is not None else 3
            if current_id not in speaker_ids:
                current_id = speaker_ids[0]
            selected_speaker = _prompt_speaker_id(
                "Speaker を選択してください:",
                speaker_options,
                speaker_ids,
                current_id,
                in_stream=in_stream,
                out_stream=out,
            )
        else:
            out.write("有効な Speaker 情報が取得できませんでした。スキップします。\n\n")

    _ensure_vvread_settings_file(cwd, dry_run=dry_run, out=out)
    if selected_speaker is not None and not dry_run:
        _write_vvread_settings_speaker(cwd, selected_speaker)
        out.write(f"Speaker ID {selected_speaker} を vvread.settings.json に保存しました。\n")

    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    scope, warn = _resolve_scope_alias(args.scope)
    if warn:
        print(warn, file=sys.stderr)

    if not args.yes and not args.dry_run:
        return interactive_install(scope=scope, dry_run=False)

    result = install(
        scope=scope,
        dry_run=args.dry_run,
        yes=args.yes,
    )
    _emit_install(result)
    _ensure_vvread_settings_file(Path.cwd(), dry_run=args.dry_run, out=sys.stdout)
    return 1 if result.error else 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    scope, warn = _resolve_scope_alias(args.scope)
    if warn:
        print(warn, file=sys.stderr)
    result = uninstall(
        scope=scope,
        dry_run=args.dry_run,
    )
    _emit_uninstall(result)
    return 1 if result.error else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "vvread install / uninstall: "
            "Claude Code hook を現在のプロジェクトに登録・解除する。"
            "hook 登録のみが目的。VOICEVOX Engine の起動・依存確認は主目的ではない。"
            "初回は通常 vvread setup を使い、別プロジェクトへの追加登録では install を使う。"
            "setup の代替ではない。"
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _all_scope_choices = list(SCOPES) + list(_DEPRECATED_SCOPE_ALIASES.keys())

    p_install = sub.add_parser(
        "install",
        help=(
            "Claude Code hook を登録する。"
            "初回は vvread setup 推奨。別プロジェクトへの追加登録に使う。"
        ),
    )
    p_install.add_argument(
        "--scope", choices=_all_scope_choices, default=DEFAULT_SCOPE,
        help=f"target settings scope (default: {DEFAULT_SCOPE})",
        metavar="SCOPE",
    )
    p_install.add_argument(
        "--dry-run", action="store_true",
        help="do not write any file, just report what would change",
    )
    p_install.add_argument(
        "--yes", action="store_true",
        help="non-interactive mode: skip prompts and use defaults",
    )
    p_install.set_defaults(func=_cmd_install)

    p_uninstall = sub.add_parser(
        "uninstall", help="remove vvread-managed Stop hook only",
    )
    p_uninstall.add_argument(
        "--scope", choices=_all_scope_choices, default=DEFAULT_SCOPE,
        help=f"target settings scope (default: {DEFAULT_SCOPE})",
        metavar="SCOPE",
    )
    p_uninstall.add_argument(
        "--dry-run", action="store_true",
        help="do not write any file, just report what would change",
    )
    p_uninstall.set_defaults(func=_cmd_uninstall)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # UX 優先で対話キャンセルを正常終了扱い (exit 0)。
        # SIGINT の慣習 (exit 130) とは異なる意図的な設計判断。
        sys.stderr.write("\nキャンセルしました。\n")
        sys.exit(0)
