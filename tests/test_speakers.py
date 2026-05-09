"""scripts/speakers.py のテスト (B-017)

VOICEVOX Engine の /speakers API から話者一覧を取得・表示する機能をテスト。

テスト方針:
- Engine は実際には起動しない（モックで代替）
- defensive 処理（不正 payload でも落ちない）を確認
- API 返却順が保持されることを確認
"""
import io
import json
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import speakers as sp  # noqa: E402


# ---------------------------------------------------------------------------
# モック VOICEVOX サーバー
# ---------------------------------------------------------------------------

SAMPLE_SPEAKERS = [
    {
        "name": "ずんだもん",
        "speaker_uuid": "388f246b-8c41-4ac1-8e2d-5d79f3ff56d9",
        "styles": [
            {"name": "ノーマル", "id": 3},
            {"name": "あまあま", "id": 1},
            {"name": "ツンツン", "id": 7},
        ],
    },
    {
        "name": "四国めたん",
        "speaker_uuid": "7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff",
        "styles": [
            {"name": "ノーマル", "id": 2},
            {"name": "あまあま", "id": 0},
        ],
    },
]


def _make_mock_server(payload=None, status=200):
    """一時的な HTTP サーバーを起動して (url, shutdown_fn) を返す。"""
    if payload is None:
        payload = SAMPLE_SPEAKERS
    body = json.dumps(payload).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            pass  # サーバーログを抑制

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    url = f"http://127.0.0.1:{port}"
    return url, server.shutdown


# ---------------------------------------------------------------------------
# _fetch_speakers
# ---------------------------------------------------------------------------


class TestFetchSpeakers:
    def test_normal_response(self):
        url, shutdown = _make_mock_server(SAMPLE_SPEAKERS)
        try:
            result, err = sp._fetch_speakers(url)
            assert err is None
            assert isinstance(result, list)
            assert len(result) == 2
            assert result[0]["name"] == "ずんだもん"
        finally:
            shutdown()

    def test_engine_unreachable(self):
        result, err = sp._fetch_speakers("http://127.0.0.1:1")
        assert result is None
        assert err is not None

    def test_non_list_response(self):
        url, shutdown = _make_mock_server({"error": "not a list"})
        try:
            result, err = sp._fetch_speakers(url)
            assert result is None
            assert err is not None
            assert "unexpected" in err.lower() or "type" in err.lower()
        finally:
            shutdown()

    def test_invalid_json_response(self):
        class BadHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not json {{{")

            def log_message(self, format, *args):  # noqa: A002
                pass

        server = HTTPServer(("127.0.0.1", 0), BadHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        url = f"http://127.0.0.1:{port}"
        try:
            result, err = sp._fetch_speakers(url)
            assert result is None
            assert err is not None
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# _format_speakers
# ---------------------------------------------------------------------------


class TestFormatSpeakers:
    def test_normal_format(self):
        lines = sp._format_speakers(SAMPLE_SPEAKERS)
        assert len(lines) == 2
        assert lines[0] == "ずんだもん: 3: ノーマル, 1: あまあま, 7: ツンツン"
        assert lines[1] == "四国めたん: 2: ノーマル, 0: あまあま"

    def test_api_order_preserved(self):
        """styles は API 返却順を保持（ソートしない）"""
        speakers = [
            {
                "name": "テスト",
                "styles": [
                    {"name": "C", "id": 30},
                    {"name": "A", "id": 10},
                    {"name": "B", "id": 20},
                ],
            }
        ]
        lines = sp._format_speakers(speakers)
        assert lines[0] == "テスト: 30: C, 10: A, 20: B"

    def test_speaker_uuid_not_shown(self):
        lines = sp._format_speakers(SAMPLE_SPEAKERS)
        for line in lines:
            assert "388f246b" not in line
            assert "7ffcb7ce" not in line

    def test_non_dict_entry_skipped(self):
        speakers = ["not_a_dict", SAMPLE_SPEAKERS[0]]
        lines = sp._format_speakers(speakers)
        assert len(lines) == 1
        assert "ずんだもん" in lines[0]

    def test_missing_name_skipped(self):
        speakers = [{"styles": [{"name": "A", "id": 1}]}]
        lines = sp._format_speakers(speakers)
        assert lines == []

    def test_missing_styles_skipped(self):
        speakers = [{"name": "テスト"}]
        lines = sp._format_speakers(speakers)
        assert lines == []

    def test_empty_styles_skipped(self):
        speakers = [{"name": "テスト", "styles": []}]
        lines = sp._format_speakers(speakers)
        assert lines == []

    def test_malformed_style_entries_skipped(self):
        speakers = [
            {
                "name": "テスト",
                "styles": [
                    "not_dict",
                    {"name": "valid", "id": 5},
                    {"name": "no_id"},
                    {"id": 9},
                ],
            }
        ]
        lines = sp._format_speakers(speakers)
        assert lines == ["テスト: 5: valid"]

    def test_empty_list(self):
        assert sp._format_speakers([]) == []


# ---------------------------------------------------------------------------
# fetch_and_display
# ---------------------------------------------------------------------------


class TestFetchAndDisplay:
    def test_exit_0_on_success(self):
        url, shutdown = _make_mock_server(SAMPLE_SPEAKERS)
        try:
            out = io.StringIO()
            err = io.StringIO()
            rc = sp.fetch_and_display(url, out=out, err=err)
            assert rc == 0
            output = out.getvalue()
            assert "ずんだもん" in output
            assert "四国めたん" in output
            assert err.getvalue() == ""
        finally:
            shutdown()

    def test_exit_1_on_unreachable(self):
        out = io.StringIO()
        err = io.StringIO()
        rc = sp.fetch_and_display("http://127.0.0.1:1", out=out, err=err)
        assert rc == 1
        assert "VOICEVOX" in err.getvalue() or "Warning" in err.getvalue()
        assert out.getvalue() == ""

    def test_exit_1_on_empty_valid_entries(self):
        """レスポンスは list だが有効なエントリが 0 件"""
        url, shutdown = _make_mock_server([{"no_name": True}])
        try:
            out = io.StringIO()
            err = io.StringIO()
            rc = sp.fetch_and_display(url, out=out, err=err)
            assert rc == 1
            assert err.getvalue() != ""
        finally:
            shutdown()

    def test_warning_contains_doctor_hint(self):
        out = io.StringIO()
        err = io.StringIO()
        sp.fetch_and_display("http://127.0.0.1:1", out=out, err=err)
        assert "doctor" in err.getvalue()


# ---------------------------------------------------------------------------
# CLI 統合 (bin/vvread speakers)
# ---------------------------------------------------------------------------


VVREAD = REPO / "bin" / "vvread"


class TestVvreadSpeakersCli:
    def test_unreachable_engine_exits_1(self, tmp_path):
        import os
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("VOICEVOX_")}
        env["VOICEVOX_ENGINE_URL"] = "http://127.0.0.1:1"
        result = subprocess.run(
            [str(VVREAD), "speakers"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 1
        assert "VOICEVOX" in result.stderr or "Warning" in result.stderr

    def test_engine_url_option(self, tmp_path):
        result = subprocess.run(
            [str(VVREAD), "speakers", "--engine-url", "http://127.0.0.1:1"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1

    def test_success_with_mock_engine(self):
        url, shutdown = _make_mock_server(SAMPLE_SPEAKERS)
        try:
            result = subprocess.run(
                [str(VVREAD), "speakers", "--engine-url", url],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 0
            assert "ずんだもん" in result.stdout
        finally:
            shutdown()
