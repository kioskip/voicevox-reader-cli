"""OS 別パス resolver (R-001)

state / log / cache の 3 種類のディレクトリを OS 別の既定値で返す。
副作用なし(mkdir はしない、呼び出し側責務)。

優先順位: VVREAD_*_DIR 環境変数 > OS 既定値

- macOS (Darwin):
    state: ~/Library/Application Support/vvread/
    log:   ~/Library/Logs/vvread/
    cache: ~/Library/Caches/vvread/
- Linux / WSL / Git Bash 等:
    state: ${XDG_STATE_HOME:-~/.local/state}/vvread/
    log:   ${XDG_STATE_HOME:-~/.local/state}/vvread/logs/
    cache: ${XDG_CACHE_HOME:-~/.cache}/vvread/

bash 側 lib_paths.sh と完全一致した出力を返す(tests/test_paths.py で固定)。

CLI usage:
    python paths.py {state|log|cache}
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

APP_NAME = "vvread"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _expand(path: str) -> Path:
    # `~` のみ展開する(`$VAR` は意図的に扱わない、bash 側 _vvread_expand と整合)。
    # 末尾スラッシュは Path() が自動正規化する。
    return Path(os.path.expanduser(path))


def _xdg_or_default(env_var: str, default_unexpanded: str) -> Path:
    val = os.environ.get(env_var, "")
    if val:
        return _expand(val)
    return _expand(default_unexpanded)


def state_dir() -> Path:
    override = os.environ.get("VVREAD_STATE_DIR", "")
    if override:
        return _expand(override)
    if _is_macos():
        return _expand("~/Library/Application Support") / APP_NAME
    return _xdg_or_default("XDG_STATE_HOME", "~/.local/state") / APP_NAME


def log_dir() -> Path:
    override = os.environ.get("VVREAD_LOG_DIR", "")
    if override:
        return _expand(override)
    if _is_macos():
        return _expand("~/Library/Logs") / APP_NAME
    return _xdg_or_default("XDG_STATE_HOME", "~/.local/state") / APP_NAME / "logs"


def cache_dir() -> Path:
    override = os.environ.get("VVREAD_CACHE_DIR", "")
    if override:
        return _expand(override)
    if _is_macos():
        return _expand("~/Library/Caches") / APP_NAME
    return _xdg_or_default("XDG_CACHE_HOME", "~/.cache") / APP_NAME


_DISPATCH = {
    "state": state_dir,
    "log": log_dir,
    "cache": cache_dir,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in _DISPATCH:
        print("usage: paths.py {state|log|cache}", file=sys.stderr)
        return 2
    print(_DISPATCH[argv[1]]())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
