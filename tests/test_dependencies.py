"""scripts/dependencies.py のテスト (R-024)

dependency catalog の整合性 + check + CLI を検証。
catalog 自体が「依存仕様の単一真実の源」(R-009 doctor / README / publish.sh
が consume する)なので、name 重複 / kind/category enum 範囲 / 必須要素の
存在を test で固定し、追加変更時の事故を防ぐ。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import dependencies as deps_module  # noqa: E402

SCRIPT = REPO / "scripts" / "dependencies.py"


# ---------------------------------------------------------------------------
# catalog 整合性
# ---------------------------------------------------------------------------


class TestCatalogIntegrity:
    def test_no_duplicate_names(self):
        names = [d.name for d in deps_module.DEPENDENCIES]
        assert len(names) == len(set(names)), \
            f"重複あり: {[n for n in names if names.count(n) > 1]}"

    def test_kind_within_enum(self):
        for d in deps_module.DEPENDENCIES:
            assert d.kind in deps_module.KINDS, \
                f"{d.name}: invalid kind {d.kind!r}"

    def test_category_within_enum(self):
        for d in deps_module.DEPENDENCIES:
            assert d.category in deps_module.CATEGORIES, \
                f"{d.name}: invalid category {d.category!r}"

    def test_required_only_in_runtime(self):
        """v0.1: required は runtime カテゴリ専用。setup/dev/publish に
        required を増やしたくなったら一度 backlog で議論する。"""
        for d in deps_module.DEPENDENCIES:
            if d.kind == "required":
                assert d.category == "runtime", (
                    f"{d.name} is required but category={d.category!r}: "
                    f"runtime 以外で required は backlog 議論が必要"
                )

    def test_required_has_install_hint(self):
        """required は何らかの install 手順がドキュメント化されているべき"""
        for d in deps_module.DEPENDENCIES:
            if d.kind == "required":
                assert d.install_hint, \
                    f"{d.name}: required なのに install_hint が空"

    def test_optional_has_fallback_or_explicit_no(self):
        """optional は fallback 必須(代替手段が無いなら required にすべき)"""
        for d in deps_module.DEPENDENCIES:
            if d.kind == "optional":
                assert d.fallback is not None and d.fallback != "", (
                    f"{d.name}: optional だが fallback 未記載"
                )

    def test_purpose_not_empty(self):
        for d in deps_module.DEPENDENCIES:
            assert d.purpose, f"{d.name}: purpose が空"

    def test_required_runtime_set_matches_user_spec(self):
        """ユーザ R-024 スコープ確定: required runtime = bash / python3 / curl"""
        required_runtime = {
            d.name for d in deps_module.DEPENDENCIES
            if d.kind == "required" and d.category == "runtime"
        }
        assert required_runtime == {"bash", "python3", "curl"}, (
            f"required runtime mismatch: {required_runtime}"
        )

    def test_known_optional_categories_present(self):
        """ユーザ指定の optional カテゴリ / 主要ツールが揃っている"""
        names = {d.name for d in deps_module.DEPENDENCIES}
        # setup
        assert "jq" in names
        assert "docker" in names
        assert "uv" in names
        # runtime optional
        assert "e2k" in names
        # dev
        assert "shellcheck" in names
        assert "ruff" in names
        assert "pytest" in names
        # publish
        assert "gitleaks" in names
        assert "gh" in names


# ---------------------------------------------------------------------------
# accessor
# ---------------------------------------------------------------------------


class TestAccessors:
    def test_by_name_hit(self):
        d = deps_module.by_name("curl")
        assert d is not None
        assert d.kind == "required"
        assert d.category == "runtime"

    def test_by_name_miss(self):
        assert deps_module.by_name("definitely_not_a_dep") is None

    def test_filter_kind(self):
        deps = deps_module.filter_deps(kind="required")
        assert all(d.kind == "required" for d in deps)
        assert deps  # 必ず 1 件以上

    def test_filter_category(self):
        deps = deps_module.filter_deps(category="dev")
        assert all(d.category == "dev" for d in deps)
        names = {d.name for d in deps}
        assert "shellcheck" in names

    def test_filter_kind_and_category(self):
        deps = deps_module.filter_deps(kind="required", category="runtime")
        assert {d.name for d in deps} == {"bash", "python3", "curl"}


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


class TestCheck:
    def test_check_existing_command(self):
        # python3 は CI も local も存在するので安定
        d = deps_module.by_name("python3")
        result = deps_module.check(d)
        assert result.found is True
        assert result.path is not None
        assert result.version is not None
        # version 文字列に "Python" が含まれる
        assert "Python" in result.version

    def test_check_missing_command(self, tmp_path, monkeypatch):
        """PATH を空にして missing を再現"""
        monkeypatch.setenv("PATH", str(tmp_path))
        # bash も python3 も無い PATH(tmp 空)に絞る
        d = deps_module.by_name("python3")
        result = deps_module.check(d)
        assert result.found is False

    def test_check_empty_check_command_uses_which_only(self):
        """check_command=[] のとき which のみで判定、subprocess は呼ばない
        (afplay 等の OS 同梱で --version 持たないコマンド向け)。
        R-009 doctor で IndexError として発覚したバグの回帰テスト。"""
        from dependencies import Dependency, check
        fake = Dependency(
            name="bash",  # 実在するコマンドで which 成功 + check_command 空
            kind="optional",
            category="runtime",
            purpose="test",
            check_command=[],
            fallback="(test)",
        )
        result = check(fake)
        assert result.found is True
        assert result.path is not None
        # check_command 空なので version は取得しない
        assert result.version is None

    def test_check_empty_command_missing_returns_not_found(self, tmp_path, monkeypatch):
        """check_command=[] + name が PATH にない → found=False"""
        from dependencies import Dependency, check
        monkeypatch.setenv("PATH", str(tmp_path))
        fake = Dependency(
            name="definitely_not_a_cmd_xyz",
            kind="optional",
            category="runtime",
            purpose="test",
            check_command=[],
            fallback="(test)",
        )
        result = check(fake)
        assert result.found is False
        assert result.path is None

    def test_check_e2k_module_via_python_c(self, monkeypatch):
        """check_command が `python3 -c 'import e2k'` 形式の場合、Python が
        PATH にあれば呼び出され、return code で found 判定される。

        e2k が venv 内にしか入っていないユーザ環境ではここは False になる
        ことが期待される。安定検証のため import 失敗するモジュール名で
        判定の論理を確認する。
        """
        # 直接 module を作って check() を呼ぶ
        from dependencies import Dependency, check
        fake = Dependency(
            name="not_a_module_xyz",
            kind="optional",
            category="runtime",
            purpose="test",
            check_command=["python3", "-c",
                           "import not_a_real_module_xyz_999"],
            fallback="(test)",
        )
        result = check(fake)
        # python3 自体は見つかるが import が失敗 → found=False + error あり
        assert result.found is False
        assert result.error is not None
        assert result.path is not None  # python3 自身は見つかっている


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(*args, env_extra=None):
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=base,
        capture_output=True,
        text=True,
    )


class TestCli:
    def test_list_default_includes_all(self):
        r = _run_cli("list")
        assert r.returncode == 0
        # 主要 deps が含まれる
        for name in ("bash", "python3", "curl", "jq", "docker", "shellcheck"):
            assert name in r.stdout

    def test_list_filter_kind_required(self):
        r = _run_cli("list", "--kind", "required")
        assert r.returncode == 0
        assert "bash" in r.stdout
        assert "python3" in r.stdout
        assert "curl" in r.stdout
        # optional は出ない
        assert "jq" not in r.stdout
        assert "shellcheck" not in r.stdout

    def test_list_filter_category_publish(self):
        r = _run_cli("list", "--category", "publish")
        assert r.returncode == 0
        assert "gitleaks" in r.stdout
        assert "gh" in r.stdout
        # runtime 系は出ない
        assert "curl" not in r.stdout

    def test_list_invalid_kind_rejected(self):
        r = _run_cli("list", "--kind", "bogus")
        assert r.returncode == 2  # argparse のエラーは 2
        assert "invalid choice" in r.stderr or "bogus" in r.stderr

    def test_list_json_outputs_array(self):
        r = _run_cli("list", "--kind", "required", "--json")
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert isinstance(payload, list)
        names = {d["name"] for d in payload}
        assert names == {"bash", "python3", "curl"}
        # フィールドが揃っている
        for d in payload:
            assert "kind" in d
            assert "category" in d
            assert "purpose" in d
            assert "check_command" in d
            assert "install_hint" in d

    def test_check_known(self):
        """python3 自身を check しても OK 行が出る"""
        r = _run_cli("check", "python3")
        assert r.returncode == 0
        assert r.stdout.startswith("OK:")

    def test_check_unknown_exits_1(self):
        r = _run_cli("check", "no_such_dep_xyz")
        assert r.returncode == 1
        assert "unknown name" in r.stderr

    def test_check_missing_exits_2(self, tmp_path):
        """PATH を空にして missing を再現 → exit 2"""
        env = {"PATH": str(tmp_path)}
        # python3 は使うので明示的に存在パス無しに
        r = _run_cli("check", "jq", env_extra=env)
        # jq 自身が PATH に無い tmp_path 経由なら missing
        # jq が brew 経由で /usr/local/bin にあっても tmp_path 単独 PATH では見えない
        if shutil.which("jq", path=str(tmp_path)):
            pytest.skip("tmp PATH にも jq があった(本テスト無効)")
        assert r.returncode == 2
        assert r.stdout.startswith("MISSING:")

    def test_check_json(self):
        r = _run_cli("check", "python3", "--json")
        assert r.returncode == 0
        result = json.loads(r.stdout)
        assert result["name"] == "python3"
        assert result["found"] is True
        assert result["path"] is not None
        assert result["version"] is not None
