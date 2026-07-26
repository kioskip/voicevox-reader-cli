#!/usr/bin/env python3
"""scripts/launch_agent.py - vvread menubar LaunchAgent 登録/解除 (B-156)

macOS の LaunchAgent (`~/Library/LaunchAgents/com.vvread.menubar.plist`) を
生成・登録(`launchctl bootstrap`)・解除(`launchctl bootout`)するモジュール。
`vvread setup` の menubar step (setup.py の `step_menubar`)と
`vvread uninstall --with-menubar` (hook_install.py の `_cmd_uninstall`)の両方
から呼ばれる。B-151(menubar UI 本体)から分離した後続タスク。

設計判断:
  - plist 生成は plistlib のみを使う(文字列テンプレートは使わない)。
    `plistlib.dumps()` は既定で key を sort するため、同一内容なら常に同一
    bytes になり、変更検出(byte 比較)を単純にできる
  - `KeepAlive` は設定しない。クラッシュ時に再起動ループさせないため
    (menubar UI がクラッシュを繰り返すとログイン直後に CPU を食い潰す懸念)
  - rumps 可用性チェックは doctor._resolve_rumps_check() を import して再利用
    する(scripts/cmd/menubar.sh の Python 解決順と完全一致させ、drift を防ぐ
    ため)。doctor.py は本モジュールを import しないため循環しない
    (hook_install.py → launch_agent.py は関数内の遅延 import にして安全側に
    倒す。hook_install.py 側のコメント参照)
  - launchctl は全て `runner` 経由で呼ぶ(DI)。自動テストで実 launchctl を
    呼ばないための必須要件
  - register() は「plist の内容が既存と同一なら no-op」を保証する
    (バイト比較)。bootstrap 失敗時は書き込み前の内容へロールバックする
    (元々存在しなかった場合は削除する)
  - unregister() は plist 不在 / 既に unload 済みでも成功扱い(冪等)。bootout
    の失敗は無視して plist 削除へ進む(「未ロード」時は非 0 exit になるため、
    それをエラー扱いにすると通常の再実行が失敗してしまう)
  - dry_run=True のときは実際のファイル書き込み・runner 呼び出しを一切行わ
    ない(ログディレクトリ作成も含む)
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

import doctor as _doctor  # noqa: E402 (rumps 判定ロジックの再利用、drift 防止)

RunnerType = Callable[..., Any]

LABEL = "com.vvread.menubar"
_LOG_FILE_NAME = "menubar.launchagent.log"

# doctor.py の rumps install hint と表現を一致させる(drift 防止)。
_RUMPS_INSTALL_HINT = _doctor._RUMPS_INSTALL_HINT


def _default_runner(cmd: List[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, **kwargs)


def plist_path(home: Path) -> Path:
    """plist の配置先: ``<home>/Library/LaunchAgents/com.vvread.menubar.plist``。"""
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(
    repo_root: Path,
    log_dir: Path,
    menubar_python: Optional[str] = None,
) -> bytes:
    """LaunchAgent plist を生成する(副作用なし、純粋関数)。

    - Label: 固定 "com.vvread.menubar"
    - ProgramArguments: [<repo_root>/bin/vvread, "menubar"]
    - RunAtLoad: True
    - KeepAlive: 設定しない(クラッシュ再起動ループ防止)
    - StandardOutPath / StandardErrorPath: <log_dir>/menubar.launchagent.log
    - EnvironmentVariables: menubar_python が指定されていれば
      {"VVREAD_MENUBAR_PYTHON": menubar_python} を含める(未指定ならキー自体
      を持たない)。rumps が VVREAD_MENUBAR_PYTHON 経由の python でのみ利用
      可能な環境で、setup 実行時のシェル環境(export された変数)が
      ログイン時の LaunchAgent 起動には引き継がれないため、plist へ明示的に
      記録して menubar.sh の Python 解決を安定させる(B-156 追補)。
    """
    log_path = str(log_dir / _LOG_FILE_NAME)
    plist_dict: dict = {
        "Label": LABEL,
        "ProgramArguments": [str(repo_root / "bin" / "vvread"), "menubar"],
        "RunAtLoad": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if menubar_python:
        plist_dict["EnvironmentVariables"] = {"VVREAD_MENUBAR_PYTHON": menubar_python}
    return plistlib.dumps(plist_dict)


def _rumps_available(repo_root: Path) -> bool:
    """scripts/cmd/menubar.sh / doctor.py と同じ判定順で rumps 可用性を確認する。"""
    return _doctor._resolve_rumps_check(repo_root).found


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp file + os.replace() で atomic に書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _rollback(path: Path, original_content: Optional[bytes]) -> None:
    """bootstrap 失敗時、書き込み前の内容へ戻す(無ければ削除する)。"""
    if original_content is not None:
        try:
            _atomic_write_bytes(path, original_content)
        except OSError:
            pass
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _rollback_and_recover(
    path: Path,
    existing_content: Optional[bytes],
    uid: int,
    r: RunnerType,
) -> str:
    """bootstrap 失敗時のロールバック + 直前に bootout した旧定義の再ロード。

    内容変更(update)の失敗時は、新しい plist を書く前に古い定義を bootout
    済みである。ファイルを元の内容へ戻すだけでは launchd 上は「何もロードさ
    れていない」状態のまま取り残されるため、ロールバック後に同じ内容で
    bootstrap を再試行し、極力ユーザーの既存登録を壊さないようにする
    (ベストエフォート、これ自体の失敗は致命的ではない)。

    新規登録(install)の失敗時は、そもそも直前に bootout していない
    (existing_content is None)ので再ロードは不要。

    戻り値: 再ロードできなかった場合の案内文(空文字なら特に案内不要)。
    """
    _rollback(path, existing_content)

    if existing_content is None:
        return ""

    try:
        recover_proc = r(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if recover_proc.returncode == 0:
            return ""
    except (subprocess.TimeoutExpired, OSError):
        pass

    return (
        f" (previous menubar LaunchAgent could not be reloaded; "
        f"run manually: launchctl bootstrap gui/{uid} {shlex.quote(str(path))})"
    )


# ---------------------------------------------------------------------------
# 結果データ型
# ---------------------------------------------------------------------------


@dataclass
class RegisterResult:
    """register() の結果。呼出側(setup.py の step_menubar)が status に変換する。"""
    ok: bool
    rumps_available: bool
    changed: bool
    # "noop" | "install" | "update" | "rumps_unavailable" | "error"
    action: str
    plist_path: Path
    message: str
    dry_run: bool = False
    error: Optional[str] = None


@dataclass
class UnregisterResult:
    """unregister() の結果。"""
    ok: bool
    changed: bool
    # "noop" | "removed"
    action: str
    plist_path: Path
    message: str
    dry_run: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------------


def register(
    *,
    repo_root: Path,
    log_dir: Path,
    home: Path,
    uid: int,
    runner: Optional[RunnerType] = None,
    dry_run: bool = False,
) -> RegisterResult:
    """menubar 用 LaunchAgent を `gui/<uid>` domain に登録する。

    手順:
      1. rumps 可用性チェック(不可なら登録せず rumps_unavailable を返す)
      2. plist が既存と同一内容なら no-op
      3. 異なる場合(初回 or 内容変更): 既存があれば bootout → atomic write
         → bootstrap。bootstrap 失敗時はロールバック
      4. dry_run=True はファイル書き込み・runner 呼び出しを一切行わない
    """
    r = runner or _default_runner
    path = plist_path(home)

    if not _rumps_available(repo_root):
        return RegisterResult(
            ok=False, rumps_available=False, changed=False,
            action="rumps_unavailable", plist_path=path, dry_run=dry_run,
            message="rumps not available; menubar auto-start not registered",
        )

    new_content = build_plist(
        repo_root, log_dir, menubar_python=os.environ.get("VVREAD_MENUBAR_PYTHON"),
    )
    existing_content: Optional[bytes] = None
    if path.exists():
        try:
            existing_content = path.read_bytes()
        except OSError:
            existing_content = None

    if existing_content is not None and existing_content == new_content:
        return RegisterResult(
            ok=True, rumps_available=True, changed=False,
            action="noop", plist_path=path, dry_run=dry_run,
            message=f"menubar LaunchAgent already up to date at {path}",
        )

    action = "update" if existing_content is not None else "install"

    if dry_run:
        return RegisterResult(
            ok=True, rumps_available=True, changed=True,
            action=action, plist_path=path, dry_run=True,
            message=f"[dry-run] would {action} menubar LaunchAgent at {path}",
        )

    # ログディレクトリを 0700 で確保(存在しなければ)。この時点ではまだ
    # launchd 側は一切変更していない(bootout 前)ため、失敗時はそのまま
    # エラーを返せばよい(ロールバック対象が無い)。
    try:
        if not log_dir.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(log_dir, 0o700)
    except OSError as e:
        return RegisterResult(
            ok=False, rumps_available=True, changed=False,
            action="error", plist_path=path, dry_run=False,
            message=f"failed to write LaunchAgent files: {e}",
            error=str(e),
        )

    if existing_content is not None:
        # 置換前に古い定義を bootout する。失敗しても継続する(未ロードの場合
        # は非 0 exit になるだけで、実害はない)。
        try:
            r(
                ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass

    try:
        _atomic_write_bytes(path, new_content)
    except OSError as e:
        # 既存があった場合、直前に bootout 済み(launchd 上は未ロード)なのに
        # plist 書き込みが失敗すると中途半端な状態になる。ベストエフォートで
        # 旧定義の再ロードを試みる(_rollback_and_recover を再利用。書き込み
        # 自体が壊れている状況でのファイル復元は失敗しうるが、再ロード試行
        # 自体はファイル書き込みに依存しないため意味がある)。
        recovery_note = _rollback_and_recover(path, existing_content, uid, r)
        return RegisterResult(
            ok=False, rumps_available=True, changed=False,
            action="error", plist_path=path, dry_run=False,
            message=f"failed to write LaunchAgent files: {e}{recovery_note}",
            error=f"{e}{recovery_note}",
        )

    try:
        proc = r(
            ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        recovery_note = _rollback_and_recover(path, existing_content, uid, r)
        return RegisterResult(
            ok=False, rumps_available=True, changed=False,
            action="error", plist_path=path, dry_run=False,
            message=f"launchctl bootstrap failed: {e}{recovery_note}",
            error=f"{e}{recovery_note}",
        )

    if proc.returncode != 0:
        recovery_note = _rollback_and_recover(path, existing_content, uid, r)
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
        error_detail = (detail[-300:] if detail else f"exit {proc.returncode}") + recovery_note
        return RegisterResult(
            ok=False, rumps_available=True, changed=False,
            action="error", plist_path=path, dry_run=False,
            message=f"launchctl bootstrap exit {proc.returncode}{recovery_note}",
            error=error_detail,
        )

    return RegisterResult(
        ok=True, rumps_available=True, changed=True,
        action=action, plist_path=path, dry_run=False,
        message=f"{action}ed menubar LaunchAgent at {path}",
    )


def unregister(
    *,
    home: Path,
    uid: int,
    runner: Optional[RunnerType] = None,
    dry_run: bool = False,
) -> UnregisterResult:
    """menubar 用 LaunchAgent を解除する。

    plist が存在しない、または既に unload 済みの場合も成功として扱う(冪等)。
    """
    r = runner or _default_runner
    path = plist_path(home)

    if not path.exists():
        return UnregisterResult(
            ok=True, changed=False, action="noop", plist_path=path,
            dry_run=dry_run, message="menubar LaunchAgent not registered",
        )

    if dry_run:
        return UnregisterResult(
            ok=True, changed=True, action="removed", plist_path=path,
            dry_run=True,
            message=f"[dry-run] would remove menubar LaunchAgent at {path}",
        )

    try:
        r(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        # 既に unload 済み / launchctl 不在等 → 無視して plist 削除へ進む
        pass

    try:
        path.unlink()
    except FileNotFoundError:
        pass

    return UnregisterResult(
        ok=True, changed=True, action="removed", plist_path=path,
        dry_run=False, message=f"removed menubar LaunchAgent at {path}",
    )
