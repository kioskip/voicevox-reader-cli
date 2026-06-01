"""tests/conftest.py - 共通の path 調整 + VOICEVOX モックサーバー fixture

scripts/ を sys.path に追加して `import sanitize` 等を可能にする。
voicevox_mock fixture は Python http.server で /audio_query / /synthesis を
返す簡易モック。test_lib_voicevox.py / test_cmd_synth.py 等から共用される。
"""
import json
import sys
import threading
import time
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
        self.fail_synthesis_count: int = 0  # N 回だけ synthesis を失敗させる(B-124 fallback テスト用)
        # 受信したリクエストの記録(検証用)
        self.requests = []

    def reset(self):
        self.fail_audio_query = False
        self.fail_synthesis = False
        self.fail_synthesis_count = 0
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
                if state.fail_synthesis or state.fail_synthesis_count > 0:
                    if state.fail_synthesis_count > 0:
                        state.fail_synthesis_count -= 1
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


def wait_for_file(path, timeout_sec=10, check_interval=0.05):
    """ファイルの存在または条件を待機（動的タイムアウト付き）。

    Args:
        path: チェック対象のファイルパス (Path または str)
        timeout_sec: 最大待機秒数（デフォルト 10 秒）
        check_interval: チェック間隔（デフォルト 0.05 秒）

    Raises:
        TimeoutError: timeout_sec 秒を超えても条件が満たされない場合
    """
    path = Path(path) if isinstance(path, str) else path
    start_time = time.time()

    while True:
        if path.exists():
            return

        elapsed = time.time() - start_time
        if elapsed >= timeout_sec:
            raise TimeoutError(
                f"File not created within {timeout_sec} seconds: {path}"
            )

        time.sleep(check_interval)


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

    state.reset()  # function スコープのため実質 no-op だが、将来の scope 変更への備え
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture
def voicevox_mock2():
    """B-124 マルチエンジンテスト用の2台目 VOICEVOX Engine モック。"""
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

    state.reset()
    server.shutdown()
    thread.join(timeout=2)
