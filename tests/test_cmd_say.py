"""scripts/cmd_say.sh のテスト (R-005)

vvread say は synth + play の薄い orchestrator。
- 単一/複数 chunk の合成 → 再生
- 古い playback の停止
- session token による preemption(stale 判定で silent 中断)
- player 不在 / synth 失敗 のエラーパス
- bin/vvread say 経由の dispatch

prefetch / 並列合成は R-005 のスコープ外。本テストでも逐次 (chunk N
完了後に N+1 開始)を前提に書く。
"""
import os
import subprocess
import time

import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_SAY = REPO / "scripts" / "cmd" / "say.sh"
SANITIZE_PY = REPO / "scripts" / "sanitize.py"
CHUNK_SPLIT_PY = REPO / "scripts" / "chunk_split.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _path_env(tmp_path: Path) -> dict:
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
    }


def _clean_env(env_extra=None) -> dict:
    """親プロセスの VOICEVOX_* / VVREAD_* を継承させない"""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def make_fake_player(
    bin_dir: Path,
    name: str,
    *,
    args_log: Path | None = None,
    touch_on_run: Path | None = None,
    exit_code: int = 0,
    sleep_seconds: float = 0,
):
    """偽 player バイナリ。test_cmd_play.py と同じ流儀。"""
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


def count_expected_chunks(text: str, speaker: str = "3") -> int:
    """sanitize → chunk_split で何 chunk になるかを実測する(テスト用)。

    VOICEVOX_MAX_CHARS 等のユーザ env で truncation 挙動が変わるため、
    cmd_say.sh と同じ _clean_env(VOICEVOX_* を継承させない)で実行する。
    """
    env = _clean_env()
    san = subprocess.run(
        ["python3", str(SANITIZE_PY)],
        input=text, capture_output=True, text=True, check=True, env=env,
    )
    chunk = subprocess.run(
        ["python3", str(CHUNK_SPLIT_PY), "--speaker", speaker],
        input=san.stdout, capture_output=True, text=True, check=True, env=env,
    )
    return sum(1 for line in chunk.stdout.splitlines() if line.strip())


def run_say(*args, env_extra=None, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CMD_SAY), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_vvread_say(*args, env_extra=None, timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VVREAD), "say", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _say_env(tmp_path: Path, voicevox_url: str, bin_dir: Path,
             player: str = "afplay") -> dict:
    """say 実行に必要な共通 env(state/log/cache + VOICEVOX_ENGINE +
    PATH + VVREAD_PLAYER)"""
    env = _path_env(tmp_path)
    env["VOICEVOX_ENGINE"] = voicevox_url
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["VVREAD_PLAYER"] = player
    return env


# ---------------------------------------------------------------------------
# 引数 validation
# ---------------------------------------------------------------------------


