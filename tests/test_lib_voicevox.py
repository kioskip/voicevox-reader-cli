"""lib_voicevox.sh::voicevox_synthesize のテスト

実 VOICEVOX Engine を立てるのではなく、conftest.py の voicevox_mock
fixture を使って Python http.server で `/audio_query` / `/synthesis` を
返す簡易モックサーバーを起動し、そこに lib_voicevox.sh の関数が curl で
叩きにいく構成。

ENGINE / SPEED_SCALE 等のチューニング値は呼び出し元のローカル変数として
bash の dynamic scoping で参照される設計なので、テストの bash スクリプト側で
明示的に定義する。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_LOG = REPO / "scripts" / "lib" / "log.sh"
LIB_VOICEVOX = REPO / "scripts" / "lib" / "voicevox.sh"

# voicevox_mock fixture は tests/conftest.py で定義(R-028 で移設)


# ---------------------------------------------------------------------------
# ヘルパー: bash スクリプトの組み立てと実行
# ---------------------------------------------------------------------------


def _run_synthesize(
    *,
    wav_path: Path,
    text: str,
    speaker: str,
    chunk_label: str,
    engine_url: str,
    tmp_dir: Path,
    extra_env: dict | None = None,
    speed: str = "1.5",
    pitch: str = "0",
    intonation: str = "1.0",
    volume: str = "1.0",
    pause: str = "1.0",
    pre_phoneme: str = "0",
    post_phoneme: str = "0",
):
    """lib_voicevox.sh::voicevox_synthesize を実行する shell スクリプトを構築・実行"""
    script = f"""
set -e
LOG_NAME=test
LOG_DIR='{tmp_dir}/logs'
mkdir -p "$LOG_DIR"
source '{LIB_LOG}'
source '{LIB_VOICEVOX}'

ENGINE='{engine_url}'
SPEED_SCALE={speed}
PRE_PHONEME={pre_phoneme}
POST_PHONEME={post_phoneme}
PITCH_SCALE={pitch}
INTONATION_SCALE={intonation}
VOLUME_SCALE={volume}
PAUSE_LENGTH_SCALE={pause}

voicevox_synthesize '{wav_path}' '{text}' '{speaker}' '{chunk_label}'
"""
    env = os.environ.copy()
    env["VOICEVOX_LOG_LEVEL"] = "DEBUG"  # audio_query / synthesis ログを出させる
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


class TestVoicevoxSynthesizeSuccess:
    def test_writes_wav_file(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        r = _run_synthesize(
            wav_path=wav,
            text="こんにちは",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert wav.exists()
        assert wav.read_bytes().startswith(b"RIFF"), "WAV ヘッダになっていない"

    def test_calls_audio_query_then_synthesis(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        paths = [r["path"].split("?")[0] for r in voicevox_mock["state"].requests]
        assert paths == ["/audio_query", "/synthesis"], f"paths={paths}"

    def test_passes_speaker_query_param(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="74",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        for r in voicevox_mock["state"].requests:
            assert "speaker=74" in r["path"], f"speaker クエリが渡っていない: {r['path']}"

    def test_jq_injects_speed_into_synthesis_body(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
            speed="1.7",
            pitch="0.05",
            volume="1.2",
            pause="0.8",
        )
        # 2 番目のリクエスト = synthesis、その body は jq tune 済みの JSON
        synth = voicevox_mock["state"].requests[1]
        body = json.loads(synth["body"])
        assert body["speedScale"] == 1.7
        assert body["pitchScale"] == 0.05
        assert body["volumeScale"] == 1.2
        assert body["pauseLengthScale"] == 0.8

    def test_japanese_text_url_encoded(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="日本語テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        # audio_query の path に %E... 形式でエンコードされている
        audio_query = voicevox_mock["state"].requests[0]
        assert "%E6" in audio_query["path"] or "%e6" in audio_query["path"], (
            f"日本語が URL エンコードされていない: {audio_query['path']}"
        )

    def test_query_files_are_cleaned_up(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        r = _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        assert r.returncode == 0
        # query.json と .tuned は関数内で必ず rm -f される
        assert not (tmp_path / "out.wav.query.json").exists()
        assert not (tmp_path / "out.wav.query.json.tuned").exists()


# ---------------------------------------------------------------------------
# ロギング
# ---------------------------------------------------------------------------


class TestVoicevoxSynthesizeLogging:
    def test_audio_query_log_includes_chunk_label(self, voicevox_mock, tmp_path):
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="2/5",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        log_file = tmp_path / "logs" / "speak.log"
        assert log_file.exists(), "ログファイルが作られていない"
        content = log_file.read_text()
        assert "audio_query chunk=2/5" in content
        assert "synthesis chunk=2/5" in content


# ---------------------------------------------------------------------------
# 失敗系
# ---------------------------------------------------------------------------


class TestVoicevoxSynthesizeFailure:
    def test_fails_when_audio_query_returns_5xx(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_audio_query = True
        wav = tmp_path / "out.wav"
        r = _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        assert r.returncode == 1, f"stderr={r.stderr}"
        # 残骸が残らない
        assert not (tmp_path / "out.wav.query.json").exists()
        assert not (tmp_path / "out.wav.query.json.tuned").exists()

    def test_fails_when_synthesis_returns_5xx(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_synthesis = True
        wav = tmp_path / "out.wav"
        r = _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
        )
        assert r.returncode == 1, f"stderr={r.stderr}"
        # audio_query は呼ばれている、synthesis は失敗
        paths = [r["path"].split("?")[0] for r in voicevox_mock["state"].requests]
        assert "/audio_query" in paths
        assert "/synthesis" in paths
        # 残骸が残らない
        assert not (tmp_path / "out.wav.query.json").exists()

    def test_fails_when_engine_unreachable(self, tmp_path):
        # 起動していないポートを叩く
        wav = tmp_path / "out.wav"
        r = _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url="http://127.0.0.1:1",  # ポート 1 はほぼ確実に閉じている
            tmp_dir=tmp_path,
        )
        assert r.returncode == 1
        assert not wav.exists() or wav.stat().st_size == 0
