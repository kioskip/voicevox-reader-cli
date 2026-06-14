#!/usr/bin/env python3
"""scripts/doctor.py - vvread doctor: 環境診断 (R-009)

R-024 dependencies + R-025 settings の最初の consumer。診断のみで「直す」
コマンドではない(ファイル変更しない)。各セクションが OK / INFO / WARN /
ERROR の CheckItem を返し、main で集約 → 表示 → exit code 決定。

セクション (8):
  paths        : R-001 path 解決 (state / log / cache)
  settings     : R-025 全 SCHEMA 値 + 由来 + unknown_keys / parse_errors
  dependencies : R-024 runtime required + optional check
                 (--scope all で setup/dev/publish も)
  player       : lib_playback の検出結果(bash subprocess 経由で drift 防止)
  engine       : VOICEVOX /version + /speakers + 設定 speaker の存在確認
                 (--offline でスキップ)
  claude       : claude --version → 2.1.110 比較(--offline でスキップ)
  hooks        : 3 階層の Stop hook 登録 + command 解決 + timeout + async
  vvread       : bin/vvread の PATH 解決 + symlink chain

Exit code (R-009 仕様):
  0 = OK or WARN のみ(warning だけでは non-zero にしない)
  1 = ERROR あり(必須機能が動かない見込み)
  2 = doctor 自体の使い方エラー / 不正オプション(argparse default)

CLI:
  vvread doctor [--offline] [--scope runtime|all] [--json]

--strict は v0.1 では未実装(将来 placeholder。終了コード設計は仕様通り
warning だけでは exit 0 のまま、--strict 時のみ exit 1 になる予定)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent

import dependencies as _deps  # noqa: E402
import paths as _paths  # noqa: E402
import settings as _settings  # noqa: E402
from hook_install import is_voiceclaude_hook  # noqa: E402  (drift 防止: R-008)
from lib_http import http_get as _http_get  # noqa: E402 (R-101)

# ---------------------------------------------------------------------------
# 共通データ型
# ---------------------------------------------------------------------------

STATUS_OK = "OK"
STATUS_INFO = "INFO"
STATUS_WARN = "WARN"
STATUS_ERROR = "ERROR"
STATUSES = (STATUS_OK, STATUS_INFO, STATUS_WARN, STATUS_ERROR)


@dataclass
class CheckItem:
    section: str
    label: str
    status: str
    detail: Optional[str] = None
    hint: Optional[str] = None


# ---------------------------------------------------------------------------
# section: paths
# ---------------------------------------------------------------------------


def check_paths() -> List[CheckItem]:
    items: List[CheckItem] = []
    for name, fn in (
        ("state", _paths.state_dir),
        ("log", _paths.log_dir),
        ("cache", _paths.cache_dir),
    ):
        try:
            p = fn()
        except Exception as e:  # noqa: BLE001
            items.append(CheckItem(
                section="paths", label=name,
                status=STATUS_ERROR,
                detail=f"resolution failed: {e}",
            ))
            continue
        # ディレクトリ存在は INFO(初回起動前は不在で normal)
        exists = p.exists()
        status = STATUS_OK if exists else STATUS_INFO
        detail = str(p) + ("" if exists else "  (will be created on first run)")
        items.append(CheckItem(
            section="paths", label=name,
            status=status, detail=detail,
        ))
    return items


# ---------------------------------------------------------------------------
# section: settings (R-025)
# ---------------------------------------------------------------------------


def check_settings(s: Optional[_settings.Settings] = None) -> List[CheckItem]:
    if s is None:
        s = _settings.load()

    items: List[CheckItem] = []
    # 全 SCHEMA 値を 1 行ずつ INFO として表示
    for key in sorted(s.values.keys()):
        rv = s.values[key]
        origin_str = rv.origin.source
        if rv.origin.detail:
            origin_str = f"{rv.origin.source}: {rv.origin.detail}"
        items.append(CheckItem(
            section="settings",
            label=key,
            status=STATUS_INFO,
            detail=f"{rv.value!r}  [{origin_str}]",
        ))

    # ロード元
    if s.sources:
        items.append(CheckItem(
            section="settings",
            label="sources",
            status=STATUS_OK,
            detail=", ".join(s.sources),
        ))

    # parse_errors / unknown_keys は warning
    for src, msg in s.parse_errors:
        items.append(CheckItem(
            section="settings",
            label="parse_error",
            status=STATUS_WARN,
            detail=f"{src}: {msg}",
            hint="設定ファイルの構文 / 型を確認してください",
        ))
    for fname, key in s.unknown_keys:
        items.append(CheckItem(
            section="settings",
            label="unknown_key",
            status=STATUS_WARN,
            detail=f"{key}  (in {fname})",
            hint="schema に無いキー(typo の可能性、forward compat で値は無視)",
        ))
    return items


# ---------------------------------------------------------------------------
# section: dependencies (R-024)
# ---------------------------------------------------------------------------


def check_dependencies(scope: str = "runtime",
                       project_dir: Optional[Path] = None) -> List[CheckItem]:
    """scope='runtime' は runtime カテゴリのみ、scope='all' は全カテゴリ。

    required 不在 → ERROR、optional 不在 → INFO(warning にすると数が多すぎ
    て signal が埋もれるため、ユーザ指定の「通常 doctor では warning 過多に
    しない」方針)。

    project_dir が渡されている場合、install_hint 内の相対パス (.venv/bin/python)
    を絶対パスに変換する(R-009 別プロジェクト実行対応)。
    """
    items: List[CheckItem] = []

    if scope == "runtime":
        deps = [d for d in _deps.DEPENDENCIES if d.category == "runtime"]
    elif scope == "all":
        deps = list(_deps.DEPENDENCIES)
    else:
        items.append(CheckItem(
            section="dependencies", label="scope",
            status=STATUS_ERROR,
            detail=f"unknown scope {scope!r}",
        ))
        return items

    for d in deps:
        # e2k の場合: project_dir が指定されていれば .venv/bin/python で check
        # (デフォルトは system python3 を見ていたため R-009 対応)
        if d.name == "e2k" and project_dir:
            venv_python = project_dir / ".venv" / "bin" / "python"
            if venv_python.exists():
                # .venv/bin/python で e2k import をテスト
                try:
                    proc = subprocess.run(
                        [str(venv_python), "-c", "import e2k"],
                        capture_output=True,
                        timeout=5,
                    )
                    result = _deps.CheckResult(
                        name=d.name,
                        found=proc.returncode == 0,
                        path=str(venv_python) if proc.returncode == 0 else None,
                    )
                except (subprocess.TimeoutExpired, Exception):
                    result = _deps.CheckResult(name=d.name, found=False)
            else:
                result = _deps.check(d)
        else:
            result = _deps.check(d)

        if result.found:
            tail = []
            if result.path:
                tail.append(result.path)
            if result.version:
                tail.append(result.version)
            detail = " / ".join(tail) if tail else None
            items.append(CheckItem(
                section="dependencies", label=d.name,
                status=STATUS_OK,
                detail=detail,
            ))
        else:
            # required は ERROR、optional は INFO(noise を減らす)
            if d.kind == "required":
                status = STATUS_ERROR
            else:
                status = STATUS_INFO
            hint = None
            if d.install_hint:
                # macOS / linux の優先順で 1 つ表示
                for key in ("macos", "linux", "uv"):
                    if key in d.install_hint:
                        hint = f"{key}: {d.install_hint[key]}"
                        # project_dir が指定されている場合、相対パスを絶対パスに変換
                        if project_dir and ".venv/bin/python" in hint:
                            abs_venv_python = project_dir / ".venv" / "bin" / "python"
                            hint = hint.replace(".venv/bin/python", str(abs_venv_python))
                        break
            items.append(CheckItem(
                section="dependencies", label=d.name,
                status=status,
                detail=f"missing  ({d.purpose})",
                hint=hint,
            ))
    return items


# ---------------------------------------------------------------------------
# section: player
# ---------------------------------------------------------------------------


def check_player(scripts_dir: Optional[Path] = None) -> List[CheckItem]:
    """lib_playback.sh::vvread_detect_player を bash subprocess で呼ぶ。

    Python 側で再実装すると drift するため、bash の検出結果をそのまま信用。
    VVREAD_PLAYER override も bash 側のロジックに任せる。
    """
    if scripts_dir is None:
        scripts_dir = SCRIPT_DIR
    lib_playback = scripts_dir / "lib" / "playback.sh"
    if not lib_playback.exists():
        return [CheckItem(
            section="player", label="lib/playback.sh",
            status=STATUS_ERROR,
            detail=f"not found: {lib_playback}",
        )]
    try:
        r = subprocess.run(
            ["bash", "-c",
             f'source "{lib_playback}" && vvread_detect_player'],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return [CheckItem(
            section="player", label="detect",
            status=STATUS_ERROR,
            detail=f"detection failed: {e}",
        )]

    if r.returncode == 0 and r.stdout.strip():
        player = r.stdout.strip()
        return [CheckItem(
            section="player", label="detected",
            status=STATUS_OK,
            detail=player,
        )]

    return [CheckItem(
        section="player", label="detected",
        status=STATUS_ERROR,
        detail="no audio player available",
        hint="VVREAD_PLAYER で明示指定するか、afplay (macOS) / "
             "paplay / pw-play / aplay / play / ffplay (Linux) を入れてください",
    )]


# ---------------------------------------------------------------------------
# section: engine (--offline でスキップ)
# ---------------------------------------------------------------------------




def _normalize_engine_base(url: str) -> str:
    """`/version` 付きや末尾スラッシュを剥がして base URL に正規化"""
    u = url.rstrip("/")
    if u.endswith("/version"):
        u = u[: -len("/version")]
    return u


def check_engine(
    engine_url: Optional[str] = None,
    target_speaker: Optional[int] = None,
    *,
    settings_obj: Optional[_settings.Settings] = None,
) -> List[CheckItem]:
    """VOICEVOX Engine の /version と /speakers を確認。

    target_speaker は settings の voicevox.speaker。/speakers の全 styles
    の id 集合に含まれていなければ ERROR。
    """
    items: List[CheckItem] = []

    if settings_obj is None:
        settings_obj = _settings.load()
    if engine_url is None:
        engine_url = settings_obj.values["voicevox.engineUrl"].value
    if target_speaker is None:
        target_speaker = settings_obj.values["voicevox.speaker"].value

    base = _normalize_engine_base(engine_url)

    # /version
    version_text, err = _http_get(f"{base}/version")
    if err:
        items.append(CheckItem(
            section="engine", label="reachable",
            status=STATUS_ERROR,
            detail=f"{base}: {err}",
            hint="VOICEVOX Engine が起動しているか確認してください "
                 "(docker compose up -d 等)",
        ))
        return items

    # version 文字列の抽出(JSON string or plain string)
    v = version_text.strip().strip('"')
    items.append(CheckItem(
        section="engine", label="reachable",
        status=STATUS_OK,
        detail=f"{base}  version={v}",
    ))

    # /speakers
    speakers_text, err = _http_get(f"{base}/speakers")
    if err:
        items.append(CheckItem(
            section="engine", label="speakers_endpoint",
            status=STATUS_WARN,
            detail=f"{base}/speakers: {err}",
            hint="speaker 検証ができません(/version は通っているので部分機能あり)",
        ))
        return items

    try:
        speakers = json.loads(speakers_text)
    except json.JSONDecodeError as e:
        items.append(CheckItem(
            section="engine", label="speakers_endpoint",
            status=STATUS_WARN,
            detail=f"invalid JSON from /speakers: {e}",
        ))
        return items

    # speakers は list[ {name, styles: [{name, id}, ...] } ]
    speaker_ids = set()
    if isinstance(speakers, list):
        for sp in speakers:
            if not isinstance(sp, dict):
                continue
            for st in sp.get("styles", []) or []:
                if isinstance(st, dict) and isinstance(st.get("id"), int):
                    speaker_ids.add(st["id"])

    items.append(CheckItem(
        section="engine", label="speakers",
        status=STATUS_OK,
        detail=f"{len(speaker_ids)} ids available",
    ))

    if isinstance(target_speaker, int) and target_speaker in speaker_ids:
        items.append(CheckItem(
            section="engine", label="target_speaker",
            status=STATUS_OK,
            detail=f"{target_speaker} ∈ /speakers",
        ))
    else:
        items.append(CheckItem(
            section="engine", label="target_speaker",
            status=STATUS_ERROR,
            detail=f"speaker={target_speaker!r} not found in /speakers "
                   f"(available={len(speaker_ids)} ids)",
            hint="VOICEVOX_SPEAKER または settings の voicevox.speaker を "
                 "/speakers に存在する ID に変更してください",
        ))

    return items


def check_engines_multi(
    settings_obj: Optional[_settings.Settings] = None,
) -> List[CheckItem]:
    """voicevox.engines の全 URL を疎通確認する（B-125）。

    severity 規則:
    - 全エンジン到達可能 → STATUS_OK
    - 一部エンジン到達不可 → STATUS_WARN（doctor exit 0 のまま）
    - 全エンジン到達不可 → STATUS_ERROR（doctor exit 1）

    単一エンジン設定（engines が 1 要素）の場合は check_engine() と同じ挙動。
    """
    if settings_obj is None:
        settings_obj = _settings.load()

    engines_rv = settings_obj.values.get("voicevox.engines")
    engines: List[str] = engines_rv.value if engines_rv and engines_rv.value else []

    # 単一エンジン / engines 未設定: 現行 check_engine() をそのまま使う（二重チェックなし）
    if len(engines) <= 1:
        return check_engine(settings_obj=settings_obj)

    items: List[CheckItem] = []

    # /version 疎通確認（全エンジン）
    reachable: List[tuple] = []
    unreachable: List[tuple] = []
    for url in engines:
        base = _normalize_engine_base(url)
        version_text, err = _http_get(f"{base}/version")
        if err:
            unreachable.append((url, base, err))
        else:
            v = version_text.strip().strip('"')
            reachable.append((url, base, v))

    all_failed = len(reachable) == 0
    status_ng = STATUS_ERROR if all_failed else STATUS_WARN

    for _url, base, v in reachable:
        items.append(CheckItem(
            section="engine", label="reachable",
            status=STATUS_OK,
            detail=f"{base}  version={v}",
        ))
    for _url, base, err in unreachable:
        items.append(CheckItem(
            section="engine", label="reachable",
            status=status_ng,
            detail=f"{base}: {err}",
            hint="VOICEVOX Engine が起動しているか確認してください",
        ))

    # primary engine が到達可能な場合: /speakers + target speaker の詳細チェック
    # reachable の先頭を primary として使う（設定順を優先）
    if reachable:
        primary_url = reachable[0][0]
        primary_items = check_engine(engine_url=primary_url, settings_obj=settings_obj)
        # /version check は既に上で実施済みなので label="reachable" は除外
        items.extend(i for i in primary_items if i.label != "reachable")

    return items


def engine_section_skipped() -> List[CheckItem]:
    return [CheckItem(
        section="engine", label="skipped",
        status=STATUS_INFO,
        detail="--offline (network checks skipped)",
    )]


# ---------------------------------------------------------------------------
# section: claude (--offline でスキップ)
# ---------------------------------------------------------------------------


_CLAUDE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_MIN_CLAUDE_VERSION = (2, 1, 110)


def _parse_claude_version(text: str) -> Optional[Tuple[int, int, int]]:
    m = _CLAUDE_VERSION_RE.search(text)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def check_claude() -> List[CheckItem]:
    """claude --version で Claude Code 版を取得し 2.1.110 と比較。

    PATH に claude が無ければ INFO(本ツールは Claude Code 必須ではない、
    Claude Code 経由 hook で使うときのみ要チェック)。
    """
    bin_path = shutil.which("claude")
    if not bin_path:
        return [CheckItem(
            section="claude", label="version",
            status=STATUS_INFO,
            detail="claude command not found in PATH",
            hint="Claude Code を入れている場合は PATH を確認してください "
                 "(本ツールは Claude Code 経由 hook 専用ではないので INFO)",
        )]

    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return [CheckItem(
            section="claude", label="version",
            status=STATUS_WARN,
            detail=f"claude --version failed: {e}",
        )]

    raw = (r.stdout or r.stderr or "").strip()
    if r.returncode != 0:
        return [CheckItem(
            section="claude", label="version",
            status=STATUS_WARN,
            detail=f"claude --version exit {r.returncode}: {raw[:200]}",
        )]

    parsed = _parse_claude_version(raw)
    if not parsed:
        return [CheckItem(
            section="claude", label="version",
            status=STATUS_INFO,
            detail=f"version unparseable: {raw[:80]!r}",
        )]

    if parsed < _MIN_CLAUDE_VERSION:
        return [CheckItem(
            section="claude", label="version",
            status=STATUS_WARN,
            detail=(f"{'.'.join(map(str, parsed))} "
                    f"< {'.'.join(map(str, _MIN_CLAUDE_VERSION))}"),
            hint="async hook unsupported, upgrade recommended "
                 "(Claude Code 2.1.110+)",
        )]

    return [CheckItem(
        section="claude", label="version",
        status=STATUS_OK,
        detail=f"{'.'.join(map(str, parsed))}  ({bin_path})",
    )]


def claude_section_skipped() -> List[CheckItem]:
    return [CheckItem(
        section="claude", label="skipped",
        status=STATUS_INFO,
        detail="--offline (subprocess checks skipped)",
    )]


# ---------------------------------------------------------------------------
# section: hooks
# ---------------------------------------------------------------------------


def _hook_settings_paths(cwd: Path) -> List[Tuple[str, Path]]:
    """3 階層の Claude Code settings.json パスを (label, path) で返す"""
    return [
        ("project-local", cwd / ".claude" / "settings.local.json"),
        ("project-shared", cwd / ".claude" / "settings.json"),
        ("user", Path.home() / ".claude" / "settings.json"),
    ]


def check_hooks(cwd: Optional[Path] = None,
                repo_root: Optional[Path] = None) -> List[CheckItem]:
    """3 階層の settings.json を見て vvread Stop hook 登録を確認"""
    if cwd is None:
        cwd = Path.cwd()
    items: List[CheckItem] = []

    found_layers: List[str] = []

    for label, path in _hook_settings_paths(cwd):
        if not path.exists():
            items.append(CheckItem(
                section="hooks", label=f"{label}",
                status=STATUS_INFO,
                detail=f"no file: {path}",
            ))
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            items.append(CheckItem(
                section="hooks", label=f"{label}",
                status=STATUS_WARN,
                detail=f"unreadable: {path}: {e}",
            ))
            continue

        if not isinstance(data, dict):
            items.append(CheckItem(
                section="hooks", label=f"{label}",
                status=STATUS_WARN,
                detail=f"top-level not object: {path}",
            ))
            continue

        hooks = data.get("hooks", {})
        stop_blocks = hooks.get("Stop", []) if isinstance(hooks, dict) else []

        matched_in_this_layer = False
        for block in stop_blocks if isinstance(stop_blocks, list) else []:
            if not isinstance(block, dict):
                continue
            for h in block.get("hooks", []) or []:
                if not isinstance(h, dict):
                    continue
                cmd = h.get("command", "")
                if not is_voiceclaude_hook(cmd, repo_root):
                    continue

                matched_in_this_layer = True
                # timeout 確認
                timeout = h.get("timeout")
                # async 確認
                async_flag = h.get("async", False)

                detail_parts = [path.name]
                if isinstance(timeout, (int, float)):
                    detail_parts.append(f"timeout={timeout}s")
                else:
                    detail_parts.append("timeout=<default>")
                detail_parts.append(f"async={'true' if async_flag else 'false'}")
                # command を最大 80 字で切り詰めて表示
                short_cmd = cmd if len(cmd) <= 80 else cmd[:77] + "..."

                # status 判定
                hints = []
                status = STATUS_OK
                if not async_flag:
                    status = STATUS_WARN
                    hints.append("async: true を付与してください "
                                 "(Claude Code 2.1.110+)")
                if isinstance(timeout, (int, float)) and timeout < 300:
                    status = STATUS_WARN if status != STATUS_ERROR else status
                    hints.append(
                        f"timeout が {timeout}s と短い "
                        "(長文応答の音切れ回避に 600s 推奨)"
                    )

                items.append(CheckItem(
                    section="hooks", label=f"{label}",
                    status=status,
                    detail=f"{' / '.join(detail_parts)}  cmd={short_cmd}",
                    hint=" / ".join(hints) if hints else None,
                ))

        if matched_in_this_layer:
            found_layers.append(label)
        else:
            items.append(CheckItem(
                section="hooks", label=f"{label}",
                status=STATUS_INFO,
                detail=f"no vvread Stop hook in {path.name}",
            ))

    # 重複登録チェック(複数階層に登録されていれば二重発話の恐れ)
    if len(found_layers) > 1:
        items.append(CheckItem(
            section="hooks", label="duplicate",
            status=STATUS_WARN,
            detail=f"vvread Stop hook が複数階層に登録: "
                   f"{', '.join(found_layers)}",
            hint="重複は二重発話の原因。どちらか一方を削除してください",
        ))

    if not found_layers:
        items.append(CheckItem(
            section="hooks", label="status",
            status=STATUS_INFO,
            detail="vvread Stop hook 未登録",
            hint="`vvread install` で登録できます",
        ))

    return items


# ---------------------------------------------------------------------------
# section: vvread
# ---------------------------------------------------------------------------


def check_vvread() -> List[CheckItem]:
    """bin/vvread が PATH にあるか + symlink 解決を確認"""
    bin_path = shutil.which("vvread")
    if not bin_path:
        return [CheckItem(
            section="vvread", label="path",
            status=STATUS_INFO,
            detail="vvread command not found in PATH",
            hint="`ln -s <repo>/bin/vvread ~/.local/bin/vvread` で PATH に通すと"
                 " hook 登録の絶対パスを短縮できます",
        )]
    # 実体のパス
    real = Path(bin_path).resolve()
    same = "" if str(real) == bin_path else f"  → {real}"
    return [CheckItem(
        section="vvread", label="path",
        status=STATUS_OK,
        detail=f"{bin_path}{same}",
    )]


# ---------------------------------------------------------------------------
# section: mcp
# ---------------------------------------------------------------------------


def check_mcp(project_dir: Optional[Path] = None) -> List[CheckItem]:
    """mcp package の有無と利用可能な Python を確認する。

    通常の doctor では INFO のみ（`vvread mcp` を使わない場合は無問題）。
    Python 解決順は mcp.sh と同じ:
      1. VVREAD_MCP_PYTHON 環境変数
      2. repo/.venv/bin/python
      3. python3
    """
    venv_python = None
    if project_dir:
        venv_python = project_dir / ".venv" / "bin" / "python"
    elif "VVREAD_PROJECT_DIR" in os.environ:
        venv_python = Path(os.environ["VVREAD_PROJECT_DIR"]) / ".venv" / "bin" / "python"

    candidates: List[Tuple[str, str]] = []  # (label, path)
    if "VVREAD_MCP_PYTHON" in os.environ:
        candidates.append(("VVREAD_MCP_PYTHON", os.environ["VVREAD_MCP_PYTHON"]))
    if venv_python:
        candidates.append((".venv/bin/python", str(venv_python)))
    py3 = shutil.which("python3")
    if py3:
        candidates.append(("python3", py3))

    for label, py_path in candidates:
        if not Path(py_path).exists():
            continue
        try:
            result = subprocess.run(
                [py_path, "-c",
                 "from importlib.metadata import version; print(version('mcp'))"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                return [CheckItem(
                    section="mcp",
                    label="package",
                    status=STATUS_OK,
                    detail=f"mcp {ver} ({label}: {py_path})",
                )]
        except Exception:
            pass

    return [CheckItem(
        section="mcp",
        label="package",
        status=STATUS_INFO,
        detail="mcp package not found (optional: required only for `vvread mcp`)",
        hint="Install: uv sync --extra mcp  (Python >=3.10 required)",
    )]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


_STATUS_TAG = {
    STATUS_OK: "OK   ",
    STATUS_INFO: "INFO ",
    STATUS_WARN: "WARN ",
    STATUS_ERROR: "ERROR",
}


def _emit_text(items: List[CheckItem], stream=sys.stdout) -> None:
    """plain text 出力。color は使わない(piped 利用に優しい)。"""
    current_section: Optional[str] = None
    for item in items:
        if item.section != current_section:
            print(f"\n[{item.section}]", file=stream)
            current_section = item.section
        tag = _STATUS_TAG.get(item.status, item.status)
        line = f"  {tag}  {item.label}"
        if item.detail:
            line += f": {item.detail}"
        print(line, file=stream)
        if item.hint:
            print(f"          → {item.hint}", file=stream)


def _emit_json(items: List[CheckItem], stream=sys.stdout) -> None:
    payload = {
        "items": [asdict(i) for i in items],
        "summary": _summarize(items),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def _summarize(items: List[CheckItem]) -> Dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for i in items:
        counts[i.status] = counts.get(i.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# section: queue（F-114 wedge / stale mutate 診断）
# ---------------------------------------------------------------------------


def _queue_env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default
    return v if v >= 1 else default


def _queue_read_owner(lockdir: Path) -> Optional[Tuple[str, str]]:
    """canonical owner（pid<TAB>token）/ legacy pid を読む。なければ None。"""
    owner = lockdir / "owner"
    try:
        if owner.is_file():
            line = owner.read_text(errors="replace").strip("\n")
            pid, tok = (line.split("\t", 1) + [""])[:2] if "\t" in line else (line, "")
            return (pid, tok) if pid else None
        pidf = lockdir / "pid"
        if pidf.is_file():
            pid = pidf.read_text(errors="replace").strip()
            return (pid, "") if pid else None
    except OSError:
        return None
    return None


def _queue_pid_alive(pid: str) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def _queue_lock_age(lockdir: Path, name: str) -> Optional[int]:
    f = lockdir / name
    try:
        if not f.is_file():
            return None
        v = int(f.read_text().strip())
    except (ValueError, OSError):
        return None
    return max(0, int(time.time()) - v)


def check_queue() -> List[CheckItem]:
    """queue.lock の wedge / busy / ok 分類 + stale mutate.lock を診断する。"""
    items: List[CheckItem] = []
    try:
        qdir = _paths.state_dir() / "queue"
    except Exception:  # noqa: BLE001
        return items
    if not qdir.is_dir():
        return items  # queue 未使用なら何も出さない

    def _count(sub: str) -> int:
        d = qdir / sub
        if not d.is_dir():
            return 0
        return sum(1 for f in d.iterdir()
                   if f.is_file() and not f.is_symlink() and ".tmp." not in f.name)

    pending = _count("pending")
    playing = _count("playing")
    drain_stale = _queue_env_int("VVREAD_QUEUE_DRAIN_STALE_S", 150)
    mutate_stale = _queue_env_int("VVREAD_QUEUE_MUTATE_STALE_S", 15)

    qlock = qdir / "queue.lock"
    owner = _queue_read_owner(qlock)
    drainer_alive = owner is not None and _queue_pid_alive(owner[0])

    if not drainer_alive:
        items.append(CheckItem(
            section="queue", label="drainer", status=STATUS_OK,
            detail=f"no live drainer (pending={pending}, playing={playing})",
        ))
    else:
        prog_age = _queue_lock_age(qlock, "progress")
        if pending == 0 or (prog_age is not None and prog_age <= drain_stale):
            items.append(CheckItem(
                section="queue", label="drainer", status=STATUS_OK,
                detail=f"ok (pid={owner[0]}, pending={pending}, playing={playing})",
            ))
        else:
            hb_age = _queue_lock_age(qlock, "hb")
            if hb_age is not None and hb_age > drain_stale:
                items.append(CheckItem(
                    section="queue", label="drainer", status=STATUS_WARN,
                    detail=(f"wedged (pid={owner[0]}, pending={pending}, "
                            f"no progress > {drain_stale}s)"),
                    hint="Run `vvread queue reset` to force recovery",
                ))
            else:
                items.append(CheckItem(
                    section="queue", label="drainer", status=STATUS_INFO,
                    detail=(f"busy — active playback/synthesis "
                            f"(pid={owner[0]}, pending={pending})"),
                ))

    # stale mutate.lock（独立 WARN）
    mlock = qdir / "queue.mutate.lock"
    mowner = _queue_read_owner(mlock)
    if mowner is not None:
        mage = _queue_lock_age(mlock, "hb")
        if (not _queue_pid_alive(mowner[0])) or (mage is not None and mage > mutate_stale):
            items.append(CheckItem(
                section="queue", label="mutate.lock", status=STATUS_WARN,
                detail=f"stale (pid={mowner[0]})",
                hint="Run `vvread queue reset` if playback is stuck",
            ))
    return items


def collect(*, offline: bool = False, scope: str = "runtime",
            cwd: Optional[Path] = None) -> List[CheckItem]:
    """全セクションを集める。テストから直接呼べるよう main から分離。"""
    if cwd is None:
        cwd = Path.cwd()

    items: List[CheckItem] = []
    items.extend(check_paths())

    s = _settings.load()
    items.extend(check_settings(s))
    # voiceClaude プロジェクトルート(SCRIPT_DIR の親)を渡して、
    # install_hint の相対パスを絶対パスに変換 (R-009 別プロジェクト実行対応)
    project_dir = SCRIPT_DIR.parent
    items.extend(check_dependencies(scope=scope, project_dir=project_dir))
    items.extend(check_player())

    if offline:
        items.extend(engine_section_skipped())
        items.extend(claude_section_skipped())
    else:
        items.extend(check_engines_multi(settings_obj=s))
        items.extend(check_claude())

    items.extend(check_hooks(cwd=cwd))
    items.extend(check_vvread())
    items.extend(check_mcp(project_dir=project_dir))
    items.extend(check_queue())
    return items


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vvread doctor: 環境診断"
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="VOICEVOX HTTP / claude --version 等のネットワーク・"
             "外部プロセス依存のチェックをスキップ",
    )
    parser.add_argument(
        "--scope",
        choices=("runtime", "all"),
        default="runtime",
        help="dependencies の表示範囲(runtime=default, all=setup/dev/publish 含む)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="構造化 JSON で出力(CI / 機械処理向け)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="(将来用 placeholder、v0.1 では未実装。"
             "WARNING でも exit 1 にする予定)",
    )
    args = parser.parse_args()

    items = collect(offline=args.offline, scope=args.scope)

    if args.json:
        _emit_json(items)
    else:
        _emit_text(items)
        # plain text は最後に summary を 1 行
        summary = _summarize(items)
        print(
            f"\nsummary: {summary[STATUS_OK]} OK / {summary[STATUS_INFO]} INFO"
            f" / {summary[STATUS_WARN]} WARN / {summary[STATUS_ERROR]} ERROR",
        )

    # exit code 仕様 (R-009):
    # ERROR があれば 1、warning のみは 0
    if any(i.status == STATUS_ERROR for i in items):
        return 1
    # --strict は v0.1 未実装(receive はするが exit code に影響させない、
    # ユーザ仕様: 「初期実装では --strict は未実装でも構いません」)
    return 0


if __name__ == "__main__":
    sys.exit(main())
