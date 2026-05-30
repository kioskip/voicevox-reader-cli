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

    def test_repo_root_path_alone_does_not_match(self, tmp_path):
        """F-102 回帰: repo_root パスを含むが /bin/vvread も on_stop.sh も含まない
        コマンドは誤検知されない"""
        rr = tmp_path / "repo"
        rr.mkdir()
        cmd = f"some-other-tool --label on-stop --workdir {rr}/data"
        assert not hi.is_voiceclaude_hook(cmd, repo_root=rr)


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
        assert r2.scope == "project-local"
        # ファイル内の hook entry は 1 つのまま
        data = _read_settings(r2.settings_path)
        flat_hooks = []
        for block in data["hooks"]["Stop"]:
            flat_hooks.extend(block["hooks"])
        vc_count = sum(
            1 for h in flat_hooks if hi.is_voiceclaude_hook(h.get("command", ""))
        )
        assert vc_count == 1

    def test_scope_stored_in_result(self, tmp_path):
        """scope が InstallResult に記録される。"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        for scope in ("user", "project", "project-local"):
            result = hi.install(scope=scope, cwd=cwd, home=home, repo_root=repo)
            assert result.scope == scope


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
    def test_shows_hook_status_table(self, tmp_path):
        """install 開始時に全 scope の hook 状況テーブルが表示される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        in_stream = _tty_stream("\n\n")
        hi.interactive_install(cwd=cwd, home=home, repo_root=repo,
                               in_stream=in_stream, out_stream=out)
        output = out.getvalue()
        assert "Claude Code hook 状況:" in output
        assert "user" in output
        assert "project" in output
        assert "project-local" in output

    def test_already_registered_shows_message_and_proceeds_to_step2(self, tmp_path):
        """登録済みなら scope 選択をスキップし、Step 2 に進む"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        out = io.StringIO()
        in_stream = _tty_stream("")  # 入力不要（scope 選択なし・settings 存在）
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        output = out.getvalue()
        assert "有効" in output
        assert "vvread project settings:" in output

    def test_skips_hook_registration_when_user_registered(self, tmp_path):
        """user scope に登録済みなら scope 選択プロンプトが出ない"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        hi.install(scope="user", cwd=cwd, home=home, repo_root=repo)
        out = io.StringIO()
        in_stream = _tty_stream("")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert "既に有効" in out.getvalue()
        assert "登録先を選択" not in out.getvalue()

    def test_step2_runs_even_if_hook_already_registered(self, tmp_path):
        """hook 登録済みでも Step 2（settings 状況確認）は必ず実行される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        hi.install(scope="user", cwd=cwd, home=home, repo_root=repo)
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()

        out = io.StringIO()
        in_stream = _tty_stream("\n")  # Step 2 Y/n で Y
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert "vvread project settings:" in out.getvalue()

    def test_legacy_scope_excluded_from_choices(self, tmp_path):
        """legacy hook のある scope は選択肢から除外される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        legacy_cmd = f"{repo}/scripts/on_stop.sh"
        _write_legacy_settings(cwd / ".claude" / "settings.local.json", legacy_cmd)
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        in_stream = _tty_stream("\n\n")  # scope=user (default), settings exists
        hi.interactive_install(cwd=cwd, home=home, repo_root=repo,
                               in_stream=in_stream, out_stream=out)
        output = out.getvalue()
        # 選択肢に project-local が出ない
        assert "project-local  →" not in output

    def test_all_scopes_legacy_shows_no_change_message(self, tmp_path):
        """全 scope が legacy の場合は「今回は変更していません」を表示して Step 2 へ"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        legacy_cmd = f"{repo}/scripts/on_stop.sh"
        _write_legacy_settings(cwd / ".claude" / "settings.local.json", legacy_cmd)
        _write_legacy_settings(cwd / ".claude" / "settings.json", legacy_cmd)
        _write_legacy_settings(home / ".claude" / "settings.json", legacy_cmd)
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        in_stream = _tty_stream("")  # 入力不要（scope 選択なし・settings 存在）
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        output = out.getvalue()
        assert "今回は変更していません" in output
        assert "vvread uninstall" in output
        assert "登録先を選択" not in output

    def test_project_scope_shows_warning(self, tmp_path):
        """project scope 選択時に共有設定の注意文が表示される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )
        out = io.StringIO()
        # 選択肢: 1=user, 2=project-local, 3=project → "3" で project を選択
        in_stream = _tty_stream("3\n")
        hi.interactive_install(cwd=cwd, home=home, repo_root=repo,
                               in_stream=in_stream, out_stream=out)
        assert "git 管理下" in out.getvalue()

    def test_step2_no_creates_settings_with_message(self, tmp_path):
        """Step 2 で n を選択するとファイル未作成 + 案内メッセージ"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()
        out = io.StringIO()
        # scope=user Enter, settings Y/n で "n"
        in_stream = _tty_stream("\nn\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert not vvread_settings.exists()
        assert "プロジェクト専用の設定を保存できません" in out.getvalue()

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

    def test_scope_defaults_to_user(self, tmp_path):
        """TTY あり + Enter で scope 選択 → デフォルト user に install"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        out = io.StringIO()
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
        assert (home / ".claude" / "settings.json").exists()

    def test_no_speaker_when_engine_unreachable(self, tmp_path, monkeypatch):
        """Engine 未接続時は speaker 選択をスキップして install は成功"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # engine を常に unreachable に見せる
        monkeypatch.setattr(hi, "_fetch_speakers_for_install", lambda *a, **kw: None)
        out = io.StringIO()
        # settings 未作成 → Step 2 で Y/n → Y → engine unreachable → スキップ
        in_stream = _tty_stream("\n\n")  # scope Enter, Y/n Enter
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert "スキップ" in out.getvalue() or "VOICEVOX" in out.getvalue()

    def test_speaker_written_to_vvread_settings(self, tmp_path, monkeypatch):
        """Engine 接続時: speaker 選択が vvread.settings.json に書かれる"""
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
        # settings 未作成 + エンジン URL を env で指定
        monkeypatch.setenv("VOICEVOX_ENGINE_URL", f"http://127.0.0.1:{port}")
        settings_path = cwd / "vvread.settings.json"
        out = io.StringIO()
        # scope Enter, Y/n=Y Enter, speaker Enter (デフォルト=5)
        in_stream = _tty_stream("\n\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        server.shutdown()
        assert rc == 0
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["voicevox"]["speaker"] == 5


# ---------------------------------------------------------------------------
# F-105: install 後に vvread.settings.json が作成されること
# ---------------------------------------------------------------------------


class TestEnsureVvreadSettingsFile:
    def test_already_installed_creates_vvread_settings_when_missing(self, tmp_path):
        """hook 登録済みでも Step 2 で Y を選べば vvread.settings.json が作成される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()

        out = io.StringIO()
        in_stream = _tty_stream("\n")  # Step 2 Y/n で Y（デフォルト）
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert vvread_settings.exists()

    def test_fresh_install_creates_vvread_settings_when_missing(self, tmp_path):
        """fresh install（VOICEVOX 未起動）後に vvread.settings.json が作成される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()

        out = io.StringIO()
        in_stream = _tty_stream("\n\n")  # scope Enter, Y/n=Y Enter
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        assert vvread_settings.exists()

    def test_yes_install_creates_vvread_settings_when_missing(self, tmp_path, monkeypatch):
        """--yes 非対話パスでも vvread.settings.json が作成される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(cwd)
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()

        argv = ["install", "--yes", "--scope", "project-local"]
        rc = hi.main(argv)
        assert rc == 0
        assert vvread_settings.exists()

    def test_yes_mode_creates_settings_even_if_hook_registered(self, tmp_path, monkeypatch):
        """--yes 経路: hook 登録済みでも vvread.settings.json が未作成なら作成される"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        # 先に hook を登録しておく
        hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)
        monkeypatch.chdir(cwd)
        vvread_settings = cwd / "vvread.settings.json"
        assert not vvread_settings.exists()

        # hook は既に登録済みだが --yes で install しても settings は作成される
        argv = ["install", "--yes", "--scope", "user"]
        rc = hi.main(argv)
        assert rc == 0
        assert vvread_settings.exists()

    def test_install_does_not_overwrite_existing_vvread_settings(self, tmp_path):
        """既存の vvread.settings.json は上書きしない"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        vvread_settings = cwd / "vvread.settings.json"
        original = {"voicevox": {"speaker": 99}}
        vvread_settings.write_text(json.dumps(original), encoding="utf-8")

        out = io.StringIO()
        in_stream = _tty_stream("\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )
        assert rc == 0
        data = json.loads(vvread_settings.read_text(encoding="utf-8"))
        assert data["voicevox"]["speaker"] == 99

    def test_dry_run_does_not_create_vvread_settings(self, tmp_path):
        """dry-run では vvread.settings.json を作成しない"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        vvread_settings = cwd / "vvread.settings.json"

        out = io.StringIO()
        in_stream = _tty_stream("\n\n")
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
            dry_run=True,
        )
        assert rc == 0
        assert not vvread_settings.exists()
        assert "DRY-RUN" in out.getvalue()


# ---------------------------------------------------------------------------
# F-103: legacy on_stop.sh hook の検出テスト
# ---------------------------------------------------------------------------


def _write_legacy_settings(path: Path, legacy_cmd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "hooks": {
            "Stop": [
                {"matcher": "", "hooks": [{"type": "command", "command": legacy_cmd}]}
            ]
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")


class TestScanForVoiceclaude:
    def test_legacy_cmds_populated_on_on_stop_sh(self, tmp_path):
        """_scan_for_voiceclaude が on_stop.sh hook を検出したら legacy_cmds に追加する"""
        rr = tmp_path / "repo"
        rr.mkdir()
        stop_blocks = [
            {"matcher": "", "hooks": [{"type": "command", "command": f"{rr}/scripts/on_stop.sh"}]}
        ]
        has_vc, legacy_cmds = hi._scan_for_voiceclaude(stop_blocks, rr)
        assert has_vc
        assert len(legacy_cmds) == 1
        assert "on_stop.sh" in legacy_cmds[0]

    def test_modern_hook_not_added_to_legacy_cmds(self, tmp_path):
        """modern vvread on-stop は legacy_cmds に含まれない"""
        stop_blocks = [
            {"matcher": "", "hooks": [{"type": "command", "command": "/path/bin/vvread on-stop"}]}
        ]
        has_vc, legacy_cmds = hi._scan_for_voiceclaude(stop_blocks)
        assert has_vc
        assert len(legacy_cmds) == 0


class TestInstallLegacy:
    def test_install_returns_error_and_no_change_on_legacy_hook(self, tmp_path):
        """install() が legacy hook 検出時にエラーを返し、変更・bak が作られないこと"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        settings_path = cwd / ".claude" / "settings.local.json"
        _write_legacy_settings(settings_path, f"{repo}/scripts/on_stop.sh")

        result = hi.install(scope="project-local", cwd=cwd, home=home, repo_root=repo)

        assert result.legacy_detected
        assert result.error is not None
        assert "on_stop.sh" in result.error
        assert "今回は変更していません" in result.error
        assert result.changed is False
        assert not (cwd / ".claude" / "settings.local.json.bak").exists()

    def test_interactive_install_warns_when_all_scopes_legacy(self, tmp_path):
        """全 scope が legacy の場合、今回は変更していませんと表示して Step 2 へ進む"""
        cwd, home = _make_dirs(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        legacy_cmd = f"{repo}/scripts/on_stop.sh"
        _write_legacy_settings(cwd / ".claude" / "settings.local.json", legacy_cmd)
        _write_legacy_settings(cwd / ".claude" / "settings.json", legacy_cmd)
        _write_legacy_settings(home / ".claude" / "settings.json", legacy_cmd)
        (cwd / "vvread.settings.json").write_text(
            json.dumps({"voicevox": {"engineUrl": "http://127.0.0.1:1"}}),
            encoding="utf-8",
        )

        out = io.StringIO()
        in_stream = _tty_stream("")  # 入力不要（scope 選択なし・settings 存在）
        rc = hi.interactive_install(
            cwd=cwd, home=home, repo_root=repo,
            in_stream=in_stream, out_stream=out,
        )

        assert rc == 0
        output = out.getvalue()
        assert "今回は変更していません" in output
        assert "vvread uninstall" in output
        assert "登録先を選択" not in output
