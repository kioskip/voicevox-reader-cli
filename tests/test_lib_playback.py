"""lib_playback.sh のテスト (R-002)

bash 関数を subprocess 経由で source して呼ぶスタイル。
PATH を fake bin dir に絞ることで実 OS の player バイナリを巻き込まず、
player ごとの優先順位 / 引数差分 / VVREAD_PLAYER override / pid_file 状態
の各経路を独立に検証する。

テストカバー範囲:
- vvread_detect_player: VVREAD_PLAYER 優先 / OS 別自動検出 / Linux 5 段の
  fallback / 全不在で exit 1 + 空 stdout
- _vvread_build_play_command: player ごとの引数差分(afplay / paplay /
  pw-play / aplay -q / play -q / ffplay -nodisp ... / 不明 player の素通し)
- vvread_play_async: wav 不在 / player 不在 / 正常起動 / fake player 起動失敗
- vvread_kill_play: pid_file 不在 / 空 / 不正 / 終了済み PID / 生存 PID
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_PLAYBACK = REPO / "scripts" / "lib" / "playback.sh"


def make_fake_player(
    bin_dir: Path,
    name: str,
    *,
    args_log: Path | None = None,
    touch_on_run: Path | None = None,
    long_running: bool = False,
    fail: bool = False,
):
    """偽の player バイナリを bin_dir に作る。

    args_log: 与えられた引数を 1 つ 1 行でこのファイルに append
    touch_on_run: 実行時にこのファイルを touch
    long_running: 60 秒 sleep する(kill テスト + bg 起動確認用)
    fail: exit 1 で即終了(起動失敗テスト用)
    """
    path = bin_dir / name
    lines = ["#!/bin/bash"]
    if args_log:
        lines.append(f'printf "%s\\n" "$@" >> "{args_log}"')
    if touch_on_run:
        lines.append(f'touch "{touch_on_run}"')
    if fail:
        lines.append("exit 1")
    if long_running:
        lines.append("exec sleep 60")
    lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def run_bash(env: dict, script: str) -> subprocess.CompletedProcess:
    """env を反映して lib_playback.sh を source し script を実行する。

    親プロセスから VVREAD_PLAYER を継承しないようクリアしておく
    (テスト環境を撹乱されないため)。

    set -e は付けない(各テストが `; echo "rc=$?"` で関数の戻り値を捕捉する
    パターンを取るため、非ゼロで早期 abort されると rc が読めない)。
    bash バイナリは絶対パスで指定し、env["PATH"] 設定の影響を受けないようにする。
    """
    base = os.environ.copy()
    for key in ("VVREAD_PLAYER",):
        base.pop(key, None)
    base.update(env)
    full = f'source "{LIB_PLAYBACK}"; {script}'
    return subprocess.run(
        ["/bin/bash", "-c", full],
        env=base,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bin_dir(tmp_path):
    """偽 player を置く bin dir。テストで PATH 先頭に置かれる。"""
    d = tmp_path / "bin"
    d.mkdir()
    return d


@pytest.fixture
def env(bin_dir):
    """default env: PATH を bin_dir + 最小限のシステム dir に絞る。

    /usr/bin は uname / sleep / kill 等の必須ツールのため残す。
    """
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


@pytest.fixture
def linux_env(env):
    """default env + Linux 強制 (`_vvread_is_macos() { return 1; }` を後から override)"""
    return env


def linux_prefix() -> str:
    """script の先頭で _vvread_is_macos を Linux 扱いに上書きする"""
    return "_vvread_is_macos() { return 1; }; "


# ---------------------------------------------------------------------------
# vvread_detect_player
# ---------------------------------------------------------------------------


class TestDetectPlayerVvreadPlayerOverride:
    def test_override_used_when_player_exists(self, env, bin_dir):
        make_fake_player(bin_dir, "ffplay")
        env["VVREAD_PLAYER"] = "ffplay"
        r = run_bash(env, "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ffplay"

    def test_override_does_not_fallback_when_missing(self, env):
        # 明示指定が見つからない場合は fallback せず exit 1 を返す
        env["VVREAD_PLAYER"] = "bogus_player_xyz_999"
        r = run_bash(env, "vvread_detect_player")
        assert r.returncode == 1
        assert r.stdout.strip() == ""

    def test_override_takes_priority_over_auto_detect(self, env, bin_dir):
        # afplay と ffplay 両方ある状況で VVREAD_PLAYER=ffplay → ffplay を選ぶ
        make_fake_player(bin_dir, "afplay")
        make_fake_player(bin_dir, "ffplay")
        env["VVREAD_PLAYER"] = "ffplay"
        r = run_bash(env, "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ffplay"

    def test_override_with_full_path(self, env, tmp_path, bin_dir):
        # 絶対パス指定も command -v で解決できる
        custom = tmp_path / "custom" / "myplayer"
        custom.parent.mkdir()
        custom.write_text("#!/bin/bash\nexit 0\n")
        custom.chmod(0o755)
        env["VVREAD_PLAYER"] = str(custom)
        r = run_bash(env, "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == str(custom)


class TestDetectPlayerMacosAuto:
    @pytest.mark.skipif(
        subprocess.run(["uname", "-s"], capture_output=True, text=True).stdout.strip() != "Darwin",
        reason="macOS ホストでのみ意味がある",
    )
    def test_macos_returns_afplay(self, env):
        # macOS の /usr/bin/afplay を拾う
        r = run_bash(env, "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "afplay"


class TestDetectPlayerLinuxAuto:
    """Linux の優先順位 5 段を _vvread_is_macos override で擬似テスト。"""

    def test_paplay_is_first_priority(self, linux_env, bin_dir):
        for name in ("paplay", "pw-play", "aplay", "play", "ffplay"):
            make_fake_player(bin_dir, name)
        r = run_bash(linux_env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "paplay"

    def test_pw_play_when_paplay_missing(self, linux_env, bin_dir):
        for name in ("pw-play", "aplay", "play", "ffplay"):
            make_fake_player(bin_dir, name)
        r = run_bash(linux_env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "pw-play"

    def test_aplay_when_paplay_pw_play_missing(self, linux_env, bin_dir):
        for name in ("aplay", "play", "ffplay"):
            make_fake_player(bin_dir, name)
        r = run_bash(linux_env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "aplay"

    def test_play_sox_fourth_priority(self, linux_env, bin_dir):
        for name in ("play", "ffplay"):
            make_fake_player(bin_dir, name)
        r = run_bash(linux_env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "play"

    def test_ffplay_last_resort(self, linux_env, bin_dir):
        make_fake_player(bin_dir, "ffplay")
        r = run_bash(linux_env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ffplay"


class TestDetectPlayerNotFound:
    def test_linux_no_players_returns_1(self, env, bin_dir):
        # bin_dir 空 + Linux 強制 + PATH を bin_dir のみに絞る
        env["PATH"] = str(bin_dir)
        r = run_bash(env, linux_prefix() + "vvread_detect_player")
        assert r.returncode == 1
        assert r.stdout.strip() == ""


# ---------------------------------------------------------------------------
# _vvread_build_play_command (player ごとの引数差分)
# ---------------------------------------------------------------------------


def _build_and_dump(env: dict, player: str, wav: str) -> list[str]:
    """_vvread_build_play_command を呼んで _vvread_play_cmd 配列を取得"""
    script = (
        f'_vvread_build_play_command "{player}" "{wav}"; '
        # 配列要素を 1 行ずつ出力
        'for el in "${_vvread_play_cmd[@]}"; do printf "%s\\n" "$el"; done'
    )
    r = run_bash(env, script)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


class TestBuildPlayCommand:
    def test_afplay(self, env):
        assert _build_and_dump(env, "afplay", "/tmp/x.wav") == ["afplay", "/tmp/x.wav"]

    def test_paplay(self, env):
        assert _build_and_dump(env, "paplay", "/tmp/x.wav") == ["paplay", "/tmp/x.wav"]

    def test_pw_play(self, env):
        assert _build_and_dump(env, "pw-play", "/tmp/x.wav") == ["pw-play", "/tmp/x.wav"]

    def test_aplay_has_q_flag(self, env):
        assert _build_and_dump(env, "aplay", "/tmp/x.wav") == ["aplay", "-q", "/tmp/x.wav"]

    def test_play_sox_has_q_flag(self, env):
        assert _build_and_dump(env, "play", "/tmp/x.wav") == ["play", "-q", "/tmp/x.wav"]

    def test_ffplay_silent_flags(self, env):
        # -nodisp -autoexit -loglevel quiet の 3 セット + wav パス
        assert _build_and_dump(env, "ffplay", "/tmp/x.wav") == [
            "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "/tmp/x.wav"
        ]

    def test_unknown_player_passes_through(self, env):
        # VVREAD_PLAYER で未知 player 指定された場合の素通しフォールバック
        assert _build_and_dump(env, "myplayer", "/tmp/x.wav") == ["myplayer", "/tmp/x.wav"]

    def test_wav_with_spaces(self, env):
        # 空白を含むパスもクォート保持
        assert _build_and_dump(env, "afplay", "/tmp/has space.wav") == [
            "afplay", "/tmp/has space.wav"
        ]


# ---------------------------------------------------------------------------
# vvread_play_async
# ---------------------------------------------------------------------------


def _kill_pid_safely(pid: int):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


class TestPlayAsync:
    def test_returns_2_when_wav_missing(self, env, bin_dir, tmp_path):
        make_fake_player(bin_dir, "afplay")
        env["VVREAD_PLAYER"] = "afplay"
        r = run_bash(
            env,
            f'vvread_play_async "{tmp_path}/no.wav" "{tmp_path}/pid"; echo "rc=$?"',
        )
        assert "rc=2" in r.stdout

    def test_returns_2_when_wav_empty(self, env, bin_dir, tmp_path):
        make_fake_player(bin_dir, "afplay")
        env["VVREAD_PLAYER"] = "afplay"
        wav = tmp_path / "empty.wav"
        wav.write_bytes(b"")
        r = run_bash(
            env,
            f'vvread_play_async "{wav}" "{tmp_path}/pid"; echo "rc=$?"',
        )
        assert "rc=2" in r.stdout

    def test_returns_1_when_no_player(self, env, bin_dir, tmp_path):
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"fake")
        env["PATH"] = str(bin_dir)  # bin_dir 空 + 他 PATH 削除
        r = run_bash(
            env,
            linux_prefix() + f'vvread_play_async "{wav}" "{tmp_path}/pid"; echo "rc=$?"',
        )
        assert "rc=1" in r.stdout

    def test_starts_player_and_writes_pid(self, env, bin_dir, tmp_path):
        # long_running fake で BG 残存させ、PID と marker を検証
        marker = tmp_path / "marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, long_running=True)
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"fake")
        pid_file = tmp_path / "pid"
        env["VVREAD_PLAYER"] = "afplay"

        r = run_bash(
            env,
            f'vvread_play_async "{wav}" "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout

        pid = int(pid_file.read_text().strip())
        try:
            # PID は数値で、起動直後は親プロセス(bash -c)子プロセスとして存在。
            # bash が抜けると orphan 化するが、sleep 60 で生存中。
            # marker が touched されたことを 1 秒以内に確認
            # busy CI で 1 秒では足りないケースがあるため 5 秒まで待つ
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists(), "fake player が走らなかった"
        finally:
            _kill_pid_safely(pid)

    def test_passes_correct_args_to_aplay(self, env, bin_dir, tmp_path):
        # aplay の `-q` フラグ + wav パスが渡されることを fake bin で検証
        args_log = tmp_path / "args.log"
        make_fake_player(bin_dir, "aplay", args_log=args_log, long_running=True)
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"fake")
        pid_file = tmp_path / "pid"
        env["VVREAD_PLAYER"] = "aplay"

        r = run_bash(
            env,
            f'vvread_play_async "{wav}" "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout

        pid = int(pid_file.read_text().strip())
        try:
            # args.log の生成を待つ(fake は touch 後 sleep 60)
            # busy CI で 1 秒では足りないケースがあるため 5 秒まで待つ
            for _ in range(100):
                if args_log.exists():
                    break
                time.sleep(0.05)
            assert args_log.exists()
            args = args_log.read_text().splitlines()
            assert args == ["-q", str(wav)]
        finally:
            _kill_pid_safely(pid)

    def test_passes_correct_args_to_ffplay(self, env, bin_dir, tmp_path):
        args_log = tmp_path / "args.log"
        make_fake_player(bin_dir, "ffplay", args_log=args_log, long_running=True)
        wav = tmp_path / "x.wav"
        wav.write_bytes(b"fake")
        pid_file = tmp_path / "pid"
        env["VVREAD_PLAYER"] = "ffplay"

        r = run_bash(
            env,
            f'vvread_play_async "{wav}" "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout

        pid = int(pid_file.read_text().strip())
        try:
            # busy CI で 1 秒では足りないケースがあるため 5 秒まで待つ
            for _ in range(100):
                if args_log.exists():
                    break
                time.sleep(0.05)
            assert args_log.exists()
            args = args_log.read_text().splitlines()
            assert args == ["-nodisp", "-autoexit", "-loglevel", "quiet", str(wav)]
        finally:
            _kill_pid_safely(pid)

    # 注: player exec 直後の即終了(command not found 等)の検出は本 lib では
    # 行わない方針(bash 3.2 で zombie/alive を区別する移植性ある手段が無いため)。
    # caller が `wait $pid` の exit code を見て検知する責務 — speak.sh の既存
    # パターンと整合する。当初予定していた return 3 は廃止。


# ---------------------------------------------------------------------------
# vvread_kill_play
# ---------------------------------------------------------------------------


class TestKillPlay:
    def test_missing_pid_file_is_noop(self, env, tmp_path):
        r = run_bash(
            env,
            f'vvread_kill_play "{tmp_path}/no_such_file"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout
        assert not (tmp_path / "no_such_file").exists()

    def test_empty_pid_file_is_removed(self, env, tmp_path):
        pid_file = tmp_path / "pid"
        pid_file.write_text("")
        r = run_bash(
            env,
            f'vvread_kill_play "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout
        assert not pid_file.exists()

    def test_non_numeric_pid_file_is_removed(self, env, tmp_path):
        pid_file = tmp_path / "pid"
        pid_file.write_text("not_a_number")
        r = run_bash(
            env,
            f'vvread_kill_play "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout
        assert not pid_file.exists()

    def test_dead_pid_returns_0_removes_file(self, env, tmp_path):
        pid_file = tmp_path / "pid"
        pid_file.write_text("99999999")  # 実用的に存在しない PID
        r = run_bash(
            env,
            f'vvread_kill_play "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout
        assert not pid_file.exists()

    def test_kills_alive_process(self, env, tmp_path):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            pid_file = tmp_path / "pid"
            pid_file.write_text(str(proc.pid))

            r = run_bash(
                env,
                f'vvread_kill_play "{pid_file}"; echo "rc=$?"',
            )
            assert "rc=0" in r.stdout
            assert not pid_file.exists()

            # 0.5 秒以内に死ぬはず
            for _ in range(10):
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            assert proc.poll() is not None, "対象プロセスが kill されていない"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_pid_with_leading_zero_is_handled(self, env, tmp_path):
        # "0" 単独は POSIX で意味があり危険(プロセスグループ全体に signal を送る)。
        # 我々の case パターン *[!0-9]* は "0" を通すが、PID 0 は kill -0 で
        # ESRCH が返る(macOS で確認)→ noop で安全
        pid_file = tmp_path / "pid"
        pid_file.write_text("0")
        r = run_bash(
            env,
            f'vvread_kill_play "{pid_file}"; echo "rc=$?"',
        )
        assert "rc=0" in r.stdout
        # pid_file は消える
        assert not pid_file.exists()
