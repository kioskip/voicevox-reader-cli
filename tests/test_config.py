"""scripts/config.py のテスト (B-014)

vvread.settings.json を対話式に編集する config コマンドのテスト。

テスト方針:
- in_stream / out_stream を StringIO で注入して対話を疑似的に実行
- TTY 判定は in_stream.isatty() のモックで制御
- 実際の settings ファイル / OS パスには触らない
"""
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import config as cfg  # noqa: E402


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _tty_stream(content: str = "") -> io.StringIO:
    """isatty() が True を返す StringIO を作る。"""
    stream = io.StringIO(content)
    stream.isatty = lambda: True
    return stream


def _non_tty_stream() -> io.StringIO:
    stream = io.StringIO()
    stream.isatty = lambda: False
    return stream


def _make_ctx(
    tmp_path: Path,
    input_text: str = "",
    tty: bool = True,
    dry_run: bool = False,
    create: bool = False,
    user_setting: bool = False,
    set_pairs: list = None,
    json_patch: str = None,
    list_mode: bool = False,
) -> tuple:
    """ConfigContext + out / err バッファを返す。"""
    out = io.StringIO()
    err = io.StringIO()
    in_stream = _tty_stream(input_text) if tty else _non_tty_stream()
    ctx = cfg.ConfigContext(
        dry_run=dry_run,
        create=create,
        user_setting=user_setting,
        set_pairs=set_pairs or [],
        json_patch=json_patch,
        list_mode=list_mode,
        cwd=tmp_path,
        in_stream=in_stream,
        out_stream=out,
        err_stream=err,
    )
    return ctx, out, err


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _require_tty
# ---------------------------------------------------------------------------


class TestRequireTty:
    def test_tty_returns_none(self, tmp_path):
        ctx, _, _ = _make_ctx(tmp_path, tty=True)
        assert cfg._require_tty(ctx) is None

    def test_non_tty_returns_error_msg(self, tmp_path):
        ctx, _, _ = _make_ctx(tmp_path, tty=False)
        msg = cfg._require_tty(ctx)
        assert msg is not None
        assert "エラー" in msg


# ---------------------------------------------------------------------------
# _find_settings_file
# ---------------------------------------------------------------------------


