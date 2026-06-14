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
            (tmp_path / sub).mkdir()
        items = doctor_mod.check_paths()
        labels = {(i.label, i.status) for i in items}
        assert ("state", "OK") in labels
        assert ("log", "OK") in labels
        assert ("cache", "OK") in labels

    def test_missing_dirs_are_info(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path / "no_state"))
        monkeypatch.setenv("VVREAD_LOG_DIR", str(tmp_path / "no_log"))
        monkeypatch.setenv("VVREAD_CACHE_DIR", str(tmp_path / "no_cache"))
        items = doctor_mod.check_paths()
        for label in ("state", "log", "cache"):
            i = _by_label(items, label)
            assert i.status == "INFO"
            assert "will be created" in i.detail


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
        assert "VOICEVOX_SPEAKER" in i.detail


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
        # afplay / paplay / e2k 等の optional は INFO であるべき
        for label in ("afplay", "paplay", "e2k"):
            i = _by_label(items, label)
            assert i is not None
            assert i.status == "INFO", \
                f"{label} should be INFO for missing optional, got {i.status}"

    def test_unknown_scope_returns_error_item(self):
        items = doctor_mod.check_dependencies(scope="bogus")
        assert any(i.status == "ERROR" for i in items)


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
