"""scripts/hook_install.py のテスト (R-008)

install / uninstall の Python module を直接 import + DI で動かして検証する。
CLI / bin/vvread 経由は tests/test_cmd_install.py / test_cmd_uninstall.py 側。

ユーザ仕様(R-008)で固定すべき項目:
- 各 scope (project / project-shared / user) の path 解決
- 新規 settings 作成 / 既存 settings への merge(他 hook を保持)
- 二重登録しない
- legacy `scripts/on_stop.sh` 検出時 ERROR(自動置換しない)
- uninstall は voiceClaude 管理の hook のみ削除、他は保持
- `.bak` 作成
- `--dry-run` でファイル変更なし
- 空白を含むパス
- JSON 破損時に止まる
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import hook_install as hi  # noqa: E402


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _make_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """cwd / home の擬似ディレクトリを作って返す"""
    cwd = tmp_path / "proj"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    return cwd, home


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# resolve_settings_path
# ---------------------------------------------------------------------------


class TestResolveSettingsPath:
    def test_project_local_scope(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        p = hi.resolve_settings_path("project-local", cwd=cwd, home=home)
        assert p == cwd / ".claude" / "settings.local.json"

    def test_project_scope(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        p = hi.resolve_settings_path("project", cwd=cwd, home=home)
        assert p == cwd / ".claude" / "settings.json"

    def test_user_scope(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        p = hi.resolve_settings_path("user", cwd=cwd, home=home)
        assert p == home / ".claude" / "settings.json"

    def test_unknown_scope_raises(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        with pytest.raises(ValueError):
            hi.resolve_settings_path("bogus", cwd=cwd, home=home)


# ---------------------------------------------------------------------------
# is_voiceclaude_hook / legacy 判定
# ---------------------------------------------------------------------------


class TestHookDetection:
    def test_modern_path_detected(self):
        assert hi.is_voiceclaude_hook("/path/to/bin/vvread on-stop")

    def test_path_with_quotes(self):
        assert hi.is_voiceclaude_hook('"/path with space/bin/vvread" on-stop')

    def test_unrelated_command_not_matched(self):
        assert not hi.is_voiceclaude_hook("/usr/local/bin/some_other_tool")
        assert not hi.is_voiceclaude_hook("echo hello")

    def test_repo_root_match(self, tmp_path):
        rr = tmp_path / "repo"
        rr.mkdir()
        cmd = f"{rr}/scripts/on_stop.sh"
        assert hi.is_voiceclaude_hook(cmd, repo_root=rr)


# ---------------------------------------------------------------------------
# build_hook_command
# ---------------------------------------------------------------------------


class TestBuildHookCommand:
    def test_no_space_path(self, tmp_path):
        rr = tmp_path / "repo"
        cmd = hi.build_hook_command(rr)
        assert cmd == f"{rr}/bin/vvread on-stop"
        assert "on-stop" in cmd

    def test_space_in_path_is_quoted(self, tmp_path):
        rr = tmp_path / "with space" / "repo"
        cmd = hi.build_hook_command(rr)
        # ダブルクォートで囲まれて on-stop が後続する
        assert cmd.startswith('"')
        assert '"' in cmd
        assert cmd.endswith(" on-stop")
        # シェル評価で正しく分割されるか shlex で検証
        import shlex
        parts = shlex.split(cmd)
        assert parts == [f"{rr}/bin/vvread", "on-stop"]


# ---------------------------------------------------------------------------
# install: 新規作成 / merge / 二重登録 / dry-run / .bak
# ---------------------------------------------------------------------------


class TestInstallNew:
    def test_creates_new_settings_file(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is True
        assert result.settings_path.exists()
        data = _read_settings(result.settings_path)
        # hooks.Stop[0].hooks[0].command が voiceClaude を指す
        cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "vvread" in cmd
        assert "on-stop" in cmd
        # default timeout / async が設定される
        entry = data["hooks"]["Stop"][0]["hooks"][0]
        assert entry["timeout"] == 600
        assert entry["async"] is True

    def test_default_scope_is_project_local(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(cwd=cwd, home=home, repo_root=repo)
        assert result.settings_path.name == "settings.local.json"

    def test_user_scope_writes_to_home(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="user", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert (home / ".claude" / "settings.json").exists()


class TestInstallMerge:
    def test_preserves_existing_unrelated_keys(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "permissions": {"allow": ["Bash(*)"]},
            "model": "opus",
        })
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        data = _read_settings(target)
        # 既存キーが保たれる
        assert data["permissions"] == {"allow": ["Bash(*)"]}
        assert data["model"] == "opus"
        # voiceClaude hook が追加されている
        assert "hooks" in data

    def test_preserves_other_hooks_in_stop(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": "/usr/local/bin/some_other_tool",
                    }]},
                ]
            }
        })
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        data = _read_settings(target)
        stop = data["hooks"]["Stop"]
        # 既存 hook が残り、voiceClaude hook が追加されている
        cmds = []
        for block in stop:
            for h in block["hooks"]:
                cmds.append(h["command"])
        assert "/usr/local/bin/some_other_tool" in cmds
        assert any("vvread" in c and "on-stop" in c for c in cmds)


class TestInstallDoubleRegister:
    def test_skip_when_already_registered(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # 1 回目
        r1 = hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        assert r1.changed is True
        # 2 回目: skip
        r2 = hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        assert r2.error is None
        assert r2.changed is False
        assert r2.skipped_already_present is True
        # ファイル内の hook entry は 1 つのまま
        data = _read_settings(r2.settings_path)
        flat_hooks = []
        for block in data["hooks"]["Stop"]:
            flat_hooks.extend(block["hooks"])
        vc_count = sum(
            1 for h in flat_hooks if hi.is_voiceclaude_hook(h.get("command", ""))
        )
        assert vc_count == 1


class TestInstallDryRun:
    def test_dry_run_does_not_create_file(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
            dry_run=True,
        )
        assert result.error is None
        assert result.changed is False
        assert result.dry_run is True
        # ファイルが作られていない
        assert not result.settings_path.exists()

    def test_dry_run_does_not_modify_existing(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {"model": "opus"})
        original = target.read_text(encoding="utf-8")
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
            dry_run=True,
        )
        assert result.error is None
        assert result.changed is False
        # 既存ファイル内容が変わっていない
        assert target.read_text(encoding="utf-8") == original
        # .bak も作られていない
        assert not target.with_suffix(target.suffix + ".bak").exists()


class TestInstallBackup:
    def test_bak_created_on_change(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {"model": "sonnet"})
        original = target.read_text(encoding="utf-8")
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is True
        assert result.backup_path is not None
        assert result.backup_path.exists()
        # .bak には変更前の内容
        assert result.backup_path.read_text(encoding="utf-8") == original

    def test_no_bak_when_file_did_not_exist(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.changed is True
        # 元ファイルが無かったので .bak も作らない
        assert result.backup_path is None

    def test_bak_overwritten_on_repeat_install_after_uninstall(self, tmp_path):
        """install → uninstall → install の繰り返しで .bak が上書きされる"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # 1 回目 install(元ファイル無し → .bak 無し)
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        # 1 回目 uninstall(変更ありで .bak 作られる)
        r1 = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert r1.backup_path is not None
        # 2 回目 install(変更ありで .bak 上書き)
        r2 = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert r2.backup_path is not None
        assert r2.backup_path == r1.backup_path  # 同じパス(上書き)