class TestFindSettingsFile:
    def test_finds_project_settings(self, tmp_path):
        project = tmp_path / "vvread.settings.json"
        _write_settings(project, {})
        result = cfg._find_settings_file(tmp_path)
        assert result is not None
        label, path = result
        assert label == "project"
        assert path == project

    def test_finds_user_settings_fallback(self, tmp_path):
        user_path = tmp_path / "user_settings.json"
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            result = cfg._find_settings_file(tmp_path)
        assert result is not None
        label, path = result
        assert label == "user"
        assert path == user_path

    def test_project_takes_priority_over_user(self, tmp_path):
        project = tmp_path / "vvread.settings.json"
        user_path = tmp_path / "user_settings.json"
        _write_settings(project, {})
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            result = cfg._find_settings_file(tmp_path)
        assert result is not None
        label, _ = result
        assert label == "project"

    def test_returns_none_when_both_missing(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            result = cfg._find_settings_file(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# _load_vvread_settings
# ---------------------------------------------------------------------------


class TestLoadVvreadSettings:
    def test_loads_valid_json(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 5}})
        data, err = cfg._load_vvread_settings(path)
        assert err is None
        assert data == {"voicevox": {"speaker": 5}}

    def test_missing_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        data, err = cfg._load_vvread_settings(path)
        assert err is None
        assert data == {}

    def test_invalid_json_returns_error(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        path.write_text("{ broken", encoding="utf-8")
        data, err = cfg._load_vvread_settings(path)
        assert data is None
        assert err is not None
        assert "JSON" in err


# ---------------------------------------------------------------------------
# _unflatten / _flatten
# ---------------------------------------------------------------------------


class TestFlattenUnflatten:
    def test_unflatten(self):
        result = cfg._unflatten({"voicevox.speaker": 3, "voicevox.speed": 1.5})
        assert result == {"voicevox": {"speaker": 3, "speed": 1.5}}

    def test_flatten(self):
        result = cfg._flatten({"voicevox": {"speaker": 3, "speed": 1.5}})
        assert result == {"voicevox.speaker": 3, "voicevox.speed": 1.5}

    def test_roundtrip(self):
        original = {"voicevox": {"speaker": 3, "speed": 1.5, "engineUrl": "http://x"}}
        assert cfg._unflatten(cfg._flatten(original)) == original


# ---------------------------------------------------------------------------
# _prompt_field
# ---------------------------------------------------------------------------


_DESC = "# テスト\n説明文です。"


class TestPromptField:
    def test_enter_keeps_current_value(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="\n")
        result = cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert result == 3

    def test_valid_int_input(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="11\n")
        result = cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert result == 11

    def test_valid_float_input(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="2.0\n")
        result = cfg._prompt_field("voicevox.speed", "Speed", float, 1.5, _DESC, ctx=ctx)
        assert result == 2.0

    def test_invalid_then_valid(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="abc\n5\n")
        result = cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert result == 5
        assert "Invalid" in out.getvalue()

    def test_str_field_accepts_any_string(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="http://example.com:50021\n")
        result = cfg._prompt_field("voicevox.engineUrl", "VOICEVOX Engine URL", str,
                                   "http://127.0.0.1:50021", _DESC, ctx=ctx)
        assert result == "http://example.com:50021"

    def test_description_is_shown(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="\n")
        cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert "# テスト" in out.getvalue()

    def test_n_uppercase_returns_clear(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="N\n")
        result = cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert result is cfg._CLEAR

    def test_n_lowercase_returns_clear(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="n\n")
        result = cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert result is cfg._CLEAR

    def test_prompt_hint_contains_clear(self, tmp_path):
        ctx, out, _ = _make_ctx(tmp_path, input_text="\n")
        cfg._prompt_field("voicevox.speaker", "Speaker ID", int, 3, _DESC, ctx=ctx)
        assert "N=クリア" in out.getvalue()


# ---------------------------------------------------------------------------
# run_config
# ---------------------------------------------------------------------------


class TestRunConfig:
    def test_non_tty_exits_1(self, tmp_path):
        ctx, out, err = _make_ctx(tmp_path, tty=False)
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "エラー" in err.getvalue()
        assert "対話型端末" in err.getvalue()

    def test_no_settings_file_exits_1(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"), \
             patch.object(cfg._hs, "get_vvread_hook_status", return_value="none"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, input_text="")
            rc = cfg.run_config(ctx)
        assert rc == 1
        assert "設定ファイルが見つかりません" in err.getvalue()
        assert "vvread config --create" in err.getvalue()
        assert "vvread setup" in err.getvalue()

    def test_create_creates_empty_settings(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            # 全フィールド Enter（変更なし）で設定ファイルが作成されることを確認
            input_text = "\n" * len(cfg.CONFIG_FIELDS)
            ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text, create=True)
            rc = cfg.run_config(ctx)
        path = tmp_path / "vvread.settings.json"
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == {}
        assert rc == 0

    def test_create_dry_run_no_creation(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, create=True, dry_run=True)
            rc = cfg.run_config(ctx)
        assert rc == 0
        assert not (tmp_path / "vvread.settings.json").exists()
        assert "DRY-RUN" in out.getvalue()

    def test_create_does_not_overwrite_existing(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 99}})
        original = path.read_text(encoding="utf-8")
        # 全フィールド Enter（変更なし）でキャンセル
        input_text = "\n" * len(cfg.CONFIG_FIELDS)
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text, create=True)
        cfg.run_config(ctx)
        assert path.read_text(encoding="utf-8") == original

    def test_non_tty_error_is_japanese(self, tmp_path):
        ctx, out, err = _make_ctx(tmp_path, tty=False)
        rc = cfg.run_config(ctx)
        assert rc == 1
        stderr_text = err.getvalue()
        assert "エラー:" in stderr_text
        assert "対話型端末" in stderr_text

    def test_broken_json_exits_1(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        path.write_text("{ broken", encoding="utf-8")
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text="")
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_no_change_exits_0(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        # 全フィールドで Enter（変更なし）
        input_text = "\n" * len(cfg.CONFIG_FIELDS)
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert "変更なし" in out.getvalue()

    def test_saves_changes(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "speed": 1.5}})
        # engineUrl=Enter, speaker=11, 残り9フィールド=Enter, 保存=Y
        n_fields = len(cfg.CONFIG_FIELDS)
        input_text = "\n11\n" + "\n" * (n_fields - 2) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["voicevox"]["speaker"] == 11

    def test_bak_created_before_save(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        original = path.read_text(encoding="utf-8")
        n_fields = len(cfg.CONFIG_FIELDS)
        input_text = "\n11\n" + "\n" * (n_fields - 2) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        cfg.run_config(ctx)
        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == original

    def test_cancel_does_not_save(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        original = path.read_text(encoding="utf-8")
        n_fields = len(cfg.CONFIG_FIELDS)
        input_text = "\n11\n" + "\n" * (n_fields - 2) + "N\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert path.read_text(encoding="utf-8") == original
        assert "キャンセル" in out.getvalue()

    def test_interactive_engine_url_list_does_not_crash(self, tmp_path):
        """F-116: engineUrl が list 形式（engines の legacy alias）の設定ファイルで
        対話 config が AttributeError でクラッシュせず、保存で engines に統一され
        engineUrl が消える。実機 claudeBot プロジェクトで発見したクラッシュの回帰。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"engineUrl": [
            "http://127.0.0.1:50021", "http://127.0.0.1:50022",
        ]}})
        # 全フィールド Enter（current 維持）+ 保存 Y
        input_text = "\n" * len(cfg.CONFIG_FIELDS) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0, err.getvalue()
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == [
            "http://127.0.0.1:50021", "http://127.0.0.1:50022",
        ]
        assert "engineUrl" not in data["voicevox"]

    def test_dry_run_does_not_write(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        original = path.read_text(encoding="utf-8")
        n_fields = len(cfg.CONFIG_FIELDS)
        input_text = "\n11\n" + "\n" * (n_fields - 2) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text, dry_run=True)
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert path.read_text(encoding="utf-8") == original
        assert "dry-run" in out.getvalue()

    def test_edit_alias_dispatches_same_script(self, tmp_path):
        """bin/vvread edit が config.sh に dispatch されることを確認。"""
        result = subprocess.run(
            [str(REPO / "bin" / "vvread"), "edit", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # 終了コード 0（--help は正常終了）
        assert result.returncode == 0
        assert "dry-run" in result.stdout.lower() or "usage" in result.stdout.lower()


# ---------------------------------------------------------------------------
# _find_settings_file (user_setting)
# ---------------------------------------------------------------------------


class TestUserSetting:
    def test_user_setting_returns_user_path(self, tmp_path):
        user_path = tmp_path / "user_settings.json"
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            result = cfg._find_settings_file(tmp_path, user_setting=True)
        assert result is not None
        label, path = result
        assert label == "user"
        assert path == user_path

    def test_user_setting_ignores_project(self, tmp_path):
        project = tmp_path / "vvread.settings.json"
        user_path = tmp_path / "user_settings.json"
        _write_settings(project, {})
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            result = cfg._find_settings_file(tmp_path, user_setting=True)
        assert result is not None
        label, _ = result
        assert label == "user"

    def test_user_setting_returns_none_when_missing(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            result = cfg._find_settings_file(tmp_path, user_setting=True)
        assert result is None


# ---------------------------------------------------------------------------
# --user-setting interactive + --create
# ---------------------------------------------------------------------------


class TestUserSettingCreate:
    def test_user_setting_create_targets_user_file(self, tmp_path):
        user_path = tmp_path / "user" / "settings.json"
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            with patch.object(cfg._stg, "project_settings_path",
                              return_value=tmp_path / "vvread.settings.json"):
                input_text = "\n" * len(cfg.CONFIG_FIELDS)
                ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text,
                                          create=True, user_setting=True)
                rc = cfg.run_config(ctx)
        assert rc == 0
        assert user_path.exists()
        assert json.loads(user_path.read_text(encoding="utf-8")) == {}


# ---------------------------------------------------------------------------
# B-107 + B-109 統合: --user-setting + --set
# ---------------------------------------------------------------------------


class TestUserSettingWithSet:
    def test_writes_to_user_file(self, tmp_path):
        user_path = tmp_path / "user_settings.json"
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            ctx, out, err = _make_ctx(tmp_path, tty=False,
                                      user_setting=True, set_pairs=["voicevox.speaker=8"])
            rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(user_path)
        assert data["voicevox"]["speaker"] == 8
        assert "Updated:" in out.getvalue()

    def test_does_not_touch_project_file(self, tmp_path):
        project = tmp_path / "vvread.settings.json"
        user_path = tmp_path / "user_settings.json"
        _write_settings(project, {"voicevox": {"speaker": 3}})
        _write_settings(user_path, {})
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            ctx, out, err = _make_ctx(tmp_path, tty=False,
                                      user_setting=True, set_pairs=["voicevox.speaker=8"])
            cfg.run_config(ctx)
        assert _read_settings(project)["voicevox"]["speaker"] == 3


# ---------------------------------------------------------------------------
# 親ディレクトリ自動作成
# ---------------------------------------------------------------------------


class TestUserSettingParentDir:
    def test_creates_parent_dir(self, tmp_path):
        user_path = tmp_path / "deep" / "nested" / "settings.json"
        with patch.object(cfg._stg, "user_settings_path", return_value=user_path):
            ctx, out, err = _make_ctx(tmp_path, tty=False,
                                      user_setting=True, set_pairs=["voicevox.speaker=3"])
            rc = cfg.run_config(ctx)
        assert rc == 0
        assert user_path.exists()


# ---------------------------------------------------------------------------
# --set フラグ
# ---------------------------------------------------------------------------


class TestSetFlag:
    def test_writes_float(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speed=2.0"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert _read_settings(path)["voicevox"]["speed"] == 2.0

    def test_writes_int(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speaker=8"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert _read_settings(path)["voicevox"]["speaker"] == 8

    def test_writes_str(self, tmp_path):
        """--set voicevox.engineUrl → canonicalize で engines 配列に変換される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.engineUrl=http://x:50021"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert "engineUrl" not in data.get("voicevox", {})
        assert data["voicevox"]["engines"] == ["http://x:50021"]

    def test_no_tty_required(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, tty=False, set_pairs=["voicevox.speaker=3"])
        rc = cfg.run_config(ctx)
        assert rc == 0


class TestSetFlagMultiple:
    def test_multiple_keys(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path,
                                  set_pairs=["voicevox.speed=2.0", "voicevox.speaker=8"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["voicevox"]["speed"] == 2.0
        assert data["voicevox"]["speaker"] == 8

    def test_same_key_last_wins(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path,
                                  set_pairs=["voicevox.speaker=3", "voicevox.speaker=99"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert _read_settings(path)["voicevox"]["speaker"] == 99


class TestSetFlagUnknownKey:
    def test_unknown_key_saved_as_string(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["custom.key=hello"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["custom"]["key"] == "hello"


class TestSetFlagErrors:
    def test_no_equals_sign(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speed"])
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_type_coerce_failure(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speed=abc"])
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_empty_value(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speed="])
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_empty_key(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["=abc"])
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_leading_dot(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=[".voicevox.speed=2.0"])
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_trailing_dot(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.=2.0"])
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_consecutive_dots(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox..speed=2.0"])
        rc = cfg.run_config(ctx)
        assert rc == 1


# ---------------------------------------------------------------------------
# --json フラグ
# ---------------------------------------------------------------------------


class TestJsonFlag:
    def test_writes_nested_object(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path,
                                  json_patch='{"voicevox": {"speed": 2.0, "speaker": 8}}')
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["voicevox"]["speed"] == 2.0
        assert data["voicevox"]["speaker"] == 8

    def test_int_accepted_for_float_field(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": {"speed": 2}}')
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert _read_settings(path)["voicevox"]["speed"] == 2.0


class TestJsonFlagDeepMerge:
    def test_sibling_keys_preserved(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speed": 1.5, "speaker": 3}})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": {"speed": 2.0}}')
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["voicevox"]["speed"] == 2.0
        assert data["voicevox"]["speaker"] == 3  # 消えない

    def test_unknown_object_preserved(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"custom": {"key": "value"}})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": {"speed": 2.0}}')
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["voicevox"]["speed"] == 2.0
        assert data["custom"]["key"] == "value"  # unknown object 保持

    def test_unknown_section_in_patch_preserved(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path,
                                  json_patch='{"voicevox": {"speed": 2.0}, "myapp": {"x": 1}}')
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = _read_settings(path)
        assert data["myapp"]["x"] == 1


class TestJsonFlagErrors:
    def test_invalid_json(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch="{broken")
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_type_mismatch(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": {"speed": "fast"}}')
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_toplevel_array(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='[1, 2, 3]')
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_toplevel_scalar(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='42')
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_null_value(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path,
                                  json_patch='{"voicevox": {"speed": null}}')
        rc = cfg.run_config(ctx)
        assert rc == 1

    def test_known_section_non_object(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": 1}')
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()


# ---------------------------------------------------------------------------
# --json + --set 併用: マージ順序
# ---------------------------------------------------------------------------


class TestMergeOrder:
    def test_set_overwrites_json(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(
            tmp_path,
            json_patch='{"voicevox": {"speaker": 3}}',
            set_pairs=["voicevox.speaker=99"],
        )
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert _read_settings(path)["voicevox"]["speaker"] == 99


# ---------------------------------------------------------------------------
# 非 TTY での --set 成功
# ---------------------------------------------------------------------------


class TestNonTtyWrite:
    def test_non_tty_with_set_succeeds(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, tty=False, set_pairs=["voicevox.speaker=3"])
        rc = cfg.run_config(ctx)
        assert rc == 0

    def test_non_tty_without_set_fails(self, tmp_path):
        ctx, out, err = _make_ctx(tmp_path, tty=False)
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "エラー" in err.getvalue()


# ---------------------------------------------------------------------------
# 自動新規作成（--create 不要）
# ---------------------------------------------------------------------------


class TestAutoCreate:
    def test_set_creates_file_when_missing(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speaker=8"])
            rc = cfg.run_config(ctx)
        path = tmp_path / "vvread.settings.json"
        assert rc == 0
        assert path.exists()
        assert _read_settings(path)["voicevox"]["speaker"] == 8

    def test_json_creates_file_when_missing(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path,
                                      json_patch='{"voicevox": {"speed": 2.0}}')
            rc = cfg.run_config(ctx)
        path = tmp_path / "vvread.settings.json"
        assert rc == 0
        assert path.exists()
        assert _read_settings(path)["voicevox"]["speed"] == 2.0


class TestCreateWithSet:
    def test_create_flag_ignored_in_non_interactive(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, create=True,
                                      set_pairs=["voicevox.speaker=8"])
            rc = cfg.run_config(ctx)
        path = tmp_path / "vvread.settings.json"
        assert rc == 0
        assert path.exists()
        assert _read_settings(path)["voicevox"]["speaker"] == 8


# ---------------------------------------------------------------------------
# dry-run（非対話モード）
# ---------------------------------------------------------------------------


class TestDryRunNonInteractive:
    def test_file_not_modified(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        original = path.read_text(encoding="utf-8")
        ctx, out, err = _make_ctx(tmp_path, dry_run=True,
                                  set_pairs=["voicevox.speaker=99"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert path.read_text(encoding="utf-8") == original

    def test_diff_shown_in_stdout(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        ctx, out, err = _make_ctx(tmp_path, dry_run=True,
                                  set_pairs=["voicevox.speaker=99"])
        cfg.run_config(ctx)
        output = out.getvalue()
        assert "voicevox.speaker" in output
        assert "3" in output
        assert "99" in output

    def test_unset_key_shows_unset(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, dry_run=True,
                                  set_pairs=["voicevox.speed=2.0"])
        cfg.run_config(ctx)
        assert "<unset>" in out.getvalue()


class TestDryRunAutoCreate:
    def test_would_create_shown(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, dry_run=True,
                                      set_pairs=["voicevox.speaker=8"])
            rc = cfg.run_config(ctx)
        assert rc == 0
        assert "Would create:" in out.getvalue()
        assert not (tmp_path / "vvread.settings.json").exists()


# ---------------------------------------------------------------------------
# unknown keys 保持
# ---------------------------------------------------------------------------


class TestUnknownKeysPreserved:
    def test_set_preserves_unknown_keys(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}, "unknown": {"k": "v"}})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speaker=8"])
        cfg.run_config(ctx)
        data = _read_settings(path)
        assert data["unknown"]["k"] == "v"

    def test_json_preserves_unknown_keys(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}, "unknown": {"k": "v"}})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox": {"speaker": 8}}')
        cfg.run_config(ctx)
        data = _read_settings(path)
        assert data["unknown"]["k"] == "v"


# ---------------------------------------------------------------------------
# Updated メッセージ
# ---------------------------------------------------------------------------


class TestUpdatedMessage:
    def test_updated_path_shown(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speaker=3"])
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert "Updated:" in out.getvalue()

    def test_no_extra_output_on_success(self, tmp_path):
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.speaker=3"])
        cfg.run_config(ctx)
        lines = [l for l in out.getvalue().strip().splitlines() if l]
        assert len(lines) == 1
        assert lines[0].startswith("Updated:")


# ---------------------------------------------------------------------------
# --list フラグ
# ---------------------------------------------------------------------------


class TestListFlag:
    def test_list_exits_zero(self, tmp_path):
        out = io.StringIO()
        ctx = cfg.ConfigContext(list_mode=True, cwd=tmp_path, out_stream=out, err_stream=io.StringIO())
        rc = cfg.run_config(ctx)
        assert rc == 0

    def test_list_shows_max_chunks(self, tmp_path):
        out = io.StringIO()
        ctx = cfg.ConfigContext(list_mode=True, cwd=tmp_path, out_stream=out, err_stream=io.StringIO())
        cfg.run_config(ctx)
        assert "voicevox.maxChunks" in out.getvalue()

    def test_list_shows_all_schema_keys(self, tmp_path):
        out = io.StringIO()
        ctx = cfg.ConfigContext(list_mode=True, cwd=tmp_path, out_stream=out, err_stream=io.StringIO())
        cfg.run_config(ctx)
        output = out.getvalue()
        import settings as _stg
        for key in _stg.SCHEMA:
            assert key in output

    def test_list_works_without_tty(self, tmp_path):
        # 非TTY環境（CI など）でも動作する
        out = io.StringIO()
        err = io.StringIO()
        in_stream = io.StringIO()  # isatty() = False
        ctx = cfg.ConfigContext(
            list_mode=True, cwd=tmp_path,
            in_stream=in_stream, out_stream=out, err_stream=err,
        )
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert "voicevox.maxChunks" in out.getvalue()


# ---------------------------------------------------------------------------
# F-112: hook 状態による project settings 自動作成
# ---------------------------------------------------------------------------


class TestF112HookStatusAutoCreate:
    """F-112: vvread config が hook 状態に応じて settings ファイル未存在時の挙動を変える"""

    def _make_ctx_no_user_settings(self, tmp_path, **kwargs):
        """user settings が見つからない状態で ConfigContext を作る。"""
        ctx, out, err = _make_ctx(tmp_path, **kwargs)
        return ctx, out, err

    def test_modern_hook_dry_run_shows_dry_run_message(self, tmp_path):
        """settings なし + modern hook + --dry-run → DRY-RUN 表示 + exit 0"""
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="modern"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, dry_run=True)
            rc = cfg.run_config(ctx)
        assert rc == 0
        assert "DRY-RUN" in out.getvalue()
        assert not (tmp_path / "vvread.settings.json").exists()

    def test_no_hook_shows_setup_install_hint(self, tmp_path):
        """settings なし + hook なし → setup/install 案内 + exit 1"""
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="none"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True)
            rc = cfg.run_config(ctx)
        assert rc == 1
        assert "vvread setup" in err.getvalue()
        assert "vvread install" in err.getvalue()
        assert "vvread config --create" in err.getvalue()

    def test_legacy_hook_shows_migration_hint(self, tmp_path):
        """settings なし + legacy hook → 移行案内 + exit 1"""
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="legacy"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True)
            rc = cfg.run_config(ctx)
        assert rc == 1
        assert "vvread uninstall" in err.getvalue()

    def test_user_setting_with_modern_hook_no_auto_create(self, tmp_path):
        """--user-setting + settings なし + modern hook → 自動 create しない"""
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="modern"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, user_setting=True)
            rc = cfg.run_config(ctx)
        assert rc == 1
        # user-scope 向けメッセージを確認
        assert "--user-setting --create" in err.getvalue()
        # modern hook による自動作成をしていない
        assert not (tmp_path / "vvread.settings.json").exists()

    def test_user_setting_with_legacy_hook_no_migration_hint(self, tmp_path):
        """--user-setting + settings なし + legacy hook → legacy 移行案内にならない"""
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="legacy"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, user_setting=True)
            rc = cfg.run_config(ctx)
        assert rc == 1
        assert "vvread uninstall" not in err.getvalue()
        assert "--user-setting --create" in err.getvalue()

    def test_modern_hook_auto_create_reaches_editor(self, tmp_path):
        """modern hook + settings なし → auto-create 後に対話エディタに到達する
        TTY ありの non-dry-run: auto-create → settings.json 作成 → 対話フロー開始
        対話フローに到達した証拠: "設定ファイル:" が out に出力される
        """
        with patch.object(cfg._hs, "get_vvread_hook_status", return_value="modern"), \
             patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            # 全フィールド Enter（変更なし）で対話フローを通過
            input_text = "\n" * len(cfg.CONFIG_FIELDS)
            ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
            rc = cfg.run_config(ctx)
        # auto-create されて settings.json が存在する
        assert (tmp_path / "vvread.settings.json").exists()
        # 対話フロー到達: "設定ファイル:" が出力される
        assert "設定ファイル:" in out.getvalue()

    def test_existing_settings_unchanged(self, tmp_path):
        """settings あり → 既存挙動を維持（--set で非対話確認）"""
        settings = tmp_path / "vvread.settings.json"
        settings.write_text(json.dumps({"voicevox": {"speaker": 3}}), encoding="utf-8")
        ctx, out, err = _make_ctx(
            tmp_path, set_pairs=["voicevox.speaker=5"]
        )
        rc = cfg.run_config(ctx)
        assert rc == 0
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["voicevox"]["speaker"] == 5


# ---------------------------------------------------------------------------
# B-119: N 入力でキーをクリアし JSONC コメントとして書き出す
# ---------------------------------------------------------------------------


class TestClearWithN:
    """B-119: N 入力でキーを JSONC コメントとして書き出す機能のテスト。

    CONFIG_FIELDS の順: engineUrl, speaker, volume, speed, pauseScale,
                        pitch, intonation, inlineCodeLimit, chunkChars,
                        chunkHardMax, maxChars, maxChunks
    """

    def _input_n_speaker(self) -> str:
        """field1=Enter, field2(speaker)=N, 残り=Enter, 確認=Y。"""
        n = len(cfg.CONFIG_FIELDS)
        return "\nN\n" + "\n" * (n - 2) + "Y\n"

    def test_n_input_writes_jsonc_comment(self, tmp_path):
        """N 入力でファイルに // \"speaker\": 行が含まれる。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "speed": 1.5}})
        ctx, out, err = _make_ctx(tmp_path, input_text=self._input_n_speaker())
        rc = cfg.run_config(ctx)
        assert rc == 0
        content = path.read_text(encoding="utf-8")
        assert '// "speaker":' in content

    def test_n_input_shown_in_summary(self, tmp_path):
        """サマリに「クリア」が出力される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 5}})
        ctx, out, err = _make_ctx(tmp_path, input_text=self._input_n_speaker())
        cfg.run_config(ctx)
        assert "クリア" in out.getvalue()

    def test_n_lowercase_also_clears(self, tmp_path):
        """小文字 n でも同じ動作。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        n = len(cfg.CONFIG_FIELDS)
        ctx, out, err = _make_ctx(tmp_path, input_text="\nn\n" + "\n" * (n - 2) + "Y\n")
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert '// "speaker":' in path.read_text(encoding="utf-8")

    def test_cleared_key_loads_as_absent(self, tmp_path):
        """JSONC ファイルを load_jsonc_file で読むとコメントキーは存在しない（JSONC valid 性）。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "speed": 1.5}})
        ctx, out, err = _make_ctx(tmp_path, input_text=self._input_n_speaker())
        cfg.run_config(ctx)
        data, err_msg = cfg._load_vvread_settings(path)
        assert err_msg is None
        assert "speaker" not in data.get("voicevox", {})
        assert data["voicevox"]["speed"] == 1.5

    def test_clear_preserves_other_keys(self, tmp_path):
        """N でクリアしたキー以外の設定は変更されない。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "speed": 1.8}})
        ctx, out, err = _make_ctx(tmp_path, input_text=self._input_n_speaker())
        cfg.run_config(ctx)
        data, _ = cfg._load_vvread_settings(path)
        assert data["voicevox"]["speed"] == 1.8

    def test_all_keys_in_section_cleared(self, tmp_path):
        """セクション内の全キーをクリアしても load_jsonc_file で読める（JSONC valid 性）。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3}})
        n = len(cfg.CONFIG_FIELDS)
        ctx, out, err = _make_ctx(tmp_path, input_text="N\n" * n + "Y\n")
        cfg.run_config(ctx)
        data, err_msg = cfg._load_vvread_settings(path)
        assert err_msg is None

    def test_multiple_keys_cleared(self, tmp_path):
        """複数キーを N でクリアしても保存後に load_jsonc_file で読める。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "volume": 1.0, "speed": 1.5}})
        n = len(cfg.CONFIG_FIELDS)
        # engineUrl=Enter, speaker=N, volume=N, 残り=Enter, 確認=Y
        ctx, out, err = _make_ctx(
            tmp_path, input_text="\nN\nN\n" + "\n" * (n - 3) + "Y\n"
        )
        rc = cfg.run_config(ctx)
        assert rc == 0
        data, err_msg = cfg._load_vvread_settings(path)
        assert err_msg is None
        assert "speaker" not in data.get("voicevox", {})
        assert "volume" not in data.get("voicevox", {})

    def test_set_flag_does_not_preserve_jsonc_comment(self, tmp_path):
        """--set による非対話書き込み後はコメント行が保持されない（制約の明示）。

        NOTE: 対話式 config で作成した // コメント行は、--set 書き込み後に消える。
        _save_vvread_settings() の docstring にも記載済み。
        """
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"speaker": 3, "speed": 1.5}})
        # 対話式で speaker をクリア
        ctx, out, err = _make_ctx(tmp_path, input_text=self._input_n_speaker())
        cfg.run_config(ctx)
        assert '// "speaker":' in path.read_text(encoding="utf-8")
        # --set で非対話書き込み → コメント行が消える
        ctx2, out2, err2 = _make_ctx(tmp_path, set_pairs=["voicevox.speed=2.0"])
        cfg.run_config(ctx2)
        assert '// "speaker":' not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# U-117: engines 統一テスト
# ---------------------------------------------------------------------------


class TestEnginesUnification:
    """U-117: vvread config が保存時に engines のみに統一することを検証。"""

    def test_old_engine_url_migrates_to_engines_on_enter(self, tmp_path):
        """旧 engineUrl のみのファイルを対話で Enter → engines=[A] のみに移行。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"engineUrl": "http://127.0.0.1:50099"}})
        n = len(cfg.CONFIG_FIELDS)
        # 全フィールド Enter（維持）+ 確認 Y
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text="\n" * n + "Y\n")
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == ["http://127.0.0.1:50099"]
        assert "engineUrl" not in data["voicevox"]

    def test_json_engine_url_converts_to_engines(self, tmp_path):
        """--json で engineUrl を指定 → canonicalize で engines に変換される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(
            tmp_path,
            json_patch='{"voicevox":{"engineUrl":"http://127.0.0.1:50099"}}',
        )
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == ["http://127.0.0.1:50099"]
        assert "engineUrl" not in data["voicevox"]

    def test_engines_and_engine_url_coexist_engines_wins(self, tmp_path):
        """既存ファイルに engineUrl + engines 両方 → engines 優先、engineUrl 削除。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {
            "engineUrl": "http://old:50021",
            "engines": ["http://new:50021", "http://new2:50022"],
        }})
        # 対話で Enter のみ（変更なし）
        n = len(cfg.CONFIG_FIELDS)
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text="\n" * n + "Y\n")
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == ["http://new:50021", "http://new2:50022"]
        assert "engineUrl" not in data["voicevox"]

    def test_interactive_comma_separated_input(self, tmp_path):
        """対話でカンマ区切り URL 入力 → engines 配列に変換される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {}})
        n = len(cfg.CONFIG_FIELDS)
        # 1フィールド目(engines)にカンマ区切り入力、残りは Enter、確認 Y
        input_text = "http://127.0.0.1:50021, http://127.0.0.1:50022\n" + "\n" * (n - 1) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == [
            "http://127.0.0.1:50021",
            "http://127.0.0.1:50022",
        ]
        assert "engineUrl" not in data["voicevox"]

    def test_interactive_invalid_scheme_reprompts(self, tmp_path):
        """対話で不正 scheme (ftp://) → 再入力プロンプトが表示される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {}})
        n = len(cfg.CONFIG_FIELDS)
        # 1回目 ftp:// → 再入力, 2回目 正常 → 残り Enter, 確認 Y
        input_text = "ftp://127.0.0.1:50021\nhttp://127.0.0.1:50021\n" + "\n" * (n - 1) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        assert "ftp://127.0.0.1:50021" in out.getvalue() or "無効" in out.getvalue()

    def test_n_clear_removes_both_engines_and_engine_url(self, tmp_path):
        """N クリアで voicevox.engines も voicevox.engineUrl も削除される（disk まで通す）。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {
            "engineUrl": "http://127.0.0.1:50021",
            "engines": ["http://127.0.0.1:50021"],
        }})
        n = len(cfg.CONFIG_FIELDS)
        # 1フィールド目(engines) を N でクリア、残り Enter、確認 Y
        input_text = "N\n" + "\n" * (n - 1) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"
        # JSONC ファイルなので JSONC-aware に読む
        data, err_msg = cfg._load_vvread_settings(path)
        assert err_msg is None
        assert "engines" not in data.get("voicevox", {})
        assert "engineUrl" not in data.get("voicevox", {})

    def test_n_clear_comment_contains_only_engines_not_engine_url(self, tmp_path):
        """N クリア時の JSONC コメントは engines のみ。engineUrl はコメントにも現れない。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"engines": ["http://127.0.0.1:50021"]}})
        n = len(cfg.CONFIG_FIELDS)
        input_text = "N\n" + "\n" * (n - 1) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        cfg.run_config(ctx)
        content = path.read_text(encoding="utf-8")
        assert "engineUrl" not in content
        assert '// "engines":' in content

    def test_n_clear_engines_file_reparseable(self, tmp_path):
        """N クリア後のファイルが JSONC として再パースできる（list 型コメント）。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {
            "engines": ["http://127.0.0.1:50021"],
            "speaker": 3,
        }})
        n = len(cfg.CONFIG_FIELDS)
        input_text = "N\n" + "\n" * (n - 1) + "Y\n"
        ctx, out, err = _make_ctx(tmp_path, tty=True, input_text=input_text)
        cfg.run_config(ctx)
        data, err_msg = cfg._load_vvread_settings(path)
        assert err_msg is None, f"再パース失敗: {err_msg}"

    def test_set_engines_shows_error(self, tmp_path):
        """--set voicevox.engines=... はエラー + --json 案内。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, set_pairs=["voicevox.engines=http://127.0.0.1:50021"])
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "--json" in err.getvalue()

    def test_json_engines_empty_array_returns_error(self, tmp_path):
        """--json で engines=[] は canonicalize がエラーを返す。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {})
        ctx, out, err = _make_ctx(tmp_path, json_patch='{"voicevox":{"engines":[]}}')
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_list_mode_shows_engines_comma_separated(self, tmp_path):
        """config --list で voicevox.engines がカンマ区切りで表示される。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"engines": [
            "http://127.0.0.1:50021", "http://127.0.0.1:50022"
        ]}})
        ctx, out, err = _make_ctx(tmp_path, list_mode=True)
        rc = cfg.run_config(ctx)
        assert rc == 0
        assert "http://127.0.0.1:50021, http://127.0.0.1:50022" in out.getvalue()

    def test_list_mode_engine_url_list_does_not_crash(self, tmp_path):
        """F-116: engineUrl が list 形式（engines の legacy alias）でも
        config --list がクラッシュせず engines 行を正しくレンダリングする。"""
        path = tmp_path / "vvread.settings.json"
        _write_settings(path, {"voicevox": {"engineUrl": [
            "http://127.0.0.1:50021", "http://127.0.0.1:50022",
        ]}})
        ctx, out, err = _make_ctx(tmp_path, list_mode=True)
        rc = cfg.run_config(ctx)
        assert rc == 0, err.getvalue()
        assert "http://127.0.0.1:50021, http://127.0.0.1:50022" in out.getvalue()


