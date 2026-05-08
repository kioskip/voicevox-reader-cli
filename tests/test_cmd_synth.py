"""scripts/cmd_synth.sh のテスト (R-028)

vvread synth subcommand。テキスト → wav 出力(再生しない)。

カバー範囲:
- 引数不足(text 無し / --output 無し / 両方無し)
- output 生成(VOICEVOX mock 経由で wav が書かれる)
- --output= 形式 / --output FILE 形式 両対応
- --speaker / VOICEVOX_SPEAKER 環境変数
- 出力先ディレクトリの自動作成(mkdir -p)
- 合成失敗時(VOICEVOX が 5xx)
- 日本語テキスト
- bin/vvread 経由(synth subcommand dispatch)
- conftest.py の voicevox_mock fixture を共用
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_SYNTH = REPO / "scripts" / "cmd" / "synth.sh"


def _path_env(tmp_path: Path) -> dict:
    """state/log/cache を tmp_path に逃がす"""
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
    }


def _clean_env(env_extra=None):
    """親プロセスの VOICEVOX_* / VVREAD_* を継承させない(テスト間汚染回避)"""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def run_synth(*args, env_extra=None, cwd=None) -> subprocess.CompletedProcess:
    """cmd_synth.sh を直接実行(vvread 経由ではない)"""
    return subprocess.run(
        [str(CMD_SYNTH), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def run_vvread_synth(*args, env_extra=None, cwd=None) -> subprocess.CompletedProcess:
    """bin/vvread synth ... 経由で実行"""
    return subprocess.run(
        [str(VVREAD), "synth", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# 引数不足 / usage
# ---------------------------------------------------------------------------


class TestArgsValidation:
    def test_no_args_shows_usage_and_exits_1(self, tmp_path):
        r = run_synth(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread synth" in r.stderr
        assert "<text> is required" in r.stderr

    def test_text_only_missing_output_exits_1(self, tmp_path):
        r = run_synth("hello", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "--output FILE is required" in r.stderr

    def test_output_only_missing_text_exits_1(self, tmp_path):
        r = run_synth("--output", str(tmp_path / "out.wav"),
                      env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "<text> is required" in r.stderr

    def test_too_many_positional_exits_1(self, tmp_path):
        r = run_synth("text1", "text2", "--output", str(tmp_path / "out.wav"),
                      env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "too many positional" in r.stderr

    def test_unknown_option_exits_1(self, tmp_path):
        r = run_synth("hello", "--bogus", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "unknown option" in r.stderr

    def test_output_without_arg_exits_1(self, tmp_path):
        r = run_synth("hello", "--output", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "--output requires an argument" in r.stderr

    def test_help_flag_shows_usage(self, tmp_path):
        r = run_synth("-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1   # usage は exit 1(自前 exit 0 にすべきか議論あり、現状 1)
        assert "Usage: vvread synth" in r.stderr


# ---------------------------------------------------------------------------
# output 生成(VOICEVOX mock)
# ---------------------------------------------------------------------------


class TestOutputGeneration:
    def test_writes_wav_to_output_path(self, voicevox_mock, tmp_path):
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()
        assert out.stat().st_size > 0
        # mock が返す RIFF ヘッダが含まれていること
        assert out.read_bytes().startswith(b"RIFF")

    def test_output_equals_form(self, voicevox_mock, tmp_path):
        """--output=FILE 形式も同じく動作"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", f"--output={out}", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()

    def test_creates_output_directory(self, voicevox_mock, tmp_path):
        """出力先のディレクトリが無くても自動作成する(mkdir -p)"""
        out = tmp_path / "nested" / "deeper" / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()

    def test_output_path_with_spaces(self, voicevox_mock, tmp_path):
        """空白を含むパスもクォート保持されて動く"""
        out = tmp_path / "with space" / "my output.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()

    def test_japanese_text(self, voicevox_mock, tmp_path):
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("こんにちは", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()


# ---------------------------------------------------------------------------
# speaker 指定
# ---------------------------------------------------------------------------


class TestSpeaker:
    def test_speaker_via_flag(self, voicevox_mock, tmp_path):
        """--speaker N を mock の audio_query path で確認"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out),
                      "--speaker", "7", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        # audio_query / synthesis 両方の URL に speaker=7 が乗ること
        for req in voicevox_mock["state"].requests:
            assert "speaker=7" in req["path"]

    def test_speaker_via_env(self, voicevox_mock, tmp_path):
        """VOICEVOX_SPEAKER 環境変数で speaker を上書き"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]
        env["VOICEVOX_SPEAKER"] = "11"

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=11" in req["path"]

    def test_flag_overrides_env(self, voicevox_mock, tmp_path):
        """--speaker は VOICEVOX_SPEAKER より優先(R-025 優先順位の縮小版)"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]
        env["VOICEVOX_SPEAKER"] = "11"

        r = run_synth("hello", "--output", str(out),
                      "--speaker", "22", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=22" in req["path"]

    def test_default_speaker_is_3(self, voicevox_mock, tmp_path):
        """speaker 未指定時の既定値は 3(VOICEVOX_SPEAKER 既定)"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]
        # VOICEVOX_SPEAKER は env から削除
        env.pop("VOICEVOX_SPEAKER", None)

        # cwd=tmp_path で実行することで repo root の vvread.settings.json を読まない
        r = run_synth("hello", "--output", str(out), env_extra=env, cwd=str(tmp_path))
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=3" in req["path"]


# ---------------------------------------------------------------------------
# 合成失敗
# ---------------------------------------------------------------------------


class TestSynthesisFailure:
    def test_audio_query_fails_returns_1(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_audio_query = True
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 1
        assert "synthesis failed" in r.stderr
        # 失敗時は output ファイルが作られない or 空
        assert not out.exists() or out.stat().st_size == 0

    def test_synthesis_fails_returns_1(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_synthesis = True
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 1
        assert "synthesis failed" in r.stderr

    def test_unreachable_engine_returns_1(self, tmp_path):
        """到達不能な ENGINE URL を渡すと curl が失敗 → exit 1"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = "http://127.0.0.1:1"  # 確実に reachable でないポート

        r = run_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 1
        assert "synthesis failed" in r.stderr


# ---------------------------------------------------------------------------
# bin/vvread 経由(dispatch 動作確認)
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_synth_dispatches_to_cmd_synth(self, voicevox_mock, tmp_path):
        """vvread synth 経由で起動しても cmd_synth.sh と同じ挙動"""
        out = tmp_path / "out.wav"
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]

        r = run_vvread_synth("hello", "--output", str(out), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert out.exists()

    def test_vvread_synth_no_args_shows_usage(self, tmp_path):
        r = run_vvread_synth(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread synth" in r.stderr