class TestInstallAtomicWrite:
    """advisor 助言で追加: 書込が atomic で .tmp が残らないこと"""

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is True
        # .tmp が残っていない(os.replace で正常 rename されている)
        tmp_path_artifact = result.settings_path.with_suffix(
            result.settings_path.suffix + ".tmp"
        )
        assert not tmp_path_artifact.exists()

    def test_uninstall_no_tmp_file_left_behind(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # install してから uninstall
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        tmp_path_artifact = result.settings_path.with_suffix(
            result.settings_path.suffix + ".tmp"
        )
        assert not tmp_path_artifact.exists()


class TestInstallSpaceInPath:
    def test_install_with_space_in_repo_path(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        # repo パスに空白を含める
        repo = tmp_path / "with space" / "repo"
        repo.mkdir(parents=True)
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is True
        cmd = result.hook_command
        # ダブルクォートで囲まれている
        assert cmd.startswith('"')
        assert " on-stop" in cmd
        # shlex で 2 トークンに分割される
        import shlex
        parts = shlex.split(cmd)
        assert len(parts) == 2
        assert parts[1] == "on-stop"
        assert parts[0].endswith("/bin/vvread")
        # 設定にも書かれている
        data = _read_settings(result.settings_path)
        written_cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert written_cmd == cmd


class TestInstallBrokenJson:
    def test_broken_json_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ this is not json", encoding="utf-8")
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is not None
        assert "invalid JSON" in result.error
        # ファイルは変更されていない
        assert target.read_text(encoding="utf-8") == "{ this is not json"
        # .bak も作られていない
        assert not target.with_suffix(target.suffix + ".bak").exists()

    def test_top_level_array_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("[1,2,3]", encoding="utf-8")
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is not None
        assert "object" in result.error


# ---------------------------------------------------------------------------
# uninstall: vvread のみ削除 / 他保持 / dry-run / bak / 該当無し
# ---------------------------------------------------------------------------


class TestUninstallOnlyVvread:
    def test_removes_only_voiceclaude_hooks(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        # voiceClaude + 別ツールの hook を混在
        _write_settings(target, {
            "model": "opus",
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": f"{repo}/bin/vvread on-stop",
                        "timeout": 600,
                        "async": True,
                    }]},
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": "/usr/local/bin/other_tool",
                    }]},
                ]
            }
        })
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is True
        assert result.removed_count == 1
        data = _read_settings(target)
        # 既存の他 hook + 他キーは残る
        assert data["model"] == "opus"
        stop = data["hooks"]["Stop"]
        # voiceClaude block は削除されて other_tool だけ残る
        assert len(stop) == 1
        assert stop[0]["hooks"][0]["command"] == "/usr/local/bin/other_tool"

    def test_collapses_empty_block(self, tmp_path):
        """voiceClaude のみだった block は削除される(残骸を残さない)"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": f"{repo}/bin/vvread on-stop",
                    }]},
                ]
            }
        })
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        data = _read_settings(target)
        # hooks.Stop が空になり、Stop 自体が消える、最終的に hooks も消える
        assert "hooks" not in data

    def test_no_op_when_no_voiceclaude_hook(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": "/usr/local/bin/other_tool",
                    }]},
                ]
            }
        })
        original = target.read_text(encoding="utf-8")
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is False
        assert result.removed_count == 0
        # ファイル内容は変更されない
        assert target.read_text(encoding="utf-8") == original

    def test_no_op_when_file_missing(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.changed is False
        assert result.removed_count == 0


class TestUninstallDryRun:
    def test_dry_run_does_not_modify(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": f"{repo}/bin/vvread on-stop",
                    }]},
                ]
            }
        })
        original = target.read_text(encoding="utf-8")
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
            dry_run=True,
        )
        assert result.error is None
        assert result.dry_run is True
        assert result.removed_count == 1  # 検出はする
        assert result.changed is False    # 書込はしない
        # ファイル不変
        assert target.read_text(encoding="utf-8") == original


class TestUninstallBackup:
    def test_bak_created_when_change(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        _write_settings(target, {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{
                        "type": "command",
                        "command": f"{repo}/bin/vvread on-stop",
                    }]},
                ]
            }
        })
        original = target.read_text(encoding="utf-8")
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is None
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert result.backup_path.read_text(encoding="utf-8") == original


class TestUninstallBrokenJson:
    def test_broken_json_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        target = cwd / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{ broken", encoding="utf-8")
        result = hi.uninstall(
            scope="project-local", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is not None
        assert "invalid JSON" in result.error


# ---------------------------------------------------------------------------
# scope 不正
# ---------------------------------------------------------------------------


class TestInvalidScope:
    def test_install_unknown_scope_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="bogus", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is not None
        assert "scope" in result.error.lower()

    def test_uninstall_unknown_scope_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.uninstall(
            scope="bogus", cwd=cwd, home=home, repo_root=repo,
        )
        assert result.error is not None


# ---------------------------------------------------------------------------
# deprecated scope alias
# ---------------------------------------------------------------------------


class TestDeprecatedScopeAlias:
    def test_project_shared_resolves_to_project(self):
        resolved, warn = hi._resolve_scope_alias("project-shared")
        assert resolved == "project"
        assert warn is not None
        assert "deprecated" in warn.lower()

    def test_non_deprecated_scope_passthrough(self):
        for scope in hi.SCOPES:
            resolved, warn = hi._resolve_scope_alias(scope)
            assert resolved == scope
            assert warn is None

    def test_project_shared_install_writes_to_settings_json(self, tmp_path, capsys):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # deprecated alias を _resolve_scope_alias で解決して install に渡す
        resolved, _warn = hi._resolve_scope_alias("project-shared")
        result = hi.install(scope=resolved, cwd=cwd, home=home, repo_root=repo)
        assert result.error is None
        assert result.changed is True
        # settings.json に書かれていること（deprecated alias の "project" 相当）
        assert result.settings_path.name == "settings.json"


# ---------------------------------------------------------------------------
# interactive_install
# ---------------------------------------------------------------------------


import io  # noqa: E402 (テスト専用ユーティリティ)


def _tty_stream(content: str) -> io.StringIO:
    s = io.StringIO(content)
    s.isatty = lambda: True
    return s


class TestInteractiveInstall:
    def test_already_registered_exits_early(self, tmp_path):
        """scope 選択後、既に登録済みなら対話を打ち切って exit 0"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        import json
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        # 先に hook を登録しておく
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        out = io.StringIO()
        # scope 選択で Enter（project-local = デフォルト）のみ入力
        in_stream = _tty_stream("\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert "設定済" in out.getvalue()
        assert "uninstall" in out.getvalue()
        assert "config" in out.getvalue()

    def test_non_tty_without_yes_returns_error(self, tmp_path):
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        err = io.StringIO()
        non_tty = io.StringIO()
        non_tty.isatty = lambda: False
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=non_tty, err_stream=err,
        )
        assert rc == 1
        assert "ERROR" in err.getvalue()
        assert "TTY" in err.getvalue()

    def test_yes_bypasses_to_direct_install(self, tmp_path):
        """--yes を渡せば非 TTY でも install 成功（_cmd_install 経由で確認）。"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        result = hi.install(
            scope="project-local", cwd=cwd, home=home, repo_root=repo, yes=True,
        )
        assert result.error is None
        assert result.changed is True

    def test_scope_defaults_to_project_local(self, tmp_path):
        """TTY あり + Enter で scope 選択 → デフォルト project-local に install"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        out = io.StringIO()
        # Engine を unreachable に向けて speaker 選択をスキップ
        import json
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        in_stream = _tty_stream("\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert (cwd / ".claude" / "settings.local.json").exists()

    def test_no_speaker_when_engine_unreachable(self, tmp_path):
        """Engine 未接続時は speaker 選択をスキップして install は成功"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        out = io.StringIO()
        import json
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        in_stream = _tty_stream("\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert "スキップ" in out.getvalue() or "VOICEVOX" in out.getvalue()

    def test_speaker_written_to_vvread_settings(self, tmp_path):
        """Engine 接続時: speaker 選択が vvread.settings.json に書かれる"""
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        sample = [{"name": "テスト", "styles": [{"name": "ノーマル", "id": 5}, {"name": "あまあま", "id": 9}]}]
        body = json.dumps(sample).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *a): pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # vvread.settings.json に engine URL を設定
        settings_path = cwd / "vvread.settings.json"
        settings_path.write_text(
            json.dumps({"voicevox": {"engineUrl": f"http://127.0.0.1:{port}"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        # scope Enter + speaker Enter
        in_stream = _tty_stream("\n\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        server.shutdown()
        assert rc == 0
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["voicevox"]["speaker"] == 5
