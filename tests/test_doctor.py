"""scripts/doctor.py の単体テスト (R-009)

各セクション関数を Python module として直接 import して検証する。CLI / bin/
vvread 経由の統合テストは tests/test_cmd_doctor.py 側。

カバー範囲:
- check_paths: tmp_path 注入で 3 dir 状態
- check_settings: Settings 注入で values / unknown_keys / parse_errors
- check_dependencies: scope=runtime / all、PATH 操作で missing 再現
- check_player: bash subprocess の戻り値ベース
- check_engine: voicevox_mock + speaker 存在 / 不在
- engine_section_skipped / claude_section_skipped: --offline 用
- check_claude: claude バイナリ不在 / 古いバージョン / 新バージョン
- check_hooks: 3 階層 + 重複 + timeout / async warning
- check_vvread: PATH 解決
- collect: offline / scope の挙動
- main: exit code 仕様(error→1 / warn のみ→0)
"""
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import doctor as doctor_mod  # noqa: E402
import settings as settings_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------


def _statuses(items, section: str = None):
    if section is not None:
        items = [i for i in items if i.section == section]
    return [(i.label, i.status) for i in items]


def _by_label(items, label: str):
    for i in items:
        if i.label == label:
            return i
    return None


# ---------------------------------------------------------------------------
# check_paths
# ---------------------------------------------------------------------------


class TestCheckPaths:
    def test_existing_dirs_are_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "cache"))
        for sub in ("state", "log", "cache"):
            # R-121: 0700 で明示作成(既定の mkdir() は umask 依存で 0755 等に
            # なり得るため、新設の perm-WARN 判定を誤って踏まないようにする)。
            (tmp_path / sub).mkdir(mode=0o700)
        items = doctor_mod.check_paths()
        labels = {(i.label, i.status) for i in items}
        assert ("state", "OK") in labels
        assert ("log", "OK") in labels
        assert ("cache", "OK") in labels
        # 0700 なので他ユーザー可読の WARN は出ないはず
        assert not any(i.label.endswith("_perm") for i in items)

    def test_missing_dirs_are_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "no_state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "no_log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "no_cache"))
        items = doctor_mod.check_paths()
        for label in ("state", "log", "cache"):
            i = _by_label(items, label)
            assert i.status == "INFO"
            assert "will be created" in i.detail

    # -- R-121: 他ユーザー可読な既存ディレクトリの検出 -----------------------

    def test_world_readable_dir_is_warn(self, tmp_path, monkeypatch):
        """0755 相当(旧インストール)の state dir は {name}_perm WARN を出す"""
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "cache"))
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)
        # chmod は umask の影響を受けず厳密にビットを設定できる
        state_dir.chmod(0o755)
        (tmp_path / "log").mkdir(mode=0o700)
        (tmp_path / "cache").mkdir(mode=0o700)

        items = doctor_mod.check_paths()

        perm_items = {i.label: i for i in items if i.label.endswith("_perm")}
        assert "state_perm" in perm_items
        assert perm_items["state_perm"].status == "WARN"
        assert "mode=755" in perm_items["state_perm"].detail
        assert perm_items["state_perm"].hint is not None
        assert "chmod 700" in perm_items["state_perm"].hint
        # log / cache は 0700 のままなので perm WARN は出ない
        assert "log_perm" not in perm_items
        assert "cache_perm" not in perm_items

    def test_hint_quotes_path_with_spaces(self, tmp_path, monkeypatch):
        """パスにスペースが含まれる場合(macOS既定の "Application Support" 等)、
        hint はそのままシェルにコピペしても word-split されないよう
        shlex.quote で引用符が付与される。"""
        state_dir = tmp_path / "state dir with spaces"
        monkeypatch.setenv("VVREAD_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "cache"))
        state_dir.mkdir(mode=0o700, parents=True)
        state_dir.chmod(0o755)
        (tmp_path / "log").mkdir(mode=0o700)
        (tmp_path / "cache").mkdir(mode=0o700)

        items = doctor_mod.check_paths()

        perm_items = {i.label: i for i in items if i.label.endswith("_perm")}
        hint = perm_items["state_perm"].hint
        assert hint is not None
        expected_quoted = shlex.quote(str(state_dir))
        # 前提: このテストのパスは実際にスペースを含み、クォートが必要
        assert expected_quoted != str(state_dir)
        assert hint == f"chmod 700 {expected_quoted}"

    def test_0700_dir_has_no_perm_warn(self, tmp_path, monkeypatch):
        """0700 は既に他ユーザーアクセス不可なので perm WARN は出ない"""
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "cache"))
        for sub in ("state", "log", "cache"):
            (tmp_path / sub).mkdir(mode=0o700)

        items = doctor_mod.check_paths()

        assert not any(i.label.endswith("_perm") for i in items)

    def test_windows_skips_perm_check(self, tmp_path, monkeypatch):
        """Windows は POSIX permission bit の概念が薄いのでチェック自体を skip"""
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "cache"))
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)
        state_dir.chmod(0o755)  # 世界可読でも Windows なら WARN が出ないはず
        (tmp_path / "log").mkdir(mode=0o700)
        (tmp_path / "cache").mkdir(mode=0o700)
        monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Windows")

        items = doctor_mod.check_paths()

        assert not any(i.label.endswith("_perm") for i in items)

    def test_stat_failure_is_info_not_fatal(self, tmp_path, monkeypatch):
        """TOCTOU 等で stat() が失敗しても例外を飛ばさず STATUS_INFO にする

        Path.exists() / Path.is_dir() はどちらも内部で self.stat() を呼ぶため、
        単純に Path.stat を常時 raise にすると exists() が False を返してしまい
        perm チェック自体に到達しない(advisor 指摘の落とし穴)。ここでは対象
        ディレクトリへの stat() 呼び出しを 3 回目以降だけ失敗させることで、
        exists()/is_dir() (1, 2 回目) は成功させつつ、check_paths が明示的に
        呼ぶ stat() (3 回目) だけを失敗させる。
        """
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "no_log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "no_cache"))
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)

        orig_stat = Path.stat
        victim = str(state_dir)
        calls = {"n": 0}

        def fake_stat(self, *a, **kw):
            if str(self) == victim:
                calls["n"] += 1
                if calls["n"] <= 2:
                    return orig_stat(self, *a, **kw)
                raise PermissionError(13, "Permission denied")
            return orig_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", fake_stat)

        items = doctor_mod.check_paths()

        state_perm = _by_label(items, "state_perm")
        assert state_perm is not None
        assert state_perm.status == "INFO"
        assert "permission check failed" in state_perm.detail


