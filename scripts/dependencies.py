#!/usr/bin/env python3
"""scripts/dependencies.py - vvread の依存カタログ (R-024)

依存(必須/任意)を Python データ構造として定義し、R-009 doctor が import して
checking + 表示できるようにする。doc/10-dependencies.md は本カタログを人間向け
に書き下したミラーで、内容は本ファイル(コード = 真実の唯一の源)に合わせる。

カテゴリ:
  runtime  : `vvread say` 等の通常実行に必要(`vvread` が動く前提)
  setup    : `vvread setup` / `vvread install` の補助。なくても fallback 経路あり
  dev      : 本リポでの開発(lint/test)に必要
  publish  : 配布リポへの mirror push / リリース運用で使う

kind:
  required : このカテゴリで必須(無いと当該機能が動かない)
  optional : 推奨だが代替手段あり

R-024 では catalog + CLI 提供のみで、doctor 本体での check は R-009 で実装。
本カタログは「依存仕様の単一真実の源」で、README / doc / setup / install /
doctor / publish のすべての文脈から参照される。

CLI:
  python dependencies.py list [--kind ...] [--category ...] [--json]
  python dependencies.py check <name>     # PATH 上の存在 + version 取得
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# 値域 enum(constants として宣言、test で固定)
# ---------------------------------------------------------------------------

KINDS = ("required", "optional")
CATEGORIES = ("runtime", "setup", "dev", "publish")


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------


@dataclass
class Dependency:
    """1 つの依存ツール / ライブラリの仕様。

    name           : 実際に PATH で探すコマンド名 / pip パッケージ名
    kind           : "required" / "optional"
    category       : "runtime" / "setup" / "dev" / "publish"
    purpose        : 用途を 1 行で(doctor 表示 / README 表で使う)
    check_command  : `--version` 系の引数列。PATH 検出後 invoke して動作確認
                     も兼ねる。空 list なら shutil.which のみで判定
    fallback       : optional の場合の代替挙動説明。required は None
    install_hint   : OS 別インストールコマンド。{"macos": "...", "linux": "...",
                     "pip": "..."} 等のキー
    notes          : 追加メモ(doctor 表示時に warning として出すこともある)
    """
    name: str
    kind: str
    category: str
    purpose: str
    check_command: List[str] = field(default_factory=list)
    fallback: Optional[str] = None
    install_hint: Dict[str, str] = field(default_factory=dict)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# カタログ本体
# ---------------------------------------------------------------------------


DEPENDENCIES: List[Dependency] = [
    # ---- runtime: required ----
    Dependency(
        name="bash",
        kind="required",
        category="runtime",
        purpose="bin/vvread および scripts/*.sh のシェル実行(Bash 3.2 互換)",
        check_command=["bash", "--version"],
        install_hint={
            "macos": "システム同梱(macOS は 3.2 のまま、newer brew install bash も可)",
            "linux": "ディストリ標準で同梱(apt install bash 等は通常不要)",
        },
    ),
    Dependency(
        name="python3",
        kind="required",
        category="runtime",
        purpose="sanitize / chunk_split / parse_transcript / settings 等の Python helper を実行",
        check_command=["python3", "--version"],
        install_hint={
            "macos": "brew install python@3.12 (システム同梱の 3.x でも 3.10 以上なら可)",
            "linux": "apt install python3 / dnf install python3",
        },
        notes="最低 Python 3.10 を要求(Backlog 確定事項)。",
    ),
    Dependency(
        name="curl",
        kind="required",
        category="runtime",
        purpose="VOICEVOX Engine HTTP API 呼び出し / health check",
        check_command=["curl", "--version"],
        install_hint={
            "macos": "システム同梱",
            "linux": "apt install curl / dnf install curl",
        },
    ),
    # ---- runtime: optional ----
    Dependency(
        name="afplay",
        kind="optional",
        category="runtime",
        purpose="macOS 標準の wav プレイヤー(lib_playback.sh の macOS 経路)",
        check_command=[],  # 存在判定のみ(macOS は --version 不採用)
        fallback="VVREAD_PLAYER で代替プレイヤーを明示指定可",
        install_hint={
            "macos": "システム同梱",
            "linux": "afplay は macOS 専用(代わりに paplay/aplay/play/ffplay を使用)",
        },
        notes="Linux/WSL では存在しない。Linux は paplay > pw-play > aplay > play > ffplay の順で自動検出。",
    ),
    Dependency(
        name="paplay",
        kind="optional",
        category="runtime",
        purpose="Linux/WSL の PulseAudio 系プレイヤー(lib_playback.sh の Linux 経路の最優先)",
        check_command=["paplay", "--version"],
        fallback="pw-play / aplay / play / ffplay のいずれかで代替",
        install_hint={
            "linux": "apt install pulseaudio-utils / dnf install pulseaudio-utils",
        },
    ),
    Dependency(
        name="terminal-notifier",
        kind="optional",
        category="runtime",
        purpose="VOICEVOX 不通時のデスクトップ通知(lib_notify.sh が優先採用)",
        check_command=["terminal-notifier", "-help"],
        fallback="osascript display notification にフォールバック(macOS Sequoia 以降は通知許可周りで silent fail することがある)",
        install_hint={
            "macos": "brew install terminal-notifier",
        },
    ),
    Dependency(
        name="e2k",
        kind="optional",
        category="runtime",
        purpose="英単語の自動カタカナ化(sanitize.py が import して使用)",
        check_command=[
            "python3", "-c", "import e2k",
        ],
        fallback="kana_dict.py の WORD_KANA + 逐字フォールバックで動作(精度は落ちる)",
        install_hint={
            "uv": "uv pip install --python .venv/bin/python e2k",
        },
    ),
    Dependency(
        name="mcp",
        kind="optional",
        category="runtime",
        purpose="MCP サーバー機能 (vvread mcp サブコマンド)。Python >=3.10 が必要。"
                "通常の vvread say / Stop hook 利用には不要。",
        check_command=["python3", "-c", "import mcp"],
        fallback="`vvread mcp` が使用不可。CLI / Stop hook は引き続き利用可能。",
        install_hint={
            "uv": "uv sync --extra mcp  # Python >=3.10 required",
            "pip": "pip install 'mcp>=1,<2'",
        },
    ),
    Dependency(
        name="rumps",
        kind="optional",
        category="runtime",
        purpose="macOS メニューバー常駐 UI (vvread menubar サブコマンド, B-151)。"
                "通常の vvread say / Stop hook 利用には不要。macOS 専用機能。",
        check_command=["python3", "-c", "import rumps"],
        fallback="`vvread menubar` が使用不可。CLI / Stop hook 等の通常機能には影響しない。"
                 "macOS 以外ではそもそも対象外(pyproject の sys_platform=='darwin' marker "
                 "により uv sync でもインストールされない)。",
        install_hint={
            "macos": "uv sync  # pyproject.toml の sys_platform=='darwin' marker で"
                     "コア依存として自動解決(追加 --extra 不要)",
        },
        notes="macOS 専用(pyproject.toml でコア依存に `rumps>=0.4.0; sys_platform == "
              "'darwin'` として宣言)。Linux/WSL では doctor 上「対象外」表示になる。",
    ),
    # ---- setup: optional ----
    Dependency(
        name="jq",
        kind="optional",
        category="setup",
        purpose="vvread install で .claude/settings.json への hook 登録(JSON merge 補助)",
        check_command=["jq", "--version"],
        fallback="Python `json` モジュールでマージ(将来実装予定)",
        install_hint={
            "macos": "brew install jq",
            "linux": "apt install jq / dnf install jq",
        },
    ),
    Dependency(
        name="docker",
        kind="optional",
        category="setup",
        purpose="`vvread setup --engine docker` で VOICEVOX Engine をコンテナ起動",
        check_command=["docker", "--version"],
        fallback="`--engine existing` でユーザが別途起動した VOICEVOX に接続(default 推奨)",
        install_hint={
            "macos": "Docker Desktop または OrbStack",
            "linux": "apt install docker.io / dnf install docker-ce(distro 公式手順を推奨)",
        },
    ),
    Dependency(
        name="uv",
        kind="optional",
        category="setup",
        purpose="venv 作成と pip install の高速化(setup の e2k 導入を短縮)",
        check_command=["uv", "--version"],
        fallback="`python3 -m venv` + `.venv/bin/pip install` で従来手順",
        install_hint={
            "macos": "brew install uv",
            "linux": "curl -LsSf https://astral.sh/uv/install.sh | sh",
        },
    ),
    # ---- dev: optional ----
    Dependency(
        name="shellcheck",
        kind="optional",
        category="dev",
        purpose="bash スクリプトの静的解析(scripts/dev/lint.sh + CI で使用)",
        check_command=["shellcheck", "--version"],
        fallback="lint.sh が exit 2 で停止(CI 必須、ローカル開発でも推奨)",
        install_hint={
            "macos": "brew install shellcheck",
            "linux": "apt install shellcheck / dnf install ShellCheck",
        },
    ),
    Dependency(
        name="ruff",
        kind="optional",
        category="dev",
        purpose="Python リンター(CI で使用、ローカル開発でも有用)",
        check_command=["ruff", "--version"],
        fallback="無くても tests/ は通るが、format/style 揺れを CI で検出できない",
        install_hint={
            "pip": "pip install ruff",
            "uv": "uv pip install ruff",
            "macos": "brew install ruff",
        },
    ),
    Dependency(
        name="pytest",
        kind="optional",
        category="dev",
        purpose="テストランナー(`tests/test_*.py` 全件実行)",
        check_command=["pytest", "--version"],
        fallback="無いと tests/ が走らない(開発上は必須に近い、優先度的には任意)",
        install_hint={
            "pip": "pip install pytest",
            "uv": "uv pip install pytest",
        },
    ),
    # ---- publish: optional ----
    Dependency(
        name="git",
        kind="optional",
        category="publish",
        purpose="公開リポへの mirror push / sync ブランチ作成(publish.sh)",
        check_command=["git", "--version"],
        fallback="無いと publish.sh が動かない(配布チャネル運用時のみ必要)",
        install_hint={
            "macos": "brew install git(Xcode CLT 同梱の git でも可)",
            "linux": "apt install git / dnf install git",
        },
    ),
    Dependency(
        name="gh",
        kind="optional",
        category="publish",
        purpose="GitHub CLI(publish.sh が `gh pr create` で sync ブランチから PR 作成)",
        check_command=["gh", "--version"],
        fallback="手動で GitHub Web から PR 作成(publish.sh の自動化が落ちる)",
        install_hint={
            "macos": "brew install gh",
            "linux": "公式 https://cli.github.com/manual/installation",
        },
    ),
    Dependency(
        name="gitleaks",
        kind="optional",
        category="publish",
        purpose="publish.sh 前の secret scan(publish.sh 実行前に必須)",
        check_command=["gitleaks", "version"],
        fallback="無いと secret 漏れの自動検出ができない(publish.sh は abort、手動レビュー必須)",
        install_hint={
            "macos": "brew install gitleaks",
            "linux": "https://github.com/gitleaks/gitleaks/releases から binary、または go install",
        },
    ),
]


# ---------------------------------------------------------------------------
# accessor
# ---------------------------------------------------------------------------


def by_name(name: str) -> Optional[Dependency]:
    for dep in DEPENDENCIES:
        if dep.name == name:
            return dep
    return None


def filter_deps(
    *,
    kind: Optional[str] = None,
    category: Optional[str] = None,
) -> List[Dependency]:
    result = list(DEPENDENCIES)
    if kind is not None:
        result = [d for d in result if d.kind == kind]
    if category is not None:
        result = [d for d in result if d.category == category]
    return result


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    name: str
    found: bool
    path: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None  # check_command 実行で非 0 / 失敗した場合の理由


def check(dep: Dependency) -> CheckResult:
    """PATH 上での存在確認 + check_command による version 取得。

    check_command が空(version 取得不可な OS 同梱コマンド = afplay 等)の場合、
    `shutil.which(name)` のみで判定し subprocess は呼ばない。

    check_command が `python3 -c "import e2k"` のような import 検査の場合、
    先頭(python3)を which で確認 → 見つかれば import を試行 → return code 0
    を成功とする。
    """
    # check_command が空 → name 自体を which するだけで判定終了
    if not dep.check_command:
        path = shutil.which(dep.name)
        return CheckResult(
            name=dep.name,
            found=path is not None,
            path=path,
        )

    # check_command 経路: 先頭バイナリの存在確認 → 実行 → return code で判定
    binary = dep.check_command[0]
    found_path = shutil.which(binary)
    if not found_path:
        return CheckResult(name=dep.name, found=False)

    # T-015(b): timeout を 1s に短縮(--version 系は通常 100ms 以下)
    try:
        proc = subprocess.run(
            dep.check_command,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return CheckResult(
            name=dep.name,
            found=True,
            path=found_path,
            error=f"check command failed: {e}",
        )

    if proc.returncode != 0:
        return CheckResult(
            name=dep.name,
            found=False,
            path=found_path,
            error=f"exit {proc.returncode}: {proc.stderr.strip()[:200]}",
        )

    # version 抽出: stdout / stderr の最初の非空行を取る
    raw = proc.stdout or proc.stderr or ""
    version = ""
    for line in raw.splitlines():
        if line.strip():
            version = line.strip()
            break
    return CheckResult(
        name=dep.name,
        found=True,
        path=found_path,
        version=version or None,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_list(args: argparse.Namespace) -> int:
    deps = filter_deps(kind=args.kind, category=args.category)
    if args.json:
        payload = [asdict(d) for d in deps]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    # plain text
    if not deps:
        return 0
    for d in deps:
        flag = "REQ" if d.kind == "required" else "opt"
        print(f"[{flag}/{d.category}] {d.name} - {d.purpose}")
        if d.fallback:
            print(f"    fallback: {d.fallback}")
        if d.install_hint:
            for os_key, hint in d.install_hint.items():
                print(f"    install({os_key}): {hint}")
        if d.notes:
            print(f"    note: {d.notes}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    dep = by_name(args.name)
    if dep is None:
        print(f"dependencies: unknown name: {args.name}", file=sys.stderr)
        return 1
    result = check(dep)
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False))
    else:
        if result.found:
            label = "OK"
            extra = []
            if result.path:
                extra.append(result.path)
            if result.version:
                extra.append(result.version)
            tail = f" ({' / '.join(extra)})" if extra else ""
            print(f"{label}: {dep.name}{tail}")
        else:
            print(f"MISSING: {dep.name}")
            if result.error:
                print(f"  reason: {result.error}", file=sys.stderr)
    return 0 if result.found else 2  # 2 = missing(doctor で warning 化)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="vvread dependency catalog"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list dependencies (filter optional)")
    p_list.add_argument("--kind", choices=KINDS, help="filter by kind")
    p_list.add_argument(
        "--category",
        choices=CATEGORIES,
        help="filter by category",
    )
    p_list.add_argument(
        "--json",
        action="store_true",
        help="emit as JSON list",
    )
    p_list.set_defaults(func=_cmd_list)

    p_check = sub.add_parser(
        "check",
        help="check a single dependency by name (PATH + version)",
    )
    p_check.add_argument("name", help="dependency name (e.g. jq)")
    p_check.add_argument(
        "--json",
        action="store_true",
        help="emit CheckResult as JSON",
    )
    p_check.set_defaults(func=_cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
