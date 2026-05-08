"""tests/conftest.py - 共通の path 調整 + VOICEVOX モックサーバー fixture

scripts/ を sys.path に追加して `import sanitize` 等を可能にする。
voicevox_mock fixture は Python http.server で /audio_query / /synthesis を
返す簡易モック。test_lib_voicevox.py / test_cmd_synth.py 等から共用される。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


# ---------------------------------------------------------------------------
# VOICEVOX Engine モックサーバー(test_lib_voicevox.py から移設、R-028)
# ---------------------------------------------------------------------------


class VoicevoxMockState:
    """サーバー側の挙動を制御する設定とリクエストログ"""

    def __init__(self):
        self.fail_audio_query = False
        self.fail_synthesis = False
        # 受信したリクエストの記録(検証用)
        self.requests = []

    def reset(self):
        self.fail_audio_query = False
        self.fail_synthesis = False
        self.requests.clear()


def _make_voicevox_handler(state: VoicevoxMockState):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # /version は cmd_on_stop の health check で叩かれる(GET、空 body)。
            # /speakers は doctor の speaker 存在確認(R-009)で叩かれる。
            state.requests.append({"path": self.path, "body": b""})
            if "/version" in self.path:
                payload = b'"0.14.0"'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if "/speakers" in self.path:
                # state.speakers_payload があればそれを返す(test 用 override)、
                # 無ければ default の最小 speakers リスト(id 0..3)。
                payload_obj = getattr(state, "speakers_payload", None)
                if payload_obj is None:
                    payload_obj = [
                        {"name": "default speaker",
                         "styles": [{"name": "ノーマル", "id": 0},
                                    {"name": "あまあま", "id": 1},
                                    {"name": "セクシー", "id": 2},
                                    {"name": "ツンツン", "id": 3}]},
                    ]
                data = json.dumps(payload_obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            state.requests.append({"path": self.path, "body": body})

            if "/audio_query" in self.path:
                if state.fail_audio_query:
                    self.send_response(500)
                    self.end_headers()
                    return
                # speak.sh / cmd_synth.sh が jq で上書きするデフォルト値
                payload = {
                    "speedScale": 1.0,
                    "prePhonemeLength": 0.0,
                    "postPhonemeLength": 0.0,
                    "pitchScale": 0.0,
                    "intonationScale": 1.0,
                    "volumeScale": 1.0,
                    "pauseLengthScale": 1.0,
                    "accent_phrases": [],
                    "outputSamplingRate": 24000,
                    "outputStereo": False,
                    "kana": "",
                }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if "/synthesis" in self.path:
                if state.fail_synthesis:
                    self.send_response(500)
                    self.end_headers()
                    return
                # 最小限の WAV 風バイト列(RIFF ヘッダだけあれば検証用には十分)
                wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav_bytes)))
                self.end_headers()
                self.wfile.write(wav_bytes)
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, *args, **kwargs):
            # テスト出力を汚さないため標準ログを抑制
            pass

    return Handler


@pytest.fixture
def voicevox_mock():
    """VOICEVOX Engine のモック HTTP server を localhost:任意ポートで起動する fixture。

    yield する dict:
      - port: 割り当てられたポート番号
      - url:  http://127.0.0.1:<port>
      - state: VoicevoxMockState インスタンス(検証用、fail フラグ操作可)
    """
    state = VoicevoxMockState()
    server = HTTPServer(("127.0.0.1", 0), _make_voicevox_handler(state))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "state": state,
    }

    server.shutdown()
    thread.join(timeout=2)
