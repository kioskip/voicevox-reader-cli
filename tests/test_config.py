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
) -> tuple:
    """ConfigContext + out / err バッファを返す。"""
    out = io.StringIO()
    err = io.StringIO()
    in_stream = _tty_stream(input_text) if tty else _non_tty_stream()
    ctx = cfg.ConfigContext(
        dry_run=dry_run,
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
        assert "ERROR" in msg


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


# ---------------------------------------------------------------------------
# run_config
# ---------------------------------------------------------------------------


class TestRunConfig:
    def test_non_tty_exits_1(self, tmp_path):
        ctx, out, err = _make_ctx(tmp_path, tty=False)
        rc = cfg.run_config(ctx)
        assert rc == 1
        assert "ERROR" in err.getvalue()

    def test_no_settings_file_exits_1(self, tmp_path):
        with patch.object(cfg._stg, "user_settings_path",
                          return_value=tmp_path / "nonexistent.json"):
            ctx, out, err = _make_ctx(tmp_path, tty=True, input_text="")
            rc = cfg.run_config(ctx)
        assert rc == 1
        assert "install" in err.getvalue() or "No vvread" in err.getvalue()

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
