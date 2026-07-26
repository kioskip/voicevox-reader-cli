#!/usr/bin/env python3
"""scripts/setup.py - vvread setup (R-011)

`vvread setup` は対話モードで以下 3 step を順次実行する setup orchestration:

  1. engine: VOICEVOX Engine への疎通確認 + engineUrl の settings 書込み
  2. e2k:    英→カナ変換ライブラリの任意インストール
  3. hook:   Claude Code Stop hook の登録(R-008 hook_install を再利用)

設計判断:
  - VOICEVOX Engine の起動・管理はプロジェクト外の責務(v0.1 方針)。
    setup は「起動済みの Engine に接続できるか」のみを確認する
  - 対話 prompt を default、`--yes` で完全非対話化
  - tty が無く `--yes` も無い場合は ERROR(意図せず install されるのを防ぐ)
  - settings 書込先は project (`<cwd>/vvread.settings.json`) 一択
  - e2k インストール失敗は WARN で続行(optional 依存、止めない)
  - settings 書込みは hook_install._write_settings を import で再利用
    (atomic write + drift 防止)
  - --skip-engine / --skip-e2k / --skip-hook で部分実行(power user 用)
  - rumps (B-151 menubar UI) はコア依存 (pyproject `sys_platform=='darwin'`
    marker) なので `uv sync` だけで導入される。setup は専用の install step
    を持たず、冒頭の状態サマリに導入状況を表示するだけ(macOS のみ)

Exit code:
  0 = 全 step OK / WARN / SKIPPED
  1 = いずれかの step ERROR
  2 = 使い方エラー(argparse default、不正オプション)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent

import dependencies as _deps  # noqa: E402
import hook_install as _hi  # noqa: E402
import settings as _stg  # noqa: E402
from lib_git import in_git_repo as _in_git_repo  # noqa: E402 (U-115)
from lib_http import http_get as _http_get_impl  # noqa: E402 (R-101)
from lib_prompt import (  # noqa: E402 (U-113 / U-114)
    prompt_choice as _prompt_choice,
    prompt_yn as _prompt_yn,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

DEFAULT_ENGINE_URL = "http://127.0.0.1:50021"
HTTP_TIMEOUT_SEC = 3.0


STATUS_OK = "OK"
STATUS_INFO = "INFO"
STATUS_WARN = "WARN"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED = "SKIPPED"


@dataclass
class StepResult:
    step: str            # "engine" / "e2k" / "hook"
    status: str          # OK / WARN / ERROR / SKIPPED
    message: str
    detail: Optional[str] = None
    hint: Optional[str] = None


@dataclass
class SetupContext:
    """setup 実行時のオプション + 環境を保持する DI コンテナ。"""
    yes: bool = False
    dry_run: bool = False
    engine_url: Optional[str] = None
    skip_engine: bool = False
    skip_e2k: bool = False
    skip_hook: bool = False
    skip_mcp: bool = False
    with_mcp: bool = False
    with_receiver: bool = False
    skip_menubar: bool = False
    with_menubar: bool = False
    install_e2k: Optional[bool] = None  # None = 対話で聞く / True = 強制インストール / False = skip
    hook_scope: str = _hi.DEFAULT_SCOPE
    cwd: Path = field(default_factory=Path.cwd)
    home: Path = field(default_factory=Path.home)
    repo_root: Optional[Path] = None
    json_mode: bool = False  # True のとき status サマリを stdout に出さない
    # I/O DI: テストで stdin / stdout / stderr を差し替える
    in_stream: Any = None
    out_stream: Any = None
    err_stream: Any = None
    # subprocess factory(テストで monkeypatch しやすいよう関数経由)
    runner: Any = None  # callable: (cmd, **kwargs) -> CompletedProcess


def _default_runner(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# 対話 helper
# ---------------------------------------------------------------------------


def _prompt(ctx: SetupContext, question: str, default: str) -> str:
    """対話 prompt。ctx.yes なら default を即返す。tty が無く --yes も無い
    場合は呼出側が事前にチェックしている前提(_require_tty_or_yes 経由)。
    """
    if ctx.yes:
        return default
    out = ctx.out_stream or sys.stdout
    in_ = ctx.in_stream or sys.stdin
    out.write(f"{question} [{default}]: ")
    out.flush()
    line = in_.readline()
    if not line:
        return default
    line = line.strip()
    return line if line else default


def _prompt_yes_no(ctx: SetupContext, question: str, default: bool = False) -> bool:
    """ctx.yes なら即 default、それ以外は lib_prompt.prompt_yn に委譲 (U-114)。

    注意: lib_prompt.prompt_yn の既定は default=True だが、本ラッパーの既定は
    default=False。default は必ず明示的に転送する(opt-out への反転を防ぐ)。
    """
    if ctx.yes:
        return default
    return _prompt_yn(
        question,
        default=default,
        in_stream=ctx.in_stream,
        out_stream=ctx.out_stream,
    )


def _require_tty_or_yes(ctx: SetupContext) -> Optional[StepResult]:
    """対話モードのとき stdin が tty でなければ ERROR を返す。"""
    if ctx.yes:
        return None
    in_stream = ctx.in_stream or sys.stdin
    isatty = getattr(in_stream, "isatty", lambda: False)
    try:
        is_tty = bool(isatty())
    except Exception:  # noqa: BLE001
        is_tty = False
    if not is_tty:
        return StepResult(
            step="setup",
            status=STATUS_ERROR,
            message="non-interactive stdin detected without --yes",
            hint="Run with --yes to accept defaults, or run from an "
                 "interactive terminal",
        )
    return None


# ---------------------------------------------------------------------------
# HTTP helper (R-101: lib_http.http_get を使用)
# ---------------------------------------------------------------------------


def _normalize_engine_base(url: str) -> str:
    u = url.rstrip("/")
    if u.endswith("/version"):
        u = u[: -len("/version")]
    return u


def _engine_reachable(url: str) -> Optional[Dict[str, Any]]:
    """engine が応答するなら {version, speakers_count} 程度の情報を返す。
    不通なら None。"""
    base = _normalize_engine_base(url)
    v, _ = _http_get_impl(f"{base}/version", HTTP_TIMEOUT_SEC)
    if v is None:
        return None
    info: Dict[str, Any] = {"version": v.strip().strip('"')}
    sp_text, _ = _http_get_impl(f"{base}/speakers", HTTP_TIMEOUT_SEC)
    if sp_text:
        try:
            speakers = json.loads(sp_text)
            ids = []
            if isinstance(speakers, list):
                for sp in speakers:
                    if isinstance(sp, dict):
                        for st in sp.get("styles", []) or []:
                            if isinstance(st, dict) and isinstance(st.get("id"), int):
                                ids.append(st["id"])
            info["speakers_count"] = len(ids)
        except json.JSONDecodeError:
            pass
    return info


# ---------------------------------------------------------------------------
# settings.json 書込み(project scope, JSON)
# ---------------------------------------------------------------------------


def _project_settings_path(cwd: Path) -> Path:
    return cwd / "vvread.settings.json"


def _update_engine_setting(
    cwd: Path,
    new_url: str,
    *,
    dry_run: bool = False,
) -> Optional[Path]:
    """`<cwd>/vvread.settings.json` の `voicevox.engines` を [new_url] に
    更新する。既存ファイルが無ければ新規作成。default 値と同じなら no-op。
    保存時は canonicalize_settings_dict() で engines に統一する。

    戻り値: 書き込み実施したら settings_path、no-op (default 一致 or dry_run)
    なら None。
    """
    normalized_url = new_url.rstrip("/")
    if normalized_url == DEFAULT_ENGINE_URL.rstrip("/"):
        # default のままなら settings.json に書き込まなくても解決される(R-025)
        return None

    path = _project_settings_path(cwd)
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = parsed
        except (OSError, json.JSONDecodeError):
            # 既存ファイルが壊れている場合は触らない(setup の責務外、ユーザに
            # 修正させる)。caller が ERROR で止める判断をする想定で None
            return None

    voicevox = data.setdefault("voicevox", {})
    if not isinstance(voicevox, dict):
        return None

    # 既に同じ engines=[normalized_url] で engineUrl なし → no-op
    current_engines = voicevox.get("engines")
    if (
        current_engines == [normalized_url]
        and "engineUrl" not in voicevox
    ):
        return None

    voicevox["engines"] = [normalized_url]
    voicevox.pop("engineUrl", None)

    try:
        data = _stg.canonicalize_settings_dict(data)
    except ValueError:
        return None

    if dry_run:
        return path  # 書込予定パスを返す(実書込みはしない)

    # hook_install._write_settings を import で再利用(atomic write + drift 防止)
    _hi._write_settings(path, data)
    return path


# ---------------------------------------------------------------------------
# step: engine
# ---------------------------------------------------------------------------


def step_engine(ctx: SetupContext) -> StepResult:
    if ctx.skip_engine:
        return StepResult(
            step="engine", status=STATUS_SKIPPED,
            message="engine step skipped (--skip-engine)",
        )

    url = ctx.engine_url
    if url is None:
        url = _prompt(ctx, "Engine URL", DEFAULT_ENGINE_URL)
    url = url.strip()
    if not url:
        url = DEFAULT_ENGINE_URL

    info = _engine_reachable(url)
    if info is None:
        return StepResult(
            step="engine", status=STATUS_ERROR,
            message=f"VOICEVOX Engine not reachable at {url}",
            hint="Start VOICEVOX before running setup",
        )

    detail = f"version={info['version']}"
    if "speakers_count" in info:
        detail += f", speakers={info['speakers_count']}"

    written = _update_engine_setting(
        ctx.cwd, url, dry_run=ctx.dry_run,
    )
    msg = f"connected to VOICEVOX at {url}"
    if written is not None and not ctx.dry_run:
        msg += f"\n  wrote voicevox.engines to {written}"
    elif written is not None and ctx.dry_run:
        msg += f"\n  [dry-run] would write voicevox.engines to {written}"

    return StepResult(
        step="engine", status=STATUS_OK,
        message=msg, detail=detail,
    )


# ---------------------------------------------------------------------------
# step: e2k (helper)
# ---------------------------------------------------------------------------


def _check_e2k_installed(venv_python: Path) -> bool:
    """voiceClaude の .venv 内に e2k がインストール済みか確認。

    優先順:
      1. .venv/bin/python で import e2k チェック (推奨環境)
      2. システム python で確認 (fallback)
    """
    e2k_found = False
    if venv_python.exists():
        try:
            proc = subprocess.run(
                [str(venv_python), "-c", "import e2k"],
                capture_output=True,
                timeout=5,
            )
            e2k_found = proc.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            pass

    # .venv になければシステム python で確認
    if not e2k_found:
        e2k_dep = _deps.by_name("e2k")
        if e2k_dep is not None and _deps.check(e2k_dep).found:
            e2k_found = True

    return e2k_found


def _check_rumps_installed(venv_python: Path) -> bool:
    """macOS 専用(B-151): voiceClaude の .venv 内に rumps が入っているか確認。

    rumps は pyproject.toml のコア依存 (`sys_platform == 'darwin'` marker) な
    ので、通常は `uv sync` だけで .venv に入る。menubar は .venv の python で
    動かす前提のため、判定は venv_python 限定(e2k と異なりシステム python
    fallback は持たない)。fallback を持たせると、`python3` が PATH 上で
    voiceClaude 自身の .venv を指す開発環境(例: `uv run` 経由のテスト実行)
    で venv 不在/import 失敗のケースを正しく検出できなくなるため。
    """
    if not venv_python.exists():
        return False
    try:
        proc = subprocess.run(
            [str(venv_python), "-c", "import rumps"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------------
# 現在状態サマリ（run_setup 冒頭で表示）
# ---------------------------------------------------------------------------

_SUMMARY_TIMEOUT_SEC = 1.0


def _get_setup_status(ctx: SetupContext) -> Dict[str, Any]:
    """現在の engine / e2k / hook 状態を読み取り専用でチェックする。
    エラーは無視して状態を返す（run_setup 冒頭のサマリ用）。"""
    url = ctx.engine_url or DEFAULT_ENGINE_URL
    base = _normalize_engine_base(url)
    v, _ = _http_get_impl(f"{base}/version", _SUMMARY_TIMEOUT_SEC)
    engine_info: Optional[Dict[str, Any]] = (
        {"version": v.strip().strip('"')} if v else None
    )

    project_dir = SCRIPT_DIR.parent
    venv_python = project_dir / ".venv" / "bin" / "python"
    e2k_installed = _check_e2k_installed(venv_python)

    # rumps (B-151, menubar UI): macOS 専用機能。非 macOS では pyproject の
    # sys_platform=='darwin' marker によりそもそも導入対象外なので、案内
    # 自体を出さない(is_macos=False のときは rumps_installed=None のまま)。
    is_macos = platform.system() == "Darwin"
    rumps_installed = _check_rumps_installed(venv_python) if is_macos else None

    hook_scopes: Dict[str, str] = {}
    for scope in _hi.SCOPES:
        path = _hi.resolve_settings_path(scope, cwd=ctx.cwd, home=ctx.home)
        if path is None:
            hook_scopes[scope] = "not-registered"
            continue
        data, _ = _hi._read_settings(path)
        if data is None:
            hook_scopes[scope] = "not-registered"
            continue
        stops = (data.get("hooks") or {}).get("Stop") or []
        found = any(
            _hi.is_voiceclaude_hook(h) for h in stops if isinstance(h, dict)
        )
        hook_scopes[scope] = "registered" if found else "not-registered"

    return {
        "engine_url": url,
        "engine_info": engine_info,
        "e2k_installed": e2k_installed,
        "is_macos": is_macos,
        "rumps_installed": rumps_installed,
        "hook_scopes": hook_scopes,
    }


def _print_setup_status(status: Dict[str, Any], stream: Any) -> None:
    """現在の状態サマリを表示する。"""
    stream.write("─" * 52 + "\n")

    engine_info = status["engine_info"]
    url = status["engine_url"]
    if engine_info is not None:
        ver = engine_info.get("version", "?")
        stream.write(f"  engine  {url}  ✓ connected (v{ver})\n")
    else:
        stream.write(f"  engine  {url}  ✗ not reachable\n")

    e2k_label = "✓ installed" if status["e2k_installed"] else "- not installed"
    stream.write(f"  e2k     {e2k_label}\n")

    # rumps: macOS のみ表示(非 macOS ではそもそも対象外なので案内しない)
    if status.get("is_macos"):
        rumps_label = "✓ installed" if status["rumps_installed"] else "- not installed"
        stream.write(f"  rumps   {rumps_label}  (menubar UI, `uv sync` で導入)\n")

    hook_scopes = status["hook_scopes"]
    parts = []
    for s in _hi.SCOPES:
        mark = "✓" if hook_scopes.get(s) == "registered" else "-"
        parts.append(f"{s} {mark}")
    stream.write(f"  hook    {' | '.join(parts)}  （- = 未登録）\n")

    stream.write("─" * 52 + "\n")


# ---------------------------------------------------------------------------
# step: e2k
# ---------------------------------------------------------------------------


def step_e2k(ctx: SetupContext) -> StepResult:
    if ctx.skip_e2k:
        return StepResult(
            step="e2k", status=STATUS_SKIPPED,
            message="e2k step skipped (--skip-e2k)",
        )

    # インストール決定を先に行う(明示的な skip 判定)
    install: bool
    if ctx.install_e2k is not None:
        install = ctx.install_e2k
    elif ctx.yes:
        # --yes default: skip(重い依存を勝手に入れない)
        install = False
    else:
        install = _prompt_yes_no(
            ctx,
            "Install e2k (English-to-kana, recommended)?",
            default=False,
        )

    # ユーザーがインストール不要と判定した場合
    if not install:
        return StepResult(
            step="e2k", status=STATUS_SKIPPED,
            message="e2k installation skipped",
            hint="WORD_KANA dictionary + per-letter fallback will be used",
        )

    # インストール予定の場合、既に入っているか確認 (R-009 対応: .venv を優先)
    project_dir = SCRIPT_DIR.parent
    venv_python = project_dir / ".venv" / "bin" / "python"
    e2k_found = _check_e2k_installed(venv_python)

    if e2k_found:
        return StepResult(
            step="e2k", status=STATUS_OK,
            message="e2k already installed",
        )

    if ctx.dry_run:
        return StepResult(
            step="e2k", status=STATUS_OK,
            message="[dry-run] would install e2k via pip / uv",
        )

    # 実インストール: uv 優先、無ければ pip
    repo_root = ctx.repo_root or Path(__file__).resolve().parent.parent
    venv_python = repo_root / ".venv" / "bin" / "python"
    runner = ctx.runner or _default_runner

    cmd: List[str]
    if shutil.which("uv"):
        # uv pip install --python <venv_python> e2k(venv 無ければ作る)
        if not venv_python.exists():
            try:
                runner(
                    ["uv", "venv", str(repo_root / ".venv")],
                    capture_output=True, text=True, timeout=60,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                return StepResult(
                    step="e2k", status=STATUS_WARN,
                    message=f"uv venv creation failed: {e}",
                    hint="WORD_KANA dictionary fallback will be used",
                )
        cmd = ["uv", "pip", "install", "--python", str(venv_python), "e2k"]
    elif venv_python.exists():
        cmd = [str(venv_python), "-m", "pip", "install", "e2k"]
    elif shutil.which("python3"):
        # 最後の砦: python3 -m venv + pip
        try:
            runner(
                ["python3", "-m", "venv", str(repo_root / ".venv")],
                capture_output=True, text=True, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return StepResult(
                step="e2k", status=STATUS_WARN,
                message=f"python3 -m venv failed: {e}",
                hint="WORD_KANA dictionary fallback will be used",
            )
        cmd = [str(venv_python), "-m", "pip", "install", "e2k"]
    else:
        return StepResult(
            step="e2k", status=STATUS_WARN,
            message="no python3 / uv found for e2k install",
            hint="WORD_KANA dictionary fallback will be used",
        )

    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        return StepResult(
            step="e2k", status=STATUS_WARN,
            message=f"e2k install failed: {e}",
            hint="WORD_KANA dictionary fallback will be used",
        )
    if proc.returncode != 0:
        return StepResult(
            step="e2k", status=STATUS_WARN,
            message=f"e2k install exit {proc.returncode}",
            detail=(proc.stderr or "").strip()[-300:],
            hint="WORD_KANA dictionary fallback will be used",
        )

    return StepResult(
        step="e2k", status=STATUS_OK,
        message="e2k installed",
    )


# ---------------------------------------------------------------------------
# step: hook
# ---------------------------------------------------------------------------


def step_hook(ctx: SetupContext) -> StepResult:
    if ctx.skip_hook:
        return StepResult(
            step="hook", status=STATUS_SKIPPED,
            message="hook step skipped (--skip-hook)",
        )

    out = ctx.out_stream or sys.stdout
    scope = ctx.hook_scope

    # Git 配下チェック（U-105）: 対話モードでのみ適用。Git 外では user scope を推奨
    if not ctx.yes and not _in_git_repo(ctx.cwd) and scope in ("project-local", "project"):
        out.write(
            "  ⚠  Git リポジトリ外で実行しています。\n"
            "     project-local / project scope は .claude/ ディレクトリを必要とします。\n"
            "     推奨 scope を user に変更します。\n"
        )
        scope = "user"

    # scope の対話確認(--yes なら scope をそのまま使う) (U-113)
    # prompt_choice の番号メニューに統一。default に渡す `scope` は Git 外補正後
    # の値で、常に SCOPES のメンバーであること(prompt_choice の [default] 表示は
    # 文字列一致のため)。空入力は default を返すので無条件代入で良い。
    if not ctx.yes and (ctx.hook_scope == _hi.DEFAULT_SCOPE or scope != ctx.hook_scope):
        scope = _prompt_choice(
            "Hook scope を選択してください:",
            list(_hi.SCOPES),
            scope,
            in_stream=ctx.in_stream,
            out_stream=ctx.out_stream,
        )
    # prompt_choice は SCOPES 内の値しか返さないが、防御的にチェックを残す
    if scope not in _hi.SCOPES:
        return StepResult(
            step="hook", status=STATUS_ERROR,
            message=f"unknown hook scope: {scope!r}",
        )

    result = _hi.install(
        scope=scope,
        cwd=ctx.cwd,
        home=ctx.home,
        repo_root=ctx.repo_root,
        dry_run=ctx.dry_run,
        yes=True,  # hook_install 内部の対話 placeholder は常に yes 扱い
    )

    if result.error:
        return StepResult(
            step="hook", status=STATUS_ERROR,
            message=result.error,
        )
    if result.dry_run:
        if result.skipped_already_present:
            return StepResult(
                step="hook", status=STATUS_OK,
                message=f"[dry-run] hook already registered in {result.settings_path}",
            )
        return StepResult(
            step="hook", status=STATUS_OK,
            message=f"[dry-run] would register hook in {result.settings_path}",
            detail=f"command={result.hook_command}",
        )
    if result.skipped_already_present:
        return StepResult(
            step="hook", status=STATUS_OK,
            message=f"hook already registered in {result.settings_path}",
        )
    return StepResult(
        step="hook", status=STATUS_OK,
        message=f"registered hook in {result.settings_path}",
        detail=f"command={result.hook_command}",
    )


# ---------------------------------------------------------------------------
# step: mcp
# ---------------------------------------------------------------------------

_MCP_SYNC_TIMEOUT_SEC = 120


def _check_mcp_installed(repo_root: Path) -> bool:
    """voiceClaude .venv に mcp distribution が入っているかを確認する。

    dependency gate の責務は「dist がインストール済みか」だけ。重い
    `import mcp`(pydantic 等 ~378 モジュールを cold compile する)は責務過剰で、
    `uv sync` 直後の初回 import が `timeout` を超過して誤 WARN を出す原因に
    なり得る。doctor.py と同じく軽量な `importlib.metadata.version()` で確認する。
    runtime import / server 起動は MCP server テスト・E2E で担保する。"""
    venv_python = repo_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return False
    try:
        r = subprocess.run(
            [str(venv_python), "-c",
             "import importlib.metadata as m; m.version('mcp')"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def step_mcp(ctx: SetupContext) -> StepResult:
    if ctx.skip_mcp:
        return StepResult(
            step="mcp", status=STATUS_SKIPPED,
            message="mcp step skipped (--skip-mcp)",
        )

    # --yes 単体は skip（opt-in が必要）
    if not ctx.with_mcp and ctx.yes:
        return StepResult(
            step="mcp", status=STATUS_SKIPPED,
            message="mcp step skipped (use --with-mcp to enable)",
        )

    # 通常対話: default=No
    if not ctx.with_mcp:
        answer = _prompt_yes_no(ctx, "Set up MCP server integration? (optional)", default=False)
        if not answer:
            return StepResult(
                step="mcp", status=STATUS_SKIPPED,
                message="mcp step skipped",
            )

    # -----------------------------------------------------------------------
    # mcp package チェック + インストール
    # -----------------------------------------------------------------------
    repo_root = ctx.repo_root or SCRIPT_DIR.parent
    runner = ctx.runner or _default_runner

    if not _check_mcp_installed(repo_root):
        if ctx.dry_run:
            return StepResult(
                step="mcp", status=STATUS_OK,
                message="[dry-run] would run: uv sync --extra mcp",
            )
        if not shutil.which("uv"):
            return StepResult(
                step="mcp", status=STATUS_WARN,
                message="uv が見つかりません。mcp package を手動でインストールしてください",
                hint=f"cd {repo_root} && uv sync --extra mcp",
            )
        try:
            proc = runner(
                ["uv", "sync", "--extra", "mcp"],
                capture_output=True, text=True,
                timeout=_MCP_SYNC_TIMEOUT_SEC,
                cwd=str(repo_root),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return StepResult(
                step="mcp", status=STATUS_WARN,
                message=f"uv sync failed: {e}",
                hint=f"手動でインストール: cd {repo_root} && uv sync --extra mcp",
            )
        if proc.returncode != 0:
            return StepResult(
                step="mcp", status=STATUS_WARN,
                message=f"uv sync exit {proc.returncode}",
                detail=(proc.stderr or "").strip()[-300:],
                hint=f"手動でインストール: cd {repo_root} && uv sync --extra mcp",
            )
        # sync 後にもう一度確認
        if not _check_mcp_installed(repo_root):
            return StepResult(
                step="mcp", status=STATUS_WARN,
                message="uv sync 後も mcp package の確認に失敗",
                hint=f"cd {repo_root} && uv sync --extra mcp を再実行してください",
            )

    # -----------------------------------------------------------------------
    # claude CLI チェック
    # -----------------------------------------------------------------------
    if not shutil.which("claude"):
        vvread_path = str(repo_root / "bin" / "vvread")
        return StepResult(
            step="mcp", status=STATUS_WARN,
            message="claude CLI が見つかりません",
            hint=(
                "手動で登録してください:\n"
                f"  claude mcp add --transport stdio --scope local vvread "
                f"-- {vvread_path} mcp"
            ),
        )

    # -----------------------------------------------------------------------
    # 登録済みチェック
    # -----------------------------------------------------------------------
    try:
        check = runner(
            ["claude", "mcp", "get", "vvread"],
            capture_output=True, text=True,
            timeout=10,
            cwd=str(ctx.cwd),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return StepResult(
            step="mcp", status=STATUS_WARN,
            message=f"claude mcp get failed: {e}",
        )
    if check.returncode == 0:
        return StepResult(
            step="mcp", status=STATUS_WARN,
            message="vvread already registered (not overwriting)",
            hint="To update: claude mcp remove vvread && vvread setup --with-mcp",
        )

    # -----------------------------------------------------------------------
    # 登録
    # -----------------------------------------------------------------------
    vvread_path = str(repo_root / "bin" / "vvread")
    if ctx.dry_run:
        return StepResult(
            step="mcp", status=STATUS_OK,
            message=(
                "[dry-run] would run: claude mcp add --transport stdio --scope local "
                f"vvread -- {vvread_path} mcp"
            ),
        )
    try:
        add = runner(
            [
                "claude", "mcp", "add",
                "--transport", "stdio",
                "--scope", "local",
                "vvread", "--", vvread_path, "mcp",
            ],
            capture_output=True, text=True,
            timeout=15,
            cwd=str(ctx.cwd),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return StepResult(
            step="mcp", status=STATUS_WARN,
            message=f"claude mcp add failed: {e}",
        )
    if add.returncode != 0:
        return StepResult(
            step="mcp", status=STATUS_WARN,
            message=f"claude mcp add exit {add.returncode}",
            detail=(add.stderr or "").strip()[-300:],
        )
    return StepResult(
        step="mcp", status=STATUS_OK,
        message="registered vvread MCP server",
        detail=f"command={vvread_path} mcp",
    )


# ---------------------------------------------------------------------------
# receiver セットアップ (B-138/B-149)
# ---------------------------------------------------------------------------
#
# --with-mcp とは別フラグ --with-receiver で扱う（receiver は research preview +
# Bun 依存のため）。--with-mcp の意味は変えない（Python MCP Tools のみ）。
# 副作用（bun 依存インストール + current project への local 登録）は対話で事前確認。
# --yes --with-receiver のみ確認省略。--dry-run は外部設定を書き換えない。

import receiver_install as _ri  # noqa: E402
import launch_agent as _la  # noqa: E402 (B-156, macOS-only)


def _receiver_sdk_installed(receiver_dir: Path) -> bool:
    """receiver/ に MCP SDK が bun install 済みかを確認する。"""
    return (receiver_dir / "node_modules" / "@modelcontextprotocol" / "sdk").exists()


def step_receiver(ctx: SetupContext) -> StepResult:
    # --yes 単体は skip（opt-in が必要）
    if not ctx.with_receiver and ctx.yes:
        return StepResult(
            step="receiver", status=STATUS_SKIPPED,
            message="receiver step skipped (use --with-receiver to enable)",
        )

    # 対話: "Set up external-event receiver?" (default=No)
    if not ctx.with_receiver:
        answer = _prompt_yes_no(
            ctx,
            "Set up external-event receiver?\n"
            "  (Claude Code Channels / experimental, requires Bun)",
            default=False,
        )
        if not answer:
            return StepResult(
                step="receiver", status=STATUS_SKIPPED,
                message="receiver step skipped",
            )

    repo_root = ctx.repo_root or SCRIPT_DIR.parent
    runner = ctx.runner or _default_runner
    receiver_dir = repo_root / "receiver"
    server_path = receiver_dir / "server.ts"
    launch_hint = (
        "claude --dangerously-load-development-channels server:vvread-receiver"
    )

    # opt-in 後の確認: 実行内容を表示して Continue? [y/N]
    # --yes (--with-receiver 付き) なら省略。--dry-run も不要。
    if not ctx.yes and not ctx.dry_run:
        out = ctx.out_stream or sys.stdout
        out.write(
            "以下の変更を行います:\n"
            "  - receiver の依存を Bun でインストール（必要な場合のみ）\n"
            "  - vvread MCP tools を現在の project に登録（未登録の場合のみ）\n"
            "  - vvread-receiver を Claude Code の local scope に登録\n"
        )
        ok = _prompt_yes_no(ctx, "続けますか?", default=False)
        if not ok:
            return StepResult(
                step="receiver", status=STATUS_SKIPPED,
                message="receiver step skipped (declined)",
            )

    # 1. bun 存在確認（任意機能なので無ければ WARN + 手動手順、setup は継続）
    if not shutil.which("bun"):
        return StepResult(
            step="receiver", status=STATUS_WARN,
            message="bun が見つかりません（receiver は任意機能）",
            hint=(
                "Bun をインストール後、手動で:\n"
                f"  cd {receiver_dir} && bun install --frozen-lockfile\n"
                f"  claude mcp add --transport stdio --scope local vvread-receiver "
                f"-- bun {server_path}"
            ),
        )

    # 2. 依存 + 登録（dry-run は何も実行せず予定だけ返す）
    needs_install = not _receiver_sdk_installed(receiver_dir)
    if ctx.dry_run:
        plan = []
        if needs_install:
            plan.append(f"cd {receiver_dir} && bun install --frozen-lockfile")
        plan.append(
            "claude mcp add --transport stdio --scope local vvread-receiver "
            f"-- bun {server_path}"
        )
        return StepResult(
            step="receiver", status=STATUS_OK,
            message="[dry-run] would run:\n  " + "\n  ".join(plan),
            hint=f"起動: {launch_hint}",
        )

    if needs_install:
        ok = _ri.ensure_receiver_dependencies(receiver_dir, dry_run=False, runner=runner)
        if not ok:
            return StepResult(
                step="receiver", status=STATUS_WARN,
                message="bun install failed",
                hint=f"手動で: cd {receiver_dir} && bun install --frozen-lockfile",
            )

    # 3. claude CLI チェック
    if not shutil.which("claude"):
        return StepResult(
            step="receiver", status=STATUS_WARN,
            message="claude CLI が見つかりません",
            hint=(
                "手動で登録してください:\n"
                f"  claude mcp add --transport stdio --scope local vvread-receiver "
                f"-- bun {server_path}"
            ),
        )

    # 既登録チェック（local scope のみ、.mcp.json は変更しない）
    status = _ri.get_receiver_registration_status(runner)
    if status == "registered_local":
        return StepResult(
            step="receiver", status=STATUS_WARN,
            message="vvread-receiver already registered (not overwriting)",
            hint=f"起動: {launch_hint}",
        )
    if status == "conflicting_non_local":
        return StepResult(
            step="receiver", status=STATUS_WARN,
            message="vvread-receiver は project/global scope で登録済みです。手動で整理してください。",
            hint="claude mcp remove vvread-receiver 後に再試行してください。",
        )

    # 登録
    ok = _ri.register_receiver_mcp(repo_root, dry_run=False, runner=runner)
    if not ok:
        return StepResult(
            step="receiver", status=STATUS_WARN,
            message="claude mcp add failed",
        )
    return StepResult(
        step="receiver", status=STATUS_OK,
        message="registered vvread-receiver MCP server",
        detail=f"command=bun {server_path}",
        hint=f"起動: {launch_hint}",
    )


# ---------------------------------------------------------------------------
# step: menubar (B-156)
# ---------------------------------------------------------------------------
#
# rumps 製 menubar UI (B-151) のログイン時自動起動(LaunchAgent)を登録する。
# mcp/receiver と同じ opt-in 規約(--yes 単体では有効化しない)に従うが、
# 対話プロンプトの既定値だけは他 step と異なり default=True にする
# (「ログイン時に自動で立ち上がってほしい」が menubar 利用者の自然な期待の
# ため。mcp/receiver は重い依存 / experimental 機能なので default=False)。
# macOS 専用機能なので非 macOS では常に SKIPPED("not macOS")。


def _resolve_menubar_log_dir(ctx: SetupContext) -> Path:
    """LaunchAgent の StandardOutPath/StandardErrorPath に使う log dir を解決する。

    paths.py の log_dir() と同じ優先順位(VVREAD_LOG_DIR env > OS 既定値)を
    踏襲するが、OS 既定値の解決に実プロセスの $HOME ではなく ctx.home を使う
    (テストで home を tmp_path に差し替えられるようにするため。menubar
    LaunchAgent は macOS 専用機能なので macOS の既定値のみを使う)。
    """
    override = os.environ.get("VVREAD_LOG_DIR", "")
    if override:
        return Path(os.path.expanduser(override))
    return ctx.home / "Library" / "Logs" / "vvread"


def step_menubar(ctx: SetupContext) -> StepResult:
    # 事前バリデーション: 相互排他(mcp/receiver は argparse のみで防ぐが、
    # menubar は ctx を直接組み立てて呼ぶケース(テスト等)も守るため関数内でも
    # 検証する)
    if ctx.skip_menubar and ctx.with_menubar:
        return StepResult(
            step="menubar", status=STATUS_ERROR,
            message="--skip-menubar and --with-menubar are mutually exclusive",
        )

    if platform.system() != "Darwin":
        return StepResult(
            step="menubar", status=STATUS_SKIPPED,
            message="menubar step skipped (not macOS)",
        )

    if ctx.skip_menubar:
        return StepResult(
            step="menubar", status=STATUS_SKIPPED,
            message="menubar step skipped (--skip-menubar)",
        )

    # --yes 単体は skip(opt-in が必要、mcp/receiver と同じ非対話規約)
    if not ctx.with_menubar and ctx.yes:
        return StepResult(
            step="menubar", status=STATUS_SKIPPED,
            message="menubar step skipped (use --with-menubar to enable)",
        )

    # 通常対話: default=Yes(他 step と異なり既定で有効にする)
    if not ctx.with_menubar:
        answer = _prompt_yes_no(
            ctx, "Enable menubar auto-start on login? (optional)", default=True,
        )
        if not answer:
            return StepResult(
                step="menubar", status=STATUS_SKIPPED,
                message="menubar step skipped",
            )

    repo_root = ctx.repo_root or SCRIPT_DIR.parent
    runner = ctx.runner or _default_runner
    log_dir = _resolve_menubar_log_dir(ctx)

    result = _la.register(
        repo_root=repo_root,
        log_dir=log_dir,
        home=ctx.home,
        uid=os.getuid(),
        runner=runner,
        dry_run=ctx.dry_run,
    )

    if not result.rumps_available:
        return StepResult(
            step="menubar", status=STATUS_WARN,
            message="rumps not installed; menubar auto-start not registered",
            hint=_la._RUMPS_INSTALL_HINT,
        )

    if not result.ok:
        return StepResult(
            step="menubar", status=STATUS_WARN,
            message=result.message,
            detail=result.error,
            hint=(
                f"手動で確認/復旧: launchctl bootstrap gui/{os.getuid()} "
                f"{shlex.quote(str(result.plist_path))}"
            ),
        )

    return StepResult(
        step="menubar", status=STATUS_OK,
        message=result.message,
    )


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def run_setup(ctx: SetupContext) -> List[StepResult]:
    """6 step を順次実行。ERROR が出たら以降をスキップ(連鎖失敗を防ぐ)。"""
    # 対話モードかつ stdin が tty でない → ERROR
    pre = _require_tty_or_yes(ctx)
    if pre is not None:
        return [pre]

    # 現在の状態サマリを表示（ctx.json_mode が有効なときは出力しない）
    if not getattr(ctx, "json_mode", False):
        out = ctx.out_stream or sys.stdout
        _print_setup_status(_get_setup_status(ctx), out)

    results: List[StepResult] = []
    abort = False

    for step_fn in (step_engine, step_e2k, step_hook, step_mcp, step_receiver, step_menubar):
        if abort:
            results.append(StepResult(
                step=step_fn.__name__.replace("step_", ""),
                status=STATUS_SKIPPED,
                message="skipped due to earlier ERROR",
            ))
            continue
        result = step_fn(ctx)
        results.append(result)
        if result.status == STATUS_ERROR:
            abort = True
    return results


def _emit_text(results: List[StepResult], stream: Any) -> None:
    for i, r in enumerate(results, 1):
        tag = r.status
        stream.write(f"[{i}/{len(results)}] {r.step}: {tag}\n")
        for line in r.message.splitlines():
            stream.write(f"  {line}\n")
        if r.detail:
            stream.write(f"  detail: {r.detail}\n")
        if r.hint:
            stream.write(f"  hint: {r.hint}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "vvread setup: 初回セットアップ。"
            "VOICEVOX Engine 接続確認・依存確認（e2k）・Claude Code hook 登録・"
            "project settings 作成をまとめて行う。"
            "setup 後は vvread config を実行すると project settings を作成・編集できる状態になる。"
        )
    )
    parser.add_argument(
        "--engine-url",
        default=None,
        help=f"VOICEVOX Engine base URL (default: {DEFAULT_ENGINE_URL})",
    )
    parser.add_argument(
        "--scope",
        choices=_hi.SCOPES,
        default=_hi.DEFAULT_SCOPE,
        help=f"Claude Code hook scope (default: {_hi.DEFAULT_SCOPE})",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="non-interactive mode: accept all defaults",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="do not change anything; print what each step would do",
    )
    parser.add_argument(
        "--skip-engine", action="store_true",
        help="skip the engine step",
    )
    parser.add_argument(
        "--skip-e2k", action="store_true",
        help="skip the e2k step",
    )
    parser.add_argument(
        "--skip-hook", action="store_true",
        help="skip the hook registration step",
    )
    mcp_group = parser.add_mutually_exclusive_group()
    mcp_group.add_argument(
        "--skip-mcp", action="store_true",
        help="skip the MCP registration step",
    )
    mcp_group.add_argument(
        "--with-mcp", action="store_true",
        help="run MCP registration step (even with --yes)",
    )
    parser.add_argument(
        "--with-receiver", action="store_true",
        help="set up receiver (Bun deps + local registration of vvread-receiver)",
    )
    menubar_group = parser.add_mutually_exclusive_group()
    menubar_group.add_argument(
        "--skip-menubar", action="store_true",
        help="skip the menubar auto-start (LaunchAgent) step",
    )
    menubar_group.add_argument(
        "--with-menubar", action="store_true",
        help="register menubar auto-start LaunchAgent (even with --yes), macOS only",
    )
    parser.add_argument(
        "--install-e2k",
        action="store_true",
        help="force e2k installation (overrides --yes default of skip)",
    )
    parser.add_argument(
        "--no-install-e2k",
        action="store_true",
        help="force e2k skip even in interactive mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit step results as JSON",
    )

    args = parser.parse_args(argv)

    # repo_root: VVREAD_PROJECT_DIR が export されていればそれ、無ければ
    # __file__ から解決
    rr_env = os.environ.get("VVREAD_PROJECT_DIR")
    repo_root = Path(rr_env) if rr_env else Path(__file__).resolve().parent.parent

    # install_e2k の解決(両 flag 同時指定はエラー)
    install_e2k_explicit: Optional[bool] = None
    if args.install_e2k and args.no_install_e2k:
        print(
            "setup: --install-e2k and --no-install-e2k are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if args.install_e2k:
        install_e2k_explicit = True
    elif args.no_install_e2k:
        install_e2k_explicit = False

    ctx = SetupContext(
        yes=args.yes,
        dry_run=args.dry_run,
        engine_url=args.engine_url,
        skip_engine=args.skip_engine,
        skip_e2k=args.skip_e2k,
        skip_hook=args.skip_hook,
        skip_mcp=args.skip_mcp,
        with_mcp=args.with_mcp,
        with_receiver=args.with_receiver,
        skip_menubar=args.skip_menubar,
        with_menubar=args.with_menubar,
        install_e2k=install_e2k_explicit,
        hook_scope=args.scope,
        repo_root=repo_root,
        json_mode=args.json,
    )

    results = run_setup(ctx)

    if args.json:
        payload = [asdict(r) for r in results]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _emit_text(results, sys.stdout)

    if any(r.status == STATUS_ERROR for r in results):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # UX 優先で対話キャンセルを正常終了扱い (exit 0)。
        # SIGINT の慣習 (exit 130) とは異なる意図的な設計判断。
        sys.stderr.write("\nキャンセルしました。\n")
        sys.exit(0)