# ---------------------------------------------------------------------------
# B-124: voicevox.engines の config --json 導線 E2E テスト
# ---------------------------------------------------------------------------


class TestEnginesConfigChain:
    """config --json → settings.py list/env の engines 配列 E2E 検証。"""

    ENGINES_JSON = '{"voicevox":{"engines":["http://127.0.0.1:50021","http://127.0.0.1:50022"]}}'

    def test_json_flag_saves_engines_list(self, tmp_path):
        """config --json で engines 配列が settings ファイルに保存される。"""
        nonexistent_user = tmp_path / "nonexistent_user.json"
        with patch.object(cfg._stg, "user_settings_path", return_value=nonexistent_user):
            ctx, out, err = _make_ctx(tmp_path, json_patch=self.ENGINES_JSON, create=True)
            rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"

        path = tmp_path / "vvread.settings.json"
        data = _read_settings(path)
        assert data["voicevox"]["engines"] == [
            "http://127.0.0.1:50021",
            "http://127.0.0.1:50022",
        ]

    def test_json_flag_dry_run_shows_diff(self, tmp_path):
        """config --json --dry-run が差分を表示するだけで保存しない。"""
        nonexistent_user = tmp_path / "nonexistent_user.json"
        with patch.object(cfg._stg, "user_settings_path", return_value=nonexistent_user):
            ctx, out, err = _make_ctx(tmp_path, json_patch=self.ENGINES_JSON,
                                      dry_run=True, create=True)
            rc = cfg.run_config(ctx)
        assert rc == 0, f"err={err.getvalue()}"

        path = tmp_path / "vvread.settings.json"
        assert not path.exists(), "dry-run なのにファイルが作成された"

    def test_settings_py_resolves_engines_from_saved_file(self, tmp_path):
        """config --json で保存した engines を settings.py がリストとして解決する。"""
        import settings as stg

        nonexistent_user = tmp_path / "nonexistent_user.json"
        with patch.object(cfg._stg, "user_settings_path", return_value=nonexistent_user):
            ctx, _, _ = _make_ctx(tmp_path, json_patch=self.ENGINES_JSON, create=True)
            cfg.run_config(ctx)

        s = stg.load(cwd=tmp_path, env={}, user_path=nonexistent_user)
        rv = s.get("voicevox.engines")
        assert rv is not None
        assert rv.value == ["http://127.0.0.1:50021", "http://127.0.0.1:50022"]
        assert rv.origin.source == "project"

    def test_settings_py_env_exports_semicolon_separated(self, tmp_path):
        """settings.py env が VOICEVOX_ENGINES を ';' 区切りで export する。"""
        import subprocess as _sp

        ctx, _, _ = _make_ctx(tmp_path, json_patch=self.ENGINES_JSON, create=True)
        cfg.run_config(ctx)

        env_clean = {k: v for k, v in __import__("os").environ.items()
                     if not k.startswith("VOICEVOX_") and not k.startswith("VVREAD_")}

        r = _sp.run(
            [sys.executable, str(REPO / "scripts" / "settings.py"), "env"],
            capture_output=True, text=True,
            env=env_clean,
            cwd=str(tmp_path),
        )
        assert r.returncode == 0
        assert "VOICEVOX_ENGINES='http://127.0.0.1:50021;http://127.0.0.1:50022'" in r.stdout


