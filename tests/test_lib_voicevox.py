"""lib_voicevox.sh::voicevox_synthesize のテスト

実 VOICEVOX Engine を立てるのではなく、conftest.py の voicevox_mock
fixture を使って Python http.server で `/audio_query` / `/synthesis` を
返す簡易モックサーバーを起動し、そこに lib_voicevox.sh の関数が curl で
叩きにいく構成。

VOICEVOX_ENGINE_URL / VOICEVOX_SPEED 等のチューニング値は VOICEVOX_* 環境変数
として渡す。voicevox_synthesize() が内部で直解決するため、呼び出し側の
shell 変数(ENGINE / SPEED_SCALE 等)は不要(S-006/S-007)。
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
# voicevox.sh は encoded=$("${{PYTHON}}" -c ...) を使う(Info: python3 直呼び解消、
# uv 管理下の `${{PYTHON}}` 慣行に統一)。caller(cmd/say.sh, cmd/synth.sh)と
# 同じ解決ロジックをここでも再現する。
PYTHON="{REPO}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
source '{LIB_VOICEVOX}'

voicevox_synthesize '{wav_path}' '{text}' '{speaker}' '{chunk_label}'
"""
    env = os.environ.copy()
    env["VOICEVOX_LOG_LEVEL"] = "DEBUG"  # audio_query / synthesis ログを出させる
    env["VOICEVOX_ENGINE_URL"] = engine_url
    env["VOICEVOX_SPEED"] = speed
    env["VOICEVOX_PRE_PHONEME"] = pre_phoneme
    env["VOICEVOX_POST_PHONEME"] = post_phoneme
    env["VOICEVOX_PITCH"] = pitch
    env["VOICEVOX_INTONATION"] = intonation
    env["VOICEVOX_VOLUME"] = volume
    env["VOICEVOX_PAUSE_SCALE"] = pause
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

    def test_speed_via_env_var(self, voicevox_mock, tmp_path):
        """VOICEVOX_SPEED 環境変数で speedScale が反映される(S-006/S-007)"""
        wav = tmp_path / "out.wav"
        _run_synthesize(
            wav_path=wav,
            text="テスト",
            speaker="3",
            chunk_label="1/1",
            engine_url=voicevox_mock["url"],
            tmp_dir=tmp_path,
            speed="2.0",
        )
        synth = voicevox_mock["state"].requests[1]
        body = json.loads(synth["body"])
        assert body["speedScale"] == 2.0

    def test_does_not_read_caller_speed_scale(self, voicevox_mock, tmp_path):
        """呼び出し側の SPEED_SCALE shell 変数に依存しないことの回帰防止テスト(S-006/S-007)。
        bash script 内で SPEED_SCALE=9.9 を設定しても VOICEVOX_SPEED=1.5 が使われる。
        voicevox_synthesize は SPEED_SCALE を参照しないので 9.9 は出てこない。
        """
        wav = tmp_path / "out.wav"
        script = f"""
set -e
LOG_NAME=test
LOG_DIR='{tmp_path}/logs'
mkdir -p "$LOG_DIR"
source '{LIB_LOG}'
PYTHON="{REPO}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
source '{LIB_VOICEVOX}'
SPEED_SCALE=9.9
voicevox_synthesize '{wav}' 'テスト' '3' '1/1'
"""
        env = os.environ.copy()
        env["VOICEVOX_ENGINE_URL"] = voicevox_mock["url"]
        env["VOICEVOX_SPEED"] = "1.5"
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        assert r.returncode == 0, f"stderr={r.stderr}"
        synth = voicevox_mock["state"].requests[1]
        body = json.loads(synth["body"])
        assert body["speedScale"] == 1.5, f"SPEED_SCALE=9.9 が漏れている: {body['speedScale']}"

    def test_engine_url_takes_priority_over_engine(self, voicevox_mock, tmp_path):
        """VOICEVOX_ENGINE_URL が VOICEVOX_ENGINE より優先される(S-008 legacy fallback)"""
        wav = tmp_path / "out.wav"
        # VOICEVOX_ENGINE に到達不能 URL を設定し、VOICEVOX_ENGINE_URL が勝つことを確認
        script = f"""
set -e
LOG_NAME=test
LOG_DIR='{tmp_path}/logs'
mkdir -p "$LOG_DIR"
source '{LIB_LOG}'
PYTHON="{REPO}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
source '{LIB_VOICEVOX}'
voicevox_synthesize '{wav}' 'テスト' '3' '1/1'
"""
        env = os.environ.copy()
        env["VOICEVOX_ENGINE_URL"] = voicevox_mock["url"]
        env["VOICEVOX_ENGINE"] = "http://127.0.0.1:1"  # 到達不能
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        assert r.returncode == 0, f"VOICEVOX_ENGINE_URL が優先されなかった: stderr={r.stderr}"
        assert wav.exists()

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