# ---------------------------------------------------------------------------
# check_settings
# ---------------------------------------------------------------------------


class TestCheckSettings:
    def test_all_schema_keys_emitted_as_info(self, tmp_path):
        # 注入済みの Settings を作る(env 空、ファイル不在)
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_settings(s)
        # 全 schema キーが INFO 行として現れる
        emitted_labels = {i.label for i in items if i.status == "INFO"}
        for k in settings_mod.SCHEMA.keys():
            assert k in emitted_labels

    def test_unknown_keys_become_warn(self, tmp_path):
        # project に不明キーを書く
        proj = tmp_path / "vvread.settings.json"
        proj.write_text(json.dumps({"future": {"x": 1}}))
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=proj,
        )
        items = doctor_mod.check_settings(s)
        warns = [i for i in items if i.status == "WARN"
                 and i.label == "unknown_key"]
        assert warns
        assert any("future.x" in (i.detail or "") for i in warns)

    def test_parse_error_becomes_warn(self, tmp_path):
        proj = tmp_path / "vvread.settings.json"
        proj.write_text("{ not valid")
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=proj,
        )
        items = doctor_mod.check_settings(s)
        warns = [i for i in items if i.status == "WARN"
                 and i.label == "parse_error"]
        assert warns

    def test_f123_project_engine_rejection_visible_as_warn(self, tmp_path):
        """F-123: project 層の非ループバック engine 拒否が doctor の
        settings セクションに WARN として明確に表示されること"""
        proj = tmp_path / "vvread.settings.json"
        proj.write_text(json.dumps({
            "voicevox": {"engines": ["http://attacker.example:50021"]}
        }))
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=proj,
        )
        items = doctor_mod.check_settings(s)
        warns = [i for i in items if i.status == "WARN"
                 and i.label == "parse_error"]
        assert any(
            "非ループバック" in (i.detail or "") and "attacker.example" in (i.detail or "")
            for i in warns
        )

    def test_origin_displayed_in_detail(self, tmp_path):
        s = settings_mod.load(
            cwd=tmp_path,
            env={"VOICEVOX_SPEAKER": "11"},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_settings(s)
        i = _by_label(items, "voicevox.speaker")
        assert i is not None
        assert "env" in i.detail
        assert "VOICEVOX_SPEAKER" not in i.detail  # U-121: short form, no var name

    def test_project_origin_is_short_form(self, tmp_path):
        proj = tmp_path / "vvread.settings.json"
        proj.write_text(json.dumps({"voicevox": {"speaker": 5}}))
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=proj,
        )
        items = doctor_mod.check_settings(s)
        i = _by_label(items, "voicevox.speaker")
        assert i is not None
        assert "project" in i.detail
        assert str(proj) not in i.detail

    def test_user_origin_is_short_form(self, tmp_path):
        user_path = tmp_path / "u.json"
        user_path.write_text(json.dumps({"voicevox": {"speaker": 7}}))
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=user_path,
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_settings(s)
        i = _by_label(items, "voicevox.speaker")
        assert i is not None
        assert "user" in i.detail
        assert str(user_path) not in i.detail

    def test_env_origin_hides_varname(self, tmp_path):
        s = settings_mod.load(
            cwd=tmp_path,
            env={"VOICEVOX_SPEAKER": "3"},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_settings(s)
        i = _by_label(items, "voicevox.speaker")
        assert i is not None
        assert "env" in i.detail
        assert "VOICEVOX_SPEAKER" not in i.detail

    def test_derived_origin_keeps_detail(self, tmp_path):
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_settings(s)
        # voicevox.engines は voicevox.engineUrl から derived される場合がある。
        # derived origin を持つ任意のキーで確認する
        derived_items = [i for i in items if "derived" in (i.detail or "")]
        if derived_items:
            d = derived_items[0]
            assert "derived" in d.detail
        # default origin（detail なし）のキーは source のみが表示される
        default_items = [i for i in items if "default" in (i.detail or "")]
        if default_items:
            d = default_items[0]
            assert "default" in d.detail

    def test_check_paths_includes_settings_sources(self, tmp_path):
        proj = tmp_path / "vvread.settings.json"
        proj.write_text(json.dumps({"voicevox": {"speaker": 3}}))
        s = settings_mod.load(
            cwd=tmp_path, env={},
            user_path=tmp_path / "u.json",
            project_path=proj,
        )
        items = doctor_mod.check_paths(s)
        details = [i.detail for i in items if i.label == "settings_file"]
        assert any(str(proj) in d for d in details), \
            f"settings_file path not found in paths section: {details}"

    def test_collect_includes_paths_and_settings(self, tmp_path):
        items = doctor_mod.collect(cwd=tmp_path, offline=True, scope="runtime")
        assert any(i.section == "paths" for i in items)
        assert any(i.section == "settings" for i in items)


# ---------------------------------------------------------------------------
# check_dependencies
# ---------------------------------------------------------------------------


class TestCheckDependencies:
    def test_runtime_scope_excludes_dev_publish(self):
        items = doctor_mod.check_dependencies(scope="runtime")
        labels = {i.label for i in items}
        # runtime のみ
        assert "bash" in labels
        assert "python3" in labels
        # dev / publish は出ない
        assert "shellcheck" not in labels
        assert "gitleaks" not in labels

    def test_all_scope_includes_dev_publish(self):
        items = doctor_mod.check_dependencies(scope="all")
        labels = {i.label for i in items}
        assert "bash" in labels
        assert "shellcheck" in labels
        assert "gitleaks" in labels

    def test_required_missing_is_error(self, tmp_path, monkeypatch):
        # PATH を空にして bash / python3 / curl が無い状態を作る
        monkeypatch.setenv("PATH", str(tmp_path))
        items = doctor_mod.check_dependencies(scope="runtime")
        # required が ERROR で出る
        errors = [i for i in items if i.status == "ERROR"]
        labels = {i.label for i in errors}
        assert "bash" in labels
        assert "python3" in labels
        assert "curl" in labels

    def test_optional_missing_is_info_not_warn(self, tmp_path, monkeypatch):
        """optional が無くても WARN にしない(ユーザ仕様: 通常 doctor で warning 過多にしない)"""
        monkeypatch.setenv("PATH", str(tmp_path))
        items = doctor_mod.check_dependencies(scope="runtime")
        # afplay / paplay / e2k / rumps 等の optional は INFO であるべき
        for label in ("afplay", "paplay", "e2k", "rumps"):
            i = _by_label(items, label)
            assert i is not None
            assert i.status == "INFO", \
                f"{label} should be INFO for missing optional, got {i.status}"

    def test_unknown_scope_returns_error_item(self):
        items = doctor_mod.check_dependencies(scope="bogus")
        assert any(i.status == "ERROR" for i in items)


class TestCheckDependenciesRumpsPlatform:
    """rumps (B-151): macOS 専用機能。pyproject の `sys_platform == 'darwin'`
    marker により非 macOS では uv sync でも導入されないため、OS 判定を先に
    行い check_command を実行せず「対象外」の INFO を出す
    (paths.py / settings.py と同じ `platform.system() == "Darwin"` 系統)。"""

    def test_non_macos_reports_not_applicable(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        items = doctor_mod.check_dependencies(scope="runtime")
        i = _by_label(items, "rumps")
        assert i is not None
        assert i.status == "INFO"
        assert "not applicable" in (i.detail or "")

    def test_macos_does_not_report_not_applicable(self, tmp_path, monkeypatch):
        """macOS 判定時は「対象外」を出さず、通常の check_command 経路に進む。"""
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        # project_dir 無し(.venv 候補が無い)ので missing 経路(INFO)を安定再現
        items = doctor_mod.check_dependencies(scope="runtime")
        i = _by_label(items, "rumps")
        assert i is not None
        assert i.status == "INFO"
        assert "not applicable" not in (i.detail or "")

    def test_macos_with_project_dir_uses_venv_python(self, tmp_path, monkeypatch):
        """project_dir 指定時は .venv/bin/python 経由で import 判定する
        (R-009 別プロジェクト実行対応 + menubar.sh の解決順そのもの)。"""
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python"
        # "import rumps" が成功したことにする偽 python(shebang script)
        fake_python.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        fake_python.chmod(0o755)

        items = doctor_mod.check_dependencies(
            scope="runtime", project_dir=tmp_path,
        )
        i = _by_label(items, "rumps")
        assert i is not None
        assert i.status == "OK"
        assert str(fake_python) in (i.detail or "")


# ---------------------------------------------------------------------------
# _resolve_rumps_check (B-151 Codex レビュー指摘: menubar.sh と解決順を
# 完全一致させる。VVREAD_MENUBAR_PYTHON → .venv、システム python3 なし)
# ---------------------------------------------------------------------------


def _write_fake_python(path: Path, exit_code: int) -> None:
    """`[python, "-c", "import rumps"]` として呼ばれても exit_code を返すだけの
    偽 python(shebang script)。実際の import は一切試みない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env python3\nimport sys\nsys.exit({exit_code})\n")
    path.chmod(0o755)


class TestResolveRumpsCheck:
    """scripts/cmd/menubar.sh の Python 解決順(1. VVREAD_MENUBAR_PYTHON
    2. project_dir/.venv/bin/python 3. NG)と doctor を一致させる回帰テスト。"""

    def test_env_var_used_first(self, tmp_path, monkeypatch):
        env_python = tmp_path / "env_python"
        _write_fake_python(env_python, 0)
        venv_python = tmp_path / "proj" / ".venv" / "bin" / "python"
        _write_fake_python(venv_python, 0)
        monkeypatch.setenv("VVREAD_MENUBAR_PYTHON", str(env_python))

        result = doctor_mod._resolve_rumps_check(tmp_path / "proj")
        assert result.found is True
        assert result.path == str(env_python)

    def test_env_var_import_failure_falls_back_to_venv(self, tmp_path, monkeypatch):
        """VVREAD_MENUBAR_PYTHON が指す python で import が失敗した場合、
        menubar.sh と同様に .venv へ進む(即 NG にしない)。"""
        env_python = tmp_path / "env_python"
        _write_fake_python(env_python, 1)  # import rumps 失敗を模す
        venv_python = tmp_path / "proj" / ".venv" / "bin" / "python"
        _write_fake_python(venv_python, 0)
        monkeypatch.setenv("VVREAD_MENUBAR_PYTHON", str(env_python))

        result = doctor_mod._resolve_rumps_check(tmp_path / "proj")
        assert result.found is True
        assert result.path == str(venv_python)

    def test_env_var_nonexistent_path_falls_back_to_venv(self, tmp_path, monkeypatch):
        venv_python = tmp_path / "proj" / ".venv" / "bin" / "python"
        _write_fake_python(venv_python, 0)
        monkeypatch.setenv("VVREAD_MENUBAR_PYTHON", str(tmp_path / "does_not_exist"))

        result = doctor_mod._resolve_rumps_check(tmp_path / "proj")
        assert result.found is True
        assert result.path == str(venv_python)

    def test_neither_candidate_available_is_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        result = doctor_mod._resolve_rumps_check(tmp_path / "proj")  # .venv 無し
        assert result.found is False
        assert result.path is None

    def test_no_project_dir_and_no_env_is_not_found(self, monkeypatch):
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        result = doctor_mod._resolve_rumps_check(None)
        assert result.found is False

    def test_never_falls_back_to_generic_dependency_check(self, tmp_path, monkeypatch):
        """回帰防止の核心: rumps は `_deps.check()`(= システム PATH の
        python3 経由)へ絶対にフォールバックしない。フォールバックすると
        `python3` が voiceClaude 自身の .venv を指す開発環境で誤って
        found=True になり、`vvread doctor` は OK なのに `vvread menubar` は
        rumps not found で失敗する不整合が再発する。"""
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)

        def _must_not_be_called(dep):
            raise AssertionError(
                "_resolve_rumps_check must not fall back to _deps.check()"
            )

        monkeypatch.setattr(doctor_mod._deps, "check", _must_not_be_called)
        result = doctor_mod._resolve_rumps_check(tmp_path / "proj")  # .venv 無し
        assert result.found is False


class TestCheckDependenciesRumpsHint:
    """check_dependencies() の rumps NG 案内が menubar.sh の案内文言
    (`uv sync` 実行を促す文言)と一致していること。"""

    def test_ng_hint_matches_menubar_sh_wording(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.delenv("VVREAD_MENUBAR_PYTHON", raising=False)
        items = doctor_mod.check_dependencies(
            scope="runtime", project_dir=tmp_path,  # .venv 無し → NG
        )
        i = _by_label(items, "rumps")
        assert i is not None
        assert i.status == "INFO"
        assert i.hint == doctor_mod._RUMPS_INSTALL_HINT
        assert "uv sync" in i.hint
        assert "rumps package not found" in (i.detail or "")


# ---------------------------------------------------------------------------
# check_player
# ---------------------------------------------------------------------------


class TestCheckPlayer:
    def test_player_detected(self):
        # 本物の lib_playback.sh + 本物の PATH(macOS は afplay / Linux は paplay 等)
        items = doctor_mod.check_player()
        # macOS / Linux いずれかで何らかの player が見つかるはず(CI でも player は通常入っている)
        # ただし無くても doctor 側は ERROR を出すのが正しいので、ここでは label と
        # status の組のみ assertion(detect 結果に依存しない構造のみ確認)
        assert items
        assert items[0].section == "player"

    def test_player_path_with_shell_metacharacters_no_injection(self, tmp_path, monkeypatch):
        """L-3py 回帰: bash -c への f-string パス補間ではなく `$1` 引数渡しにより、
        パスに `"` や `;` を含む悪意あるディレクトリ名でもコマンドインジェクションが
        起きないこと。

        旧実装(f'source "{lib_playback}" && ...')では、`w"; touch PWNED; echo "z`
        のようなディレクトリ名を含むパスを埋め込むと、構築される bash コマンドが
        `source "<...>/w"; touch PWNED; echo "z/lib/playback.sh" && vvread_detect_player`
        に展開され、`touch PWNED` が実行されてしまう(この PoC は実際に旧実装で
        PWNED が作成されることを個別に確認済み)。
        """
        marker = tmp_path / "PWNED"
        weird_name = 'w"; touch PWNED; echo "z'
        scripts_dir = tmp_path / weird_name
        lib_dir = scripts_dir / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "playback.sh").write_text(
            "vvread_detect_player() { printf '%s\\n' 'testplayer'; return 0; }\n"
        )
        # 万一注入された場合の touch 実行 cwd をこの tmp_path 配下に固定する
        monkeypatch.chdir(tmp_path)

        items = doctor_mod.check_player(scripts_dir=scripts_dir)

        assert not marker.exists(), "path 経由でコマンドインジェクションが発生した(PWNED が作成された)"
        i = _by_label(items, "detected")
        assert i is not None
        assert i.status == "OK"
        assert i.detail == "testplayer"


# ---------------------------------------------------------------------------
# check_engine
# ---------------------------------------------------------------------------


class TestCheckEngine:
    def test_engine_unreachable(self, tmp_path):
        # 確実に通信失敗するアドレス
        items = doctor_mod.check_engine(
            engine_url="http://127.0.0.1:1",
            target_speaker=3,
            settings_obj=settings_mod.load(
                cwd=tmp_path, env={},
                user_path=tmp_path / "u.json",
                project_path=tmp_path / "p.json",
            ),
        )
        i = _by_label(items, "reachable")
        assert i is not None
        assert i.status == "ERROR"

    def test_engine_reachable_speaker_found(self, voicevox_mock, tmp_path):
        """default speakers payload は id 0..3 を返す(conftest)。speaker=2 は存在 → OK"""
        s = settings_mod.load(
            cwd=tmp_path,
            env={"VOICEVOX_SPEAKER": "2"},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_engine(
            engine_url=voicevox_mock["url"],
            settings_obj=s,
        )
        i_reach = _by_label(items, "reachable")
        i_speakers = _by_label(items, "speakers")
        i_target = _by_label(items, "target_speaker")
        assert i_reach.status == "OK"
        assert i_speakers.status == "OK"
        assert i_target.status == "OK"
        assert "2" in i_target.detail

    def test_engine_reachable_speaker_not_found(self, voicevox_mock, tmp_path):
        """speaker=999 は default payload (id 0..3) に無い → ERROR"""
        s = settings_mod.load(
            cwd=tmp_path,
            env={"VOICEVOX_SPEAKER": "999"},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_engine(
            engine_url=voicevox_mock["url"],
            settings_obj=s,
        )
        i_target = _by_label(items, "target_speaker")
        assert i_target.status == "ERROR"
        assert "999" in i_target.detail

    def test_engine_with_version_suffix_url(self, voicevox_mock, tmp_path):
        """`<base>/version` 形式の URL を渡しても base に正規化される(後方互換)"""
        s = settings_mod.load(
            cwd=tmp_path,
            env={"VOICEVOX_SPEAKER": "0"},
            user_path=tmp_path / "u.json",
            project_path=tmp_path / "p.json",
        )
        items = doctor_mod.check_engine(
            engine_url=voicevox_mock["url"] + "/version",
            settings_obj=s,
        )
        i_reach = _by_label(items, "reachable")
        assert i_reach.status == "OK"


# ---------------------------------------------------------------------------
# section: skipped (--offline)
# ---------------------------------------------------------------------------


class TestSkippedSections:
    def test_engine_skipped_label(self):
        items = doctor_mod.engine_section_skipped()
        assert len(items) == 1
        assert items[0].section == "engine"
        assert items[0].label == "skipped"
        assert items[0].status == "INFO"

    def test_claude_skipped_label(self):
        items = doctor_mod.claude_section_skipped()
        assert len(items) == 1
        assert items[0].section == "claude"
        assert items[0].label == "skipped"
        assert items[0].status == "INFO"


# ---------------------------------------------------------------------------
# check_claude
# ---------------------------------------------------------------------------


class TestCheckClaude:
    def test_claude_not_in_path_is_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        items = doctor_mod.check_claude()
        assert len(items) == 1
        assert items[0].status == "INFO"
        assert "not found" in items[0].detail

    def test_claude_old_version_is_warn(self, tmp_path, monkeypatch):
        # fake claude バイナリ: stdout に "1.0.0" を出す → 2.1.110 未満
        fake_claude = tmp_path / "claude"
        fake_claude.write_text("#!/bin/bash\necho 'claude 1.0.0'\nexit 0\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
        items = doctor_mod.check_claude()
        assert len(items) == 1
        assert items[0].status == "WARN"
        assert "1.0.0" in items[0].detail

    def test_claude_new_version_is_ok(self, tmp_path, monkeypatch):
        fake_claude = tmp_path / "claude"
        fake_claude.write_text("#!/bin/bash\necho 'claude 3.0.0'\nexit 0\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
        items = doctor_mod.check_claude()
        assert len(items) == 1
        assert items[0].status == "OK"

    def test_claude_unparseable_is_info(self, tmp_path, monkeypatch):
        fake_claude = tmp_path / "claude"
        fake_claude.write_text("#!/bin/bash\necho 'no version here'\nexit 0\n")
        fake_claude.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
        items = doctor_mod.check_claude()
        assert len(items) == 1
        assert items[0].status == "INFO"


# ---------------------------------------------------------------------------
# check_hooks
# ---------------------------------------------------------------------------


def _write_hook_settings(path: Path, command: str,
                        timeout: int | None = 600,
                        async_flag: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    hook_entry = {"type": "command", "command": command}
    if timeout is not None:
        hook_entry["timeout"] = timeout
    if async_flag:
        hook_entry["async"] = True
    payload = {
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [hook_entry]}
            ]
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


class TestCheckHooks:
    def test_no_hooks_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        items = doctor_mod.check_hooks(cwd=tmp_path)
        # 全階層 INFO + 「未登録」status
        assert any(i.label == "status" and i.status == "INFO" for i in items)

    def test_project_shared_hook_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/path/to/voiceClaude/bin/vvread on-stop",
            timeout=600, async_flag=True,
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        ok = [i for i in items if i.section == "hooks"
              and i.status == "OK" and i.label == "project-shared"]
        assert ok

    def test_short_timeout_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/path/to/voiceClaude/bin/vvread on-stop",
            timeout=60, async_flag=True,
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        warns = [i for i in items if i.status == "WARN"
                 and i.label == "project-shared"]
        assert warns
        assert "60s" in warns[0].detail

    def test_async_missing_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/path/to/voiceClaude/bin/vvread on-stop",
            timeout=600, async_flag=False,
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        warns = [i for i in items if i.status == "WARN"
                 and i.label == "project-shared"]
        assert warns
        assert "async" in (warns[0].hint or "")

    def test_duplicate_registration_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/path/to/voiceClaude/bin/vvread on-stop",
        )
        _write_hook_settings(
            tmp_path / "home" / ".claude" / "settings.json",
            command="/path/to/voiceClaude/bin/vvread on-stop",
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        dup = [i for i in items if i.label == "duplicate"]
        assert dup
        assert dup[0].status == "WARN"

    def test_legacy_on_stop_sh_recognized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/some/path/scripts/on_stop.sh",
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        # legacy パスでも matched と認識される
        ok = [i for i in items if i.section == "hooks"
              and i.label == "project-shared"
              and i.status in ("OK", "WARN")
              and "on_stop.sh" in (i.detail or "")]
        assert ok

    def test_unrelated_hook_is_not_matched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        _write_hook_settings(
            tmp_path / ".claude" / "settings.json",
            command="/usr/local/bin/some_other_tool",
        )
        items = doctor_mod.check_hooks(cwd=tmp_path)
        # vvread 系として認識されないので「該当なし」INFO
        infos = [i for i in items if i.section == "hooks"
                 and i.label == "project-shared"
                 and i.status == "INFO"
                 and "no vvread" in (i.detail or "")]
        assert infos


# ---------------------------------------------------------------------------
# check_vvread
# ---------------------------------------------------------------------------


class TestCheckVvread:
    def test_vvread_in_path(self, tmp_path, monkeypatch):
        # tmp_path に vvread の symlink を作って PATH に通す
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        target = bin_dir / "vvread"
        target.write_text("#!/bin/bash\nexit 0\n")
        target.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        items = doctor_mod.check_vvread()
        assert items[0].status == "OK"
        assert str(target) in items[0].detail

    def test_vvread_not_in_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        items = doctor_mod.check_vvread()
        assert items[0].status == "INFO"
        assert "not found" in items[0].detail


# ---------------------------------------------------------------------------
# collect & main
# ---------------------------------------------------------------------------


class TestCollectAndMain:
    def test_offline_skips_engine_and_claude(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        items = doctor_mod.collect(offline=True, cwd=tmp_path)
        # engine / claude は skipped INFO のみ
        engine_items = [i for i in items if i.section == "engine"]
        claude_items = [i for i in items if i.section == "claude"]
        assert all(i.label == "skipped" for i in engine_items)
        assert all(i.label == "skipped" for i in claude_items)

    def test_summary_status_counts(self, tmp_path, monkeypatch):
        # 単純な item リストで summary を直接検証
        items = [
            doctor_mod.CheckItem("a", "x", "OK"),
            doctor_mod.CheckItem("a", "y", "WARN"),
            doctor_mod.CheckItem("a", "z", "ERROR"),
            doctor_mod.CheckItem("a", "w", "INFO"),
        ]
        s = doctor_mod._summarize(items)
        assert s["OK"] == 1
        assert s["INFO"] == 1
        assert s["WARN"] == 1
        assert s["ERROR"] == 1


# ---------------------------------------------------------------------------
# check_queue（F-114 wedge / busy / stale mutate 診断）
# ---------------------------------------------------------------------------


class TestCheckQueue:
    def _mk_queue(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "log"))
        qd = tmp_path / "state" / "queue"
        for sub in ("pending", "playing", "failed"):
            (qd / sub).mkdir(parents=True, exist_ok=True)
        return qd

    def test_no_queue_dir_emits_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "state"))
        items = doctor_mod.check_queue()
        assert _statuses(items, "queue") == []

    def test_wedge_is_warn(self, tmp_path, monkeypatch):
        qd = self._mk_queue(tmp_path, monkeypatch)
        (qd / "pending" / "1000000000000_1.1.3.cli.r0").write_text("x")
        lock = qd / "queue.lock"; lock.mkdir()
        (lock / "owner").write_text(f"{os.getpid()}\ttok\n")  # 生存 pid
        (lock / "hb").write_text("100\n")        # stale
        (lock / "progress").write_text("100\n")  # stale
        items = doctor_mod.check_queue()
        d = _by_label(items, "drainer")
        assert d is not None and d.status == "WARN"
        assert "wedged" in d.detail
        assert "queue reset" in (d.hint or "")

    def test_busy_is_info(self, tmp_path, monkeypatch):
        import time as _t
        qd = self._mk_queue(tmp_path, monkeypatch)
        (qd / "pending" / "1000000000000_1.1.3.cli.r0").write_text("x")
        lock = qd / "queue.lock"; lock.mkdir()
        (lock / "owner").write_text(f"{os.getpid()}\ttok\n")
        (lock / "hb").write_text(f"{int(_t.time())}\n")  # fresh = 再生中
        (lock / "progress").write_text("100\n")          # stale
        items = doctor_mod.check_queue()
        d = _by_label(items, "drainer")
        assert d is not None and d.status == "INFO"
        assert "busy" in d.detail

    def test_no_drainer_is_ok(self, tmp_path, monkeypatch):
        self._mk_queue(tmp_path, monkeypatch)
        items = doctor_mod.check_queue()
        d = _by_label(items, "drainer")
        assert d is not None and d.status == "OK"

    def test_stale_mutate_is_warn(self, tmp_path, monkeypatch):
        qd = self._mk_queue(tmp_path, monkeypatch)
        m = qd / "queue.mutate.lock"; m.mkdir()
        (m / "owner").write_text(f"{os.getpid()}\ttok\n")  # 生存
        (m / "hb").write_text("100\n")                      # stale
        items = doctor_mod.check_queue()
        ml = _by_label(items, "mutate.lock")
        assert ml is not None and ml.status == "WARN"