# ---------------------------------------------------------------------------
# --project flag テスト (B-133)
# ---------------------------------------------------------------------------


class TestProjectFlag:
    """--project flag: user settings にフォールバックせず project ファイルを強制使用。"""

    def test_project_scope_writes_to_project_file(self, tmp_path):
        """--project: user settings が存在しても project ファイルに書き込む。"""
        # user settings ファイルを作成
        user_settings = tmp_path / "user_settings.json"
        user_settings.write_text('{"voicevox": {"speaker": 99}}', encoding="utf-8")

        project_file = tmp_path / "vvread.settings.json"
        assert not project_file.exists()

        with patch.object(cfg._stg, "user_settings_path", return_value=user_settings):
            ctx = cfg.ConfigContext(
                project_scope=True,
                set_pairs=["voicevox.speaker=3"],
                cwd=tmp_path,
            )
            rc = cfg.run_config(ctx)

        assert rc == 0
        assert project_file.exists(), "--project なのに project ファイルが作成されなかった"
        data = json.loads(project_file.read_text(encoding="utf-8"))
        assert data.get("voicevox", {}).get("speaker") == 3
        # user settings は変更されていない
        user_data = json.loads(user_settings.read_text(encoding="utf-8"))
        assert user_data.get("voicevox", {}).get("speaker") == 99

    def test_project_scope_creates_project_file_if_absent(self, tmp_path):
        """--project: project ファイルが存在しない場合は新規作成する。"""
        nonexistent_user = tmp_path / "no_user.json"
        project_file = tmp_path / "vvread.settings.json"

        with patch.object(cfg._stg, "user_settings_path", return_value=nonexistent_user):
            ctx = cfg.ConfigContext(
                project_scope=True,
                set_pairs=["voicevox.speaker=5"],
                cwd=tmp_path,
            )
            rc = cfg.run_config(ctx)

        assert rc == 0
        assert project_file.exists()
        data = json.loads(project_file.read_text(encoding="utf-8"))
        assert data.get("voicevox", {}).get("speaker") == 5

    def test_project_and_user_setting_mutually_exclusive(self, tmp_path):
        """--project と --user-setting の同時指定は argparse usage error (exit 2)。"""
        import pytest
        with pytest.raises(SystemExit) as exc_info:
            cfg.main(["--project", "--user-setting", "--set", "voicevox.speaker=3"])
        assert exc_info.value.code == 2

    def test_no_project_flag_falls_back_to_user_settings(self, tmp_path):
        """--project なし: user settings が存在する場合は user settings に書き込む（既存動作）。"""
        user_settings = tmp_path / "user_settings.json"
        user_settings.write_text('{"voicevox": {"speaker": 99}}', encoding="utf-8")

        with patch.object(cfg._stg, "user_settings_path", return_value=user_settings):
            ctx = cfg.ConfigContext(
                set_pairs=["voicevox.speaker=3"],
                cwd=tmp_path,
            )
            rc = cfg.run_config(ctx)

        assert rc == 0
        user_data = json.loads(user_settings.read_text(encoding="utf-8"))
        assert user_data.get("voicevox", {}).get("speaker") == 3


