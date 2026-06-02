"""scripts/cmd_play.sh のテスト (R-028)

vvread play subcommand。既存 wav の再生のみ(合成しない)。
fake player を bin_dir に置いて lib_playback の検出/起動を擬似する。

カバー範囲:
- 引数不足(wav 無し / 多すぎ)
- wav 不在 / 空 wav
- fake player 経由の再生確認(wav 引数が渡る、player exit 0 で OK)
- player 不在 → exit 1 + Hint stderr
- VVREAD_PLAYER override で player 強制
- player exit code が wait 経由で伝播
- bin/vvread play 経由 dispatch
"""
import os
import subprocess
import time
from pathlib import Path

from conftest import wait_for_file

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_PLAY = REPO / "scripts" / "cmd" / "play.sh"


def _path_env(tmp_path: Path) -> dict:
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        "VVREAD_PROJECT_SETTINGS": str(tmp_path / "no-project-settings.json"),
    }


def make_fake_player(
    bin_dir: Path,
    name: str,
    *,
    args_log: Path | None = None,
    touch_on_run: Path | None = None,
    exit_code: int = 0,
    sleep_seconds: float = 0,
):
    """偽 player バイナリ。test_lib_playback と同等だが exit_code を任意に設定可能。

    args_log: 引数を 1 つずつ append
    touch_on_run: 実行時に touch
    exit_code: 終了コード
    sleep_seconds: 再生時間擬似(0 なら即終了)
    """
    path = bin_dir / name
    lines = ["#!/bin/bash"]
    if args_log:
        lines.append(f'printf "%s\\n" "$@" >> "{args_log}"')
    if touch_on_run:
        lines.append(f'touch "{touch_on_run}"')
    if sleep_seconds > 0:
        lines.append(f"sleep {sleep_seconds}")
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def make_fake_wav(path: Path, content: bytes = b"RIFF\x00\x00\x00\x00WAVE"):
    """テスト用の最小 wav-like ファイル(再生はしない、存在 + 非空のみ満たせばよい)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _clean_env(env_extra=None):
    """親プロセスの VOICEVOX_* / VVREAD_* を継承させない(テスト間汚染回避)"""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def run_play(*args, env_extra=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CMD_PLAY), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
    )


def run_vvread_play(*args, env_extra=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VVREAD), "play", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# 引数 validation
# ---------------------------------------------------------------------------


class TestArgsValidation:
    def test_no_args_shows_usage_and_exits_1(self, tmp_path):
        r = run_play(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "<wav> is required" in r.stderr
        assert "Usage: vvread play" in r.stderr

    def test_too_many_args_exits_1(self, tmp_path):
        wav = tmp_path / "a.wav"
        make_fake_wav(wav)
        r = run_play(str(wav), "extra", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "too many positional" in r.stderr

    def test_help_flag_shows_usage(self, tmp_path):
        r = run_play("-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread play" in r.stderr


# ---------------------------------------------------------------------------
# wav 検証
# ---------------------------------------------------------------------------


class TestWavValidation:
    def test_missing_wav_exits_1(self, tmp_path):
        r = run_play(str(tmp_path / "no_such.wav"),
                     env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "wav file not found" in r.stderr

    def test_empty_wav_exits_1(self, tmp_path):
        wav = tmp_path / "empty.wav"
        wav.write_bytes(b"")
        r = run_play(str(wav), env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "empty" in r.stderr


# ---------------------------------------------------------------------------
# fake player 経由再生
# ---------------------------------------------------------------------------


class TestPlaybackWithFakePlayer:
    def test_invokes_player_with_wav_arg(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        args_log = tmp_path / "args.log"
        # afplay はオプション無しで wav パスのみ受ける(lib_playback の構築通り)
        make_fake_player(bin_dir, "afplay", args_log=args_log, exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        # args.log を読んで wav パスが渡ったことを確認
        wait_for_file(args_log)
        recorded = args_log.read_text().splitlines()
        assert recorded == [str(wav)]

    def test_aplay_receives_q_flag(self, tmp_path):
        """lib_playback の player 別引数組み立てが play の経路でも効く"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        args_log = tmp_path / "args.log"
        make_fake_player(bin_dir, "aplay", args_log=args_log, exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "aplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        wait_for_file(args_log)
        recorded = args_log.read_text().splitlines()
        assert recorded == ["-q", str(wav)]

    def test_marker_file_confirms_player_ran(self, tmp_path):
        """fake が touch した marker から再生が走ったことを確認"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_pid_file_cleaned_up_after_completion(self, tmp_path):
        """再生完了後 STATE_DIR/playing.pid は消える"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        pid_file = tmp_path / "state" / "playing.pid"
        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# player 不在
# ---------------------------------------------------------------------------


class TestNoPlayer:
    def test_no_player_returns_1_with_hint(self, tmp_path):
        bin_dir = tmp_path / "empty_bin"
        bin_dir.mkdir()  # player 無し

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        # /usr/bin は dirname / cat 等の必須コマンドのため残すが、
        # VVREAD_PLAYER で bogus 名を指定して macOS の /usr/bin/afplay を
        # 拾わせない(detect_player は override 不在時に exit 1 で fallback 拒否)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "definitely_not_a_player_xyz"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 1, f"stderr={r.stderr}"
        assert "no audio player available" in r.stderr


# ---------------------------------------------------------------------------
# VVREAD_PLAYER override
# ---------------------------------------------------------------------------


class TestVvreadPlayerOverride:
    def test_override_used_for_play(self, tmp_path):
        """VVREAD_PLAYER で指定した player が呼ばれる"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        args_log = tmp_path / "args.log"
        # 通常は afplay が macOS で自動検出されるが、ffplay を明示指定して上書き
        make_fake_player(bin_dir, "ffplay", args_log=args_log, exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "ffplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        wait_for_file(args_log)
        recorded = args_log.read_text().splitlines()
        # ffplay は -nodisp -autoexit -loglevel quiet <wav> の順
        assert recorded == ["-nodisp", "-autoexit", "-loglevel", "quiet", str(wav)]


# ---------------------------------------------------------------------------
# player exit code 伝播
# ---------------------------------------------------------------------------


class TestPlayerExitCodePropagation:
    def test_player_exit_5_propagates(self, tmp_path):
        """player が非ゼロで終了したら vvread play も同じ exit code を返す"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # 即時に exit 5 で終わる player
        make_fake_player(bin_dir, "afplay", exit_code=5, sleep_seconds=0.1)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 5, f"stderr={r.stderr}"
        assert "player exited with code 5" in r.stderr


# ---------------------------------------------------------------------------
# T-013: PID_FILE 削除レース
# ---------------------------------------------------------------------------


class TestPidFileDeletionRace:
    """T-013: 「player wait → PID_FILE rm」が race を生む問題の修正検証。

    cmd_play は wait 後の cleanup で PID_FILE 内容が自分の PID と一致した時のみ
    rm する。fake player が exit 直前に foreign PID で PID_FILE を上書きする
    シナリオで、cmd_play 終了後に PID_FILE が消されないことを確認する。

    実 race(別 vvread say が間に割り込む)を直接再現するのは非決定的なため、
    上書きを fake player 内で行う単体検証で代用。
    """

    def test_pid_file_with_foreign_pid_is_not_removed(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # fake player: 起動直後に PID_FILE を foreign PID で上書きしてから exit。
        # 上書きの順序を保証するため bash で直接 PID_FILE を書く。
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        pid_file = state_dir / "playing.pid"
        foreign_pid = "987654"

        afplay = bin_dir / "afplay"
        afplay.write_text(
            "#!/bin/bash\n"
            f'echo "{foreign_pid}" > "{pid_file}"\n'
            "exit 0\n"
        )
        afplay.chmod(0o755)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        # PID_FILE は残存し、foreign_pid のままである
        assert pid_file.exists(), \
            "PID_FILE が消されている(T-013 修正未適用)"
        assert pid_file.read_text().strip() == foreign_pid, \
            f"PID_FILE 内容が foreign_pid から変わっている: {pid_file.read_text()!r}"


# ---------------------------------------------------------------------------
# bin/vvread 経由
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_play_dispatches(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        wav = tmp_path / "in.wav"
        make_fake_wav(wav)

        env = _path_env(tmp_path)
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_vvread_play(str(wav), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_vvread_play_no_args_shows_usage(self, tmp_path):
        r = run_vvread_play(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread play" in r.stderr
