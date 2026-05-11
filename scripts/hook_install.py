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
from typing import Any, Dict, List, Optional, Tuple

# scripts/ を sys.path に追加（他 module 直 import 用）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import json_file as _jf  # noqa: E402
from lib_prompt import (  # noqa: E402 (R-102)
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
# scope path 解決
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
# vvread hook 判定(doctor から共通化、drift 防止)
# ---------------------------------------------------------------------------


def is_voiceclaude_hook(
    command: str,
    repo_root: Optional[Path] = None,
) -> bool:
    """command 文字列が voiceClaude の Stop hook を指しているか判定。

    判定ルール(doctor.py から移管した正本):
    - "vvread on-stop" を含む(PATH 経由 or 絶対パス、空白 or タブ区切り)
    - "/bin/vvread" を含み、引数 "on-stop" を含む(クォート有り無しの両対応)
    - "scripts/on_stop.sh" を含む(legacy)
    - repo_root を指定された場合、そこ配下のスクリプトパスでも一致
    """
    if not isinstance(command, str):
        return False
    if "vvread on-stop" in command or "vvread\ton-stop" in command:
        return True
    if "/bin/vvread" in command and "on-stop" in command:
        return True
    if "scripts/on_stop.sh" in command or "/on_stop.sh" in command:
        return True
    if repo_root is not None:
        rr = str(repo_root)
        if rr in command and ("on-stop" in command or "on_stop.sh" in command):
            return True
    return False


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
            f"{settings_path}: legacy vvread hook detected "
            f"({len(legacy_cmds)} entries). Run `vvread uninstall "
            f"--scope {scope}` first to remove the legacy hook, "
            f"then run install again."
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
    """TTY 確認 → scope 選択 → hook 確認 → speaker 選択 → install 実行。

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

    # 2. scope 選択
    scope_choices = list(SCOPES)
    scope_labels = [
        f"project-local  →  {cwd}/.claude/settings.local.json",
        f"project        →  {cwd}/.claude/settings.json",
        f"user           →  {home}/.claude/settings.json",
    ]
    default_scope_label = scope_labels[0]
    chosen_label = _prompt_choice(
        "登録先を選択してください:",
        scope_labels,
        default_scope_label,
        in_stream=in_stream,
        out_stream=out,
    )
    chosen_scope = scope_choices[scope_labels.index(chosen_label)]

    # 2-b. 選択した scope に既に hook が登録済みか確認 → 登録済みなら即終了
    if repo_root is None:
        rr_env = os.environ.get("VVREAD_PROJECT_DIR")
        if rr_env:
            repo_root = Path(rr_env)
        else:
            repo_root = Path(__file__).resolve().parent.parent

    _check_path = resolve_settings_path(chosen_scope, cwd=cwd, home=home)
    _check_data, _check_err = _read_settings(_check_path)
    if not _check_err and _check_data is not None:
        try:
            _stop_blocks = _ensure_stop_hooks(_check_data)
            _has_vc, _ = _scan_for_voiceclaude(_stop_blocks, repo_root)
        except RuntimeError:
            _has_vc = False
        if _has_vc:
            out.write(
                "vvreadはすでに設定済です。\n"
                "`vvread uninstall`: この場所から設定を消します\n"
                "`vvread config`: 設定を変更します\n"
            )
            return 0

    # 3. .claude/ 存在確認
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

    # 4. hook command を表示
    hook_command = build_hook_command(repo_root)
    out.write(f"\nHook command:\n  {hook_command}\n\n")

    # 5. VOICEVOX speaker 選択
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
                speaker_options.append(f"{st_id}: {sp_name}")
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

    # 6. install 実行
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

    # speaker を vvread.settings.json に書き込む
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
        description="vvread install / uninstall"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    _all_scope_choices = list(SCOPES) + list(_DEPRECATED_SCOPE_ALIASES.keys())

    p_install = sub.add_parser("install", help="register Claude Code Stop hook")
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
    sys.exit(main())
