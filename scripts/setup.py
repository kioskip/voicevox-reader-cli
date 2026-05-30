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

Exit code:
  0 = 全 step OK / WARN / SKIPPED
  1 = いずれかの step ERROR
  2 = 使い方エラー(argparse default、不正オプション)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent

import dependencies as _deps  # noqa: E402
import hook_install as _hi  # noqa: E402
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


def _update_engine_url_setting(
    cwd: Path,
    new_url: str,
    *,
    dry_run: bool = False,
) -> Optional[Path]:
    """`<cwd>/vvread.settings.json` の `voicevox.engineUrl` を new_url に
    更新する。既存ファイルが無ければ新規作成。default 値と同じなら no-op。

    戻り値: 書き込み実施したら settings_path、no-op (default 一致 or dry_run)
    なら None。
    """
    if new_url == DEFAULT_ENGINE_URL:
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
    if voicevox.get("engineUrl") == new_url:
        return None  # 既に同じ値、no-op

    voicevox["engineUrl"] = new_url

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

    written = _update_engine_url_setting(
        ctx.cwd, url, dry_run=ctx.dry_run,
    )
    msg = f"connected to VOICEVOX at {url}"
    if written is not None and not ctx.dry_run:
        msg += f"\n  wrote voicevox.engineUrl to {written}"
    elif written is not None and ctx.dry_run:
        msg += f"\n  [dry-run] would write voicevox.engineUrl to {written}"

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
# orchestrator
# ---------------------------------------------------------------------------


def run_setup(ctx: SetupContext) -> List[StepResult]:
    """3 step を順次実行。ERROR が出たら以降をスキップ(連鎖失敗を防ぐ)。"""
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

    for step_fn in (step_engine, step_e2k, step_hook):
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