class TestArgsValidation:
    def test_no_args_shows_usage(self, tmp_path):
        r = run_say(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread say" in r.stderr

    def test_too_many_positional(self, tmp_path):
        r = run_say("hello", "world", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "too many positional" in r.stderr

    def test_unknown_option(self, tmp_path):
        r = run_say("hello", "--bogus", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "unknown option" in r.stderr

    def test_speaker_without_arg(self, tmp_path):
        r = run_say("hello", "--speaker", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "--speaker requires" in r.stderr

    def test_help_flag(self, tmp_path):
        r = run_say("-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread say" in r.stderr


# ---------------------------------------------------------------------------
# 単一 chunk の正常経路
# ---------------------------------------------------------------------------


class TestSingleChunk:
    def test_short_text_synth_then_play_once(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)

        r = run_say("hello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

        # 1 audio_query + 1 synthesis(chunk 1 つ)
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 1
        n_query = sum(1 for req in voicevox_mock["state"].requests
                      if "/audio_query" in req["path"])
        assert n_query == 1

    def test_speaker_via_flag(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)

        r = run_say("hello", "--speaker", "11", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        # 全 request の URL に speaker=11
        for req in voicevox_mock["state"].requests:
            assert "speaker=11" in req["path"]

    def test_session_id_written_to_state_dir(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say("hello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        # session.id が書かれている(say 中に作成、終了後も残る)
        session_file = Path(env["VVREAD_STATE_DIR"]) / "session.id"
        assert session_file.exists()
        # 形式: <ms>_<pid>
        content = session_file.read_text().strip()
        assert "_" in content


# ---------------------------------------------------------------------------
# 複数 chunk(逐次合成 + 再生)
# ---------------------------------------------------------------------------


class TestMultipleChunks:
    def test_long_text_splits_and_plays_each_chunk(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        args_log = tmp_path / "args.log"
        make_fake_player(bin_dir, "afplay", args_log=args_log, exit_code=0)

        # 200 char default を超え、複数 chunk になる句点入り text
        text = "テストです。" * 100
        expected = count_expected_chunks(text, speaker="3")
        assert expected >= 2, f"テスト前提: 2 chunk 以上(実際 {expected})"

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say(text, env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        # 各 chunk が synth + play されたことを request 数で確認
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == expected

        # player は chunk ごとに 1 回呼ばれる(args.log 1 行 = 1 invocation で
        # afplay <wav> なので行数 = chunk 数)
        for _ in range(100):
            if args_log.exists():
                break
            time.sleep(0.05)
        assert args_log.exists()
        invocations = args_log.read_text().splitlines()
        assert len(invocations) == expected


# ---------------------------------------------------------------------------
# 旧 playback の停止
# ---------------------------------------------------------------------------


class TestStopOldPlayback:
    def test_say_kills_existing_playing_pid(self, voicevox_mock, tmp_path):
        """STATE_DIR/playing.pid に PID があれば say 起動時に kill する"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # 旧 playback を simulate(長期 sleep プロセス)
        old_proc = subprocess.Popen(["sleep", "60"])
        try:
            (state_dir / "playing.pid").write_text(str(old_proc.pid))
            (state_dir / "session.id").write_text("OLD_SESSION_xyz")

            env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
            r = run_say("hello", env_extra=env)
            assert r.returncode == 0, f"stderr={r.stderr}"

            # 旧 process が kill されていること
            for _ in range(20):
                if old_proc.poll() is not None:
                    break
                time.sleep(0.05)
            assert old_proc.poll() is not None, "旧 playback が kill されなかった"

            # session.id が新しくなっていること
            new_session = (state_dir / "session.id").read_text().strip()
            assert new_session != "OLD_SESSION_xyz"
        finally:
            if old_proc.poll() is None:
                old_proc.kill()
                old_proc.wait()


# ---------------------------------------------------------------------------
# session token preemption(mid-flight 中断)
# ---------------------------------------------------------------------------


class TestSessionTokenPreemption:
    def test_say_aborts_when_session_id_changed_mid_flight(
        self, voicevox_mock, tmp_path,
    ):
        """re-entrant な say: 実行中に session.id を上書きすると残り chunk を
        スキップして exit 0 で抜ける"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # 1 chunk あたり 0.5 秒の擬似再生で観測ウィンドウを確保
        make_fake_player(bin_dir, "afplay", exit_code=0, sleep_seconds=0.5)

        # 多 chunk テキスト(>= 3 chunk あれば preempt 観測可能)
        # default VOICEVOX_MAX_CHARS=500 で truncation がかかるため 4 chunk 程度
        text = "テストです。" * 100
        expected = count_expected_chunks(text, speaker="3")
        assert expected >= 3, f"テスト前提: 3 chunk 以上(実際 {expected})"

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)

        # background 起動
        proc = subprocess.Popen(
            [str(CMD_SAY), text],
            env=_clean_env(env),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # chunk 1 が synth/play 中になるよう少し待つ
            time.sleep(0.6)

            # session.id を上書きして preempt
            (state_dir / "session.id").write_text("STALE_TOKEN_OVERRIDE")

            # 最大 10 秒で exit 0 復帰するはず
            proc.wait(timeout=10)
            assert proc.returncode == 0, "preempt 時は graceful 0 exit"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # 全 chunk より少ない数しか synth されていない
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth < expected, (
            f"preempt されたのに全 chunk({expected}) 処理された (n_synth={n_synth})"
        )


# ---------------------------------------------------------------------------
# player 不在
# ---------------------------------------------------------------------------


class TestNoPlayer:
    def test_no_player_returns_1_after_synth(self, voicevox_mock, tmp_path):
        """synth は通るが play で player が見つからず exit 1"""
        bin_dir = tmp_path / "empty_bin"
        bin_dir.mkdir()  # player 無し

        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE"] = voicevox_mock["url"]
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
        env["VVREAD_PLAYER"] = "definitely_not_a_player_xyz"

        r = run_say("hello", env_extra=env)
        assert r.returncode == 1, f"stderr={r.stderr}"
        assert "no audio player available" in r.stderr


# ---------------------------------------------------------------------------
# synth 失敗
# ---------------------------------------------------------------------------


class TestSynthFailure:
    def test_synth_failure_exits_1(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_synthesis = True
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say("hello", env_extra=env)
        assert r.returncode == 1, f"stderr={r.stderr}"
        assert "synthesis failed" in r.stderr

    def test_audio_query_failure_exits_1(self, voicevox_mock, tmp_path):
        voicevox_mock["state"].fail_audio_query = True
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say("hello", env_extra=env)
        assert r.returncode == 1, f"stderr={r.stderr}"
        assert "synthesis failed" in r.stderr


# ---------------------------------------------------------------------------
# 空テキスト
# ---------------------------------------------------------------------------


class TestEmptyAfterSanitize:
    def test_text_that_sanitizes_to_empty_exits_0(self, voicevox_mock, tmp_path):
        """sanitize で空になるような text(空白のみ)は silent に exit 0"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say("   ", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        # synth は呼ばれない
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 0


# ---------------------------------------------------------------------------
# T-013: PID_FILE 削除レース
# ---------------------------------------------------------------------------


class TestPidFileDeletionRace:
    """T-013: _say_play_chunk 末尾で PID_FILE を盲目的に rm する race を修正。

    実 race(別 vvread say が割り込む)は非決定的なため、fake player が exit
    直前に PID_FILE を foreign PID で上書きするシナリオで単体検証する。
    cmd_say 完了後に PID_FILE が消されず、foreign PID が保持されればよい。
    """

    def test_pid_file_with_foreign_pid_survives_say(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

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

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_say("hello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        assert pid_file.exists(), \
            "PID_FILE が消されている(T-013 修正未適用)"
        assert pid_file.read_text().strip() == foreign_pid, \
            f"PID_FILE 内容が foreign_pid から変わっている: {pid_file.read_text()!r}"


# ---------------------------------------------------------------------------
# bin/vvread 経由 dispatch
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_say_dispatches_to_cmd_say(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread_say("hello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_vvread_say_no_args_shows_usage(self, tmp_path):
        r = run_vvread_say(env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread say" in r.stderr


# ---------------------------------------------------------------------------
# root コマンド入力モード (B-102 / B-002)
# ---------------------------------------------------------------------------


def run_vvread(*args, env_extra=None, stdin_text=None, timeout=30) -> subprocess.CompletedProcess:
    """bin/vvread を直接呼ぶ。
    stdin_text を渡すと subprocess が stdin を pipe として接続する。
    None の場合は subprocess.DEVNULL を渡し、pipe 判定 ([ -p /dev/stdin ]) を避ける。
    """
    kwargs: dict = dict(
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if stdin_text is not None:
        kwargs["input"] = stdin_text
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run([str(VVREAD), *args], **kwargs)


class TestRootInputModes:
    """vvread <text> / cat | vvread の root dispatch テスト。"""

    def test_root_text_basic(self, voicevox_mock, tmp_path):
        """vvread "hello" → say と同じパイプラインで synth/play される。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread("hello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_root_text_with_speaker(self, voicevox_mock, tmp_path):
        """vvread "hello" --speaker 11 → speaker=11 で合成。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread("hello", "--speaker", "11", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=11" in req["path"]

    def test_root_text_command_name_dispatches_to_subcommand(self, tmp_path):
        """vvread "say" → "say" は subcommand 名なので say subcommand として dispatch。
        引数なしなので say の usage (exit 1) になる。"""
        r = run_vvread("say", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "Usage: vvread say" in r.stderr

    def test_root_no_args_tty_shows_usage(self, tmp_path):
        """vvread (引数なし、TTY) → usage (exit 1)。
        stdin_text=None の場合 subprocess は stdin を pipe せず TTY 扱いにはならないが、
        [ -t 0 ] は pytest 実行中でも偽になりうる。ここでは exit 1 だけ検証。"""
        r = run_vvread(env_extra=_path_env(tmp_path))
        assert r.returncode == 1

    def test_root_stdin_piped(self, voicevox_mock, tmp_path):
        """echo "hello" | vvread → stdin を読み上げる。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread(env_extra=env, stdin_text="hello")
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_root_stdin_with_speaker(self, voicevox_mock, tmp_path):
        """echo "text" | vvread --speaker 10 → speaker=10 で合成。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread("--speaker", "10", env_extra=env, stdin_text="hello")
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=10" in req["path"]

    def test_root_stdin_empty_exits_1(self, tmp_path):
        """stdin が空 → exit 1, 'stdin is empty'。"""
        r = run_vvread(env_extra=_path_env(tmp_path), stdin_text="")
        assert r.returncode == 1
        assert "stdin is empty" in r.stderr


# ---------------------------------------------------------------------------
# vvread file subcommand (B-002)
# ---------------------------------------------------------------------------


class TestFileSubcommand:
    """vvread file <path> のテスト。"""

    def test_file_basic(self, voicevox_mock, tmp_path):
        """vvread file path.txt → ファイル内容を synth/play。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        input_file = tmp_path / "input.txt"
        input_file.write_text("こんにちは")

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread("file", str(input_file), env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert marker.exists()

    def test_file_with_speaker(self, voicevox_mock, tmp_path):
        """vvread file path.txt --speaker 8 → speaker=8 で合成。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        input_file = tmp_path / "input.txt"
        input_file.write_text("テスト")

        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread("file", str(input_file), "--speaker", "8", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"
        for req in voicevox_mock["state"].requests:
            assert "speaker=8" in req["path"]

    def test_file_no_arg_exits_1(self, tmp_path):
        """vvread file (引数なし) → exit 1, '<file> is required'。"""
        r = run_vvread("file", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "<file> is required" in r.stderr

    def test_file_not_found_exits_1(self, tmp_path):
        """存在しないファイル → exit 1, 'file not found'。"""
        r = run_vvread("file", str(tmp_path / "nonexistent.txt"),
                       env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "file not found" in r.stderr

    def test_file_empty_exits_1(self, tmp_path):
        """空ファイル → exit 1, 'file is empty'。"""
        empty = tmp_path / "empty.txt"
        empty.write_text("")

        r = run_vvread("file", str(empty), env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert "file is empty" in r.stderr

    @pytest.mark.skipif(os.geteuid() == 0, reason="root では chmod 0000 が無効")
    def test_file_not_readable_exits_1(self, tmp_path):
        """読み取り不可ファイル → exit 1, 'file not readable'。POSIX のみ有効。"""
        unreadable = tmp_path / "unreadable.txt"
        unreadable.write_text("content")
        unreadable.chmod(0o000)
        try:
            r = run_vvread("file", str(unreadable), env_extra=_path_env(tmp_path))
            assert r.returncode == 1
            assert "file not readable" in r.stderr
        finally:
            unreadable.chmod(0o644)