# ---------------------------------------------------------------------------
# vvread_config_set MCP tool テスト (B-133)
# ---------------------------------------------------------------------------

import importlib.util as _importlib_util
_MCP_AVAILABLE_CONFIG = _importlib_util.find_spec("mcp") is not None


@pytest.mark.skipif(not _MCP_AVAILABLE_CONFIG, reason="mcp package required")
class TestVvreadConfigSetMcpTool:
    """mcp_server.py の vvread_config_set tool のテスト。"""

    def _load_mcp_server(self):
        spec = _importlib_util.spec_from_file_location(
            f"mcp_server_config_{id(self)}", str(REPO / "scripts" / "mcp_server.py")
        )
        mod = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _call_tool(self, mod, **kwargs):
        import asyncio
        return asyncio.run(
            mod.mcp._tool_manager.call_tool("vvread_config_set", kwargs)
        )

    def test_allowlist_excludes_engine_url(self):
        mod = self._load_mcp_server()
        assert "voicevox.engineUrl" not in mod._CONFIG_ALLOWLIST
        assert "voicevox.engines" not in mod._CONFIG_ALLOWLIST

    def _call_tool_expect_error(self, mod, key, value):
        """エラーが発生することを確認するヘルパー。
        FastMCP は _tool_manager.call_tool で ToolError を raise するため
        try/except で捕捉する。MCP プロトコル経由では isError=true になる。
        """
        import asyncio
        from mcp.server.fastmcp.exceptions import ToolError
        with pytest.raises((ToolError, RuntimeError)):
            asyncio.run(
                mod.mcp._tool_manager.call_tool(
                    "vvread_config_set", {"key": key, "value": value}
                )
            )

    def test_rejects_disallowed_key(self, tmp_path, monkeypatch):
        mod = self._load_mcp_server()
        self._call_tool_expect_error(mod, "voicevox.engineUrl", "http://evil/")

    def test_rejects_type_mismatch(self, tmp_path, monkeypatch):
        mod = self._load_mcp_server()
        self._call_tool_expect_error(mod, "voicevox.speaker", "not_a_number")

    def test_rejects_out_of_range(self, tmp_path, monkeypatch):
        mod = self._load_mcp_server()
        self._call_tool_expect_error(mod, "voicevox.speed", "99.0")

    def test_calls_config_set_with_project_flag(self, tmp_path, monkeypatch):
        """正常系: vvread config --set KEY=VALUE --project が subprocess で呼ばれる。"""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            import types
            return types.SimpleNamespace(returncode=0, stdout="Updated: /fake/path\n", stderr="")

        mod = self._load_mcp_server()
        mod.CWD = tmp_path
        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        import asyncio
        asyncio.run(
            mod.mcp._tool_manager.call_tool(
                "vvread_config_set", {"key": "voicevox.speaker", "value": "3"}
            )
        )
        assert calls, "subprocess.run が呼ばれなかった"
        cmd = calls[-1]
        assert "--project" in cmd, f"--project フラグが渡されなかった: {cmd}"
        assert "config" in cmd
        assert "--set" in cmd
        assert any("voicevox.speaker=3" in arg for arg in cmd)
