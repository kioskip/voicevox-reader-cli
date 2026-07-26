"""voice.sh のテスト

各テストは独立した一時ディレクトリを STATE_DIR / LOG_DIR / CACHE_DIR
として voice.sh を実行する(R-003 で path resolver 経由に切替)。

`scripts/voice.sh` を fake project layout(`<tmp>/scripts/voice.sh` をコピー、
state / log / cache を `<tmp>/{state,log,cache}/` に配置)に置き、
VVREAD_*_DIR 環境変数で resolver の OS 既定値を上書きして呼び出す。

カバー範囲:
- voice clean(orphan 削除、現セッション保護、cache 不可侵、冪等性、usage)
- voice stop / mute / off / on / status(状態遷移と副作用)

旧 ${PROJECT_DIR}/tmp/ → 新 OS 別 dir への移行は test_paths.py で別途検証。
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


@pytest.fixture
def project(tmp_path):
    """テスト用の擬似 project ディレクトリを作る。

    voice.sh は VVREAD_*_DIR 環境変数で state/log/cache を切替できるため、
    テストでは tmp_path/{state,log,cache} に向ける。<tmp>/tmp/ は
    legacy migration の入力(voice.sh が起動時に呼ぶ vvread_migrate_legacy_tmp
    の対象)として空ディレクトリを置いておく。
    """
    proj = tmp_path
    scripts_dir = proj / "scripts"
    scripts_dir.mkdir()
    state_dir = proj / "state"
    state_dir.mkdir()
    log_dir = proj / "log"
    log_dir.mkdir()
    cache_dir = proj / "cache"
    cache_dir.mkdir()
    legacy_tmp = proj / "tmp"
    legacy_tmp.mkdir()

    # voice.sh と依存 lib をコピー(シンボリックリンクだと異なる FS でテストが
    # 不安定なので素直にコピーする)
    shutil.copy(SCRIPTS / "voice.sh", scripts_dir / "voice.sh")
    (scripts_dir / "voice.sh").chmod(0o755)
    (scripts_dir / "lib").mkdir()
    for libname in ("log.sh", "os.sh", "paths.sh", "queue.sh", "duration.sh"):
        shutil.copy(SCRIPTS / "lib" / libname, scripts_dir / "lib" / libname)

    return {
        "ROOT": proj,
        "STATE": state_dir,
        "LOG": log_dir,
        "CACHE": cache_dir,
        "LEGACY_TMP": legacy_tmp,
        "VOICE": scripts_dir / "voice.sh",
    }


def run_voice(project, *args, env_extra=None):
    base = os.environ.copy()
    base["VVREAD_STATE_DIR"] = str(project["STATE"])
    base["VVREAD_LOG_DIR"] = str(project["LOG"])
    base["VVREAD_CACHE_DIR"] = str(project["CACHE"])
    if env_extra:
        base.update(env_extra)
    return subprocess.run(
        [str(project["VOICE"]), *args],
        env=base,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# voice clean
# ---------------------------------------------------------------------------


class TestVoiceClean:
    def test_removes_orphan_voice_files(self, project):
        """別セッションの voice_*.wav / .query.json / .tuned を一括削除する"""
        state = project["STATE"]
        # 別セッション(ABCDE)の orphan を 3 種類置く
        (state / "voice_ABCDE_0.wav").write_bytes(b"")
        (state / "voice_ABCDE_0.wav.query.json").write_text("{}")
        (state / "voice_ABCDE_0.wav.query.json.tuned").write_text("{}")

        # session.id は別セッション(現在は何も再生していない想定で空)
        # → __none__ 扱いで全 voice_* が削除対象になる

        r = run_voice(project, "clean")
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "removed 3 file(s)" in r.stdout

        for name in (
            "voice_ABCDE_0.wav",
            "voice_ABCDE_0.wav.query.json",
            "voice_ABCDE_0.wav.query.json.tuned",
        ):
            assert not (state / name).exists(), f"{name} が残っている"

    def test_preserves_current_session_files(self, project):
        """state/session.id にマッチする voice_${current}_* は残し、それ以外は消す"""
        state = project["STATE"]
        # 現セッション
        (state / "session.id").write_text("CURRENT_xyz")
        (state / "voice_CURRENT_xyz_0.wav").write_bytes(b"keep")
        (state / "voice_CURRENT_xyz_0.wav.query.json").write_text("{}")

        # 旧セッション(消えるべき)
        (state / "voice_OLD_abc_0.wav").write_bytes(b"orphan")
        (state / "voice_OLD_abc_0.wav.query.json").write_text("{}")

        r = run_voice(project, "clean")
        assert r.returncode == 0, f"stderr={r.stderr}"

        # 現セッションは残る
        assert (state / "voice_CURRENT_xyz_0.wav").exists()
        assert (state / "voice_CURRENT_xyz_0.wav.query.json").exists()
        # 旧セッションは消えている
        assert not (state / "voice_OLD_abc_0.wav").exists()
        assert not (state / "voice_OLD_abc_0.wav.query.json").exists()

    def test_removes_cached_wav(self, project):
        """CACHE_DIR 配下の *.wav を削除する（T-012）"""
        cache_file = project["CACHE"] / "spk3_abcd1234.wav"
        cache_file.write_bytes(b"x")

        # 一緒に orphan も置いて clean が走る状態を作る
        (project["STATE"] / "voice_OLD_0.wav").write_bytes(b"")

        r = run_voice(project, "clean")
        assert r.returncode == 0
        assert not cache_file.exists(), "cache が削除されていない"
        assert not (project["STATE"] / "voice_OLD_0.wav").exists()

    def test_does_not_remove_non_wav_from_cache(self, project):
        """CACHE_DIR 配下の wav 以外のファイルは残す"""
        non_wav = project["CACHE"] / "metadata.json"
        non_wav.write_text("{}")

        r = run_voice(project, "clean")
        assert r.returncode == 0
        assert non_wav.exists(), "wav 以外が削除されている"

    def test_removes_legacy_query_json(self, project):
        """旧 QUERY_PREFIX 形式の query_*.json / .tuned を削除する(S-001 以前の遺物)"""
        state = project["STATE"]
        (state / "query_12412.json").write_text("{}")
        (state / "query_1777571801255_55147_3.json").write_text("{}")
        (state / "query_38217.json.tuned").write_text("{}")

        r = run_voice(project, "clean")
        assert r.returncode == 0

        for name in (
            "query_12412.json",
            "query_1777571801255_55147_3.json",
            "query_38217.json.tuned",
        ):
            assert not (state / name).exists(), f"legacy {name} が残っている"

    def test_does_not_touch_unprefixed_files(self, project):
        """voice_ / query_ で始まらないファイルは削除しない(ユーザーが置いた可能性あり)"""
        state = project["STATE"]
        # ユーザーが意図的に置いた可能性のある名前
        (state / "test.wav").write_bytes(b"keep me")
        (state / "query.json").write_text("{}")  # prefix 無し
        (state / "speak.lock").touch()
        # 一緒に orphan も置いて clean が走る状態を作る
        (state / "voice_OLD_0.wav").write_bytes(b"")

        r = run_voice(project, "clean")
        assert r.returncode == 0

        for name in ("test.wav", "query.json", "speak.lock"):
            assert (state / name).exists(), f"{name} が誤って消されている"
        assert not (state / "voice_OLD_0.wav").exists()

    def test_does_not_touch_state_files(self, project):
        """session.id / playing.pid / disabled / mute_until / last_notify は触らない"""
        state = project["STATE"]
        # 状態ファイル群
        (state / "session.id").write_text("ABC")
        (state / "playing.pid").write_text("12345")
        (state / "disabled").touch()
        (state / "mute_until").write_text("9999999999")
        (state / "last_notify").write_text("100")

        # トリガーとして orphan を 1 つ
        (state / "voice_OTHER_0.wav").write_bytes(b"")

        r = run_voice(project, "clean")
        assert r.returncode == 0

        for name in ("session.id", "playing.pid", "disabled", "mute_until", "last_notify"):
            assert (state / name).exists(), f"{name} が消されている"

    def test_idempotent_on_empty_state(self, project):
        """対象ファイルが何もない状態で clean を実行しても exit 0、かつ "nothing to clean."""
        # 状態ファイルすら無い完全クリーン状態
        r = run_voice(project, "clean")
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "nothing to clean" in r.stdout

    def test_logs_to_log_dir(self, project):
        """voice clean の実行ログが LOG_DIR/speak.log に書かれる"""
        (project["STATE"] / "voice_OLD_0.wav").write_bytes(b"")

        r = run_voice(project, "clean")
        assert r.returncode == 0

        log_file = project["LOG"] / "speak.log"
        assert log_file.exists(), "speak.log が作られていない"
        content = log_file.read_text()
        assert "voice.INFO" in content
        assert "clean files=1" in content

    def test_removes_orphan_files_with_space_in_state_dir(self, project):
        """STATE_DIR にスペースを含む場合でも実際に削除される(L-1 回帰テスト)。

        macOS の既定 STATE_DIR は `~/Library/Application Support/vvread` で
        スペースを含む。旧実装の `printf '%s\\n' "${matches}" | xargs rm -f`
        は word-split によりスペース区切りで別引数化され、実際には何も
        削除されないのに「removed N file(s)」と表示する機能バグがあった。
        """
        spaced_state = project["ROOT"] / "Application Support" / "vvread"
        spaced_state.mkdir(parents=True)
        (spaced_state / "voice_OLD_0.wav").write_bytes(b"")
        (spaced_state / "voice_OLD_0.wav.query.json").write_text("{}")

        base = os.environ.copy()
        base["VVREAD_STATE_DIR"] = str(spaced_state)
        base["VVREAD_LOG_DIR"] = str(project["LOG"])
        base["VVREAD_CACHE_DIR"] = str(project["CACHE"])
        r = subprocess.run(
            [str(project["VOICE"]), "clean"],
            env=base,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "removed 2 file(s)" in r.stdout
        assert not (spaced_state / "voice_OLD_0.wav").exists(), \
            "スペース含み STATE_DIR で実削除されていない"
        assert not (spaced_state / "voice_OLD_0.wav.query.json").exists()


# ---------------------------------------------------------------------------
# voice stop
# ---------------------------------------------------------------------------


def _spawn_dummy_process():
    """テスト用に長く生きるプロセスを起動して Popen を返す。
    終了確認を呼び出し側で行いやすいよう Popen のまま渡す。"""
    return subprocess.Popen(["sleep", "60"])


class TestVoiceStop:
    def test_kills_playing_process_and_clears_pid_file(self, project):
        state = project["STATE"]
        proc = _spawn_dummy_process()
        try:
            (state / "playing.pid").write_text(str(proc.pid))
            (state / "session.id").write_text("ABC")

            r = run_voice(project, "stop")
            assert r.returncode == 0
            assert "stopped" in r.stdout

            # playing.pid は消える
            assert not (state / "playing.pid").exists()

            # 実際にプロセスが死んでいる
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pytest.fail("対象プロセスが kill されていない")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_invalidates_session_id(self, project):
        state = project["STATE"]
        (state / "session.id").write_text("ABC")

        r = run_voice(project, "stop")
        assert r.returncode == 0

        # session.id が "stopped_<epoch>" で上書きされる
        new_session = (state / "session.id").read_text().strip()
        assert new_session.startswith("stopped_")
        assert new_session != "ABC"

    def test_no_playing_pid_is_safe(self, project):
        # playing.pid が存在しない状態で stop を呼んでもエラーにならない
        r = run_voice(project, "stop")
        assert r.returncode == 0

    def test_dead_pid_in_file_is_handled(self, project):
        state = project["STATE"]
        # 不存在 PID(99999999 は実用的に存在しない)
        (state / "playing.pid").write_text("99999999")

        r = run_voice(project, "stop")
        assert r.returncode == 0
        assert not (state / "playing.pid").exists()

    def test_pid_zero_is_not_killed(self, project):
        """playing.pid が "0" に汚染されていても `kill 0`
        (呼出側プロセスグループ全体への SIGTERM)を発行しない(L-2 回帰テスト)。

        voice.sh を独立プロセスグループ(start_new_session)で起動し、ガードが
        欠落していた場合の実害(自プロセスグループへの自傷 SIGTERM)を
        テストランナーから隔離しつつ検出する: ガードが無いと voice.sh
        自身が SIGTERM を受けて signal 起因の異常終了(returncode < 0)になる。
        """
        state = project["STATE"]
        (state / "playing.pid").write_text("0")
        (state / "session.id").write_text("ABC")

        base = os.environ.copy()
        base["VVREAD_STATE_DIR"] = str(project["STATE"])
        base["VVREAD_LOG_DIR"] = str(project["LOG"])
        base["VVREAD_CACHE_DIR"] = str(project["CACHE"])
        r = subprocess.run(
            [str(project["VOICE"]), "stop"],
            env=base,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=10,
        )
        assert r.returncode == 0, f"stderr={r.stderr} returncode={r.returncode}"
        assert "stopped" in r.stdout
        assert not (state / "playing.pid").exists()


# ---------------------------------------------------------------------------
# voice stop / mute / off × queue_mode 連携 (F-128)
#
# cmd_stop / cmd_mute / cmd_off は共通 helper `_queue_stop_if_active` を通じて
# queue 全停止（pending 削除 + drainer への stop signal）を担う。判定基準は
# `${STATE_DIR}/queue_mode` フラグ OR `${STATE_DIR}/queue` ディレクトリ存在の
# OR 条件(per-call queue は queue_mode フラグを触らないため、フラグのみの
# 判定だと per-call queue 使用中の停止要求が届かなくなる回帰があった)。
# ここでは観測可能な副作用(ファイルの有無)でその挙動を検証する。
# ---------------------------------------------------------------------------


def _seed_queue_dirs(state):
    """queue の pending/playing/failed ディレクトリを用意する。"""
    for name in ("pending", "playing", "failed"):
        (state / "queue" / name).mkdir(parents=True, exist_ok=True)


class TestVoiceQueueStopIntegration:
    @pytest.mark.parametrize("subcmd,args", [
        ("stop", []),
        ("mute", ["30s"]),
        ("off", []),
    ])
    def test_clears_pending_when_queue_mode_active(self, project, subcmd, args):
        """queue_mode フラグ ON + pending entry がある状態で stop/mute/off の
        いずれを実行しても pending が削除される(vvread_queue_stop_request の
        副作用)。"""
        state = project["STATE"]
        (state / "queue_mode").touch()
        _seed_queue_dirs(state)
        (state / "queue" / "pending" / "100_entry").write_text("pending")

        r = run_voice(project, subcmd, *args)
        assert r.returncode == 0, f"stderr={r.stderr}"

        assert not (state / "queue" / "pending" / "100_entry").exists()

    @pytest.mark.parametrize("subcmd,args", [
        ("stop", []),
        ("mute", ["30s"]),
        ("off", []),
    ])
    def test_preserves_queue_mode_flag(self, project, subcmd, args):
        """stop/mute/off は queue_mode フラグ自体を削除しない
        (`vvread queue off` でのみ削除する既存の責務分担を維持する)。"""
        state = project["STATE"]
        (state / "queue_mode").touch()
        _seed_queue_dirs(state)

        r = run_voice(project, subcmd, *args)
        assert r.returncode == 0, f"stderr={r.stderr}"

        assert (state / "queue_mode").exists()

    @pytest.mark.parametrize("subcmd,args", [
        ("stop", []),
        ("mute", ["30s"]),
        ("off", []),
    ])
    def test_no_queue_processing_when_flag_and_dir_absent(self, project, subcmd, args):
        """queue_mode フラグも queue ディレクトリも無ければ queue 処理
        (vvread_queue_dirs_init 等)は走らない(OR 条件の両辺が偽の場合)。
        フラグ・ディレクトリいずれも無い状態なら、queue ディレクトリが新規
        作成されないことを確認する。"""
        state = project["STATE"]
        assert not (state / "queue_mode").exists()
        assert not (state / "queue").exists()

        r = run_voice(project, subcmd, *args)
        assert r.returncode == 0, f"stderr={r.stderr}"

        assert not (state / "queue").exists()

    @pytest.mark.parametrize("subcmd,args", [
        ("stop", []),
        ("mute", ["30s"]),
        ("off", []),
    ])
    def test_clears_pending_when_queue_dir_exists_without_flag(self, project, subcmd, args):
        """per-call queue (`say --queue` / `VVREAD_SAY_QUEUE=1`) は queue_mode
        フラグを一切触らずに queue ディレクトリだけを作る。OR 条件により、
        フラグが無くても queue ディレクトリさえ存在すれば stop/mute/off で
        pending が削除される(F-128 で `-d` → `-f` に変えた際の回帰の修正確認)。"""
        state = project["STATE"]
        assert not (state / "queue_mode").exists()
        _seed_queue_dirs(state)
        (state / "queue" / "pending" / "100_entry").write_text("pending")

        r = run_voice(project, subcmd, *args)
        assert r.returncode == 0, f"stderr={r.stderr}"

        assert not (state / "queue" / "pending" / "100_entry").exists()

    @pytest.mark.parametrize("mute_args,expected_stderr", [
        (["30x"], "duration"),  # duration の形式が不正
        ([], "Usage"),          # 引数無し(既存の Usage エラー)
    ])
    def test_mute_invalid_duration_does_not_touch_queue(
        self, project, mute_args, expected_stderr,
    ):
        """duration 不正 / 引数無しの早期 exit は queue 処理に一切触れない
        (queue_mode があっても Usage / duration エラーで即 exit するパスは
        変えていない)。"""
        state = project["STATE"]
        (state / "queue_mode").touch()
        _seed_queue_dirs(state)
        (state / "queue" / "pending" / "100_entry").write_text("pending")

        r = run_voice(project, "mute", *mute_args)

        assert r.returncode == 1
        assert expected_stderr in r.stderr
        # queue には一切触れていない: pending も queue_mode も無傷
        assert (state / "queue" / "pending" / "100_entry").exists()
        assert (state / "queue_mode").exists()

    def test_wedge_warning_on_stop_when_queue_mode_active(self, project):
        """queue_mode ON + wedge 状態の queue.lock がある場合、stop 実行時に
        WARN が出る(判定基準を dir → queue_mode flag に変えても維持されること)。"""
        state = project["STATE"]
        (state / "queue_mode").touch()
        _seed_queue_dirs(state)
        (state / "queue" / "pending" / "100_entry").write_text("pending")
        lock_dir = state / "queue" / "queue.lock"
        lock_dir.mkdir()
        # owner: 自プロセス(pytest)の pid を使い kill -0 を常に成功させる
        (lock_dir / "owner").write_text(f"{os.getpid()}\t{os.getpid()}.1.1\n")
        # hb / progress を古い epoch にして stale 扱いにする(wedge 判定)
        (lock_dir / "hb").write_text("100\n")
        (lock_dir / "progress").write_text("100\n")

        r = run_voice(project, "stop")

        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "wedged" in r.stderr


# ---------------------------------------------------------------------------
# voice mute
# ---------------------------------------------------------------------------


class TestVoiceMute:
    def test_30s_writes_mute_until(self, project):
        state = project["STATE"]
        before = int(time.time())
        r = run_voice(project, "mute", "30s")
        assert r.returncode == 0, f"stderr={r.stderr}"
        after = int(time.time())

        until = int((state / "mute_until").read_text().strip())
        # before+30 から after+30 の範囲に収まるはず
        assert before + 30 <= until <= after + 30

    def test_5m_supports_minutes(self, project):
        state = project["STATE"]
        before = int(time.time())
        r = run_voice(project, "mute", "5m")
        assert r.returncode == 0
        until = int((state / "mute_until").read_text().strip())
        assert until - before >= 5 * 60 - 2 and until - before <= 5 * 60 + 2

    def test_2h_supports_hours(self, project):
        state = project["STATE"]
        before = int(time.time())
        r = run_voice(project, "mute", "2h")
        assert r.returncode == 0
        until = int((state / "mute_until").read_text().strip())
        assert until - before >= 2 * 3600 - 2 and until - before <= 2 * 3600 + 2

    def test_invalid_duration_format_fails(self, project):
        r = run_voice(project, "mute", "30x")
        assert r.returncode == 1
        assert "duration" in r.stderr

    def test_missing_argument_fails(self, project):
        r = run_voice(project, "mute")
        assert r.returncode == 1
        assert "Usage" in r.stderr

    def test_mute_also_invalidates_session(self, project):
        # stop と同じ副作用が走ること
        state = project["STATE"]
        (state / "session.id").write_text("ABC")
        r = run_voice(project, "mute", "1m")
        assert r.returncode == 0
        new_session = (state / "session.id").read_text().strip()
        assert new_session.startswith("stopped_")


# ---------------------------------------------------------------------------
# voice unmute
# ---------------------------------------------------------------------------


class TestVoiceUnmute:
    def test_removes_mute_until(self, project):
        state = project["STATE"]
        (state / "mute_until").write_text("9999999999")

        r = run_voice(project, "unmute")

        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "ミュートを解除しました" in r.stdout
        assert not (state / "mute_until").exists()

    def test_preserves_disabled_flag(self, project):
        state = project["STATE"]
        (state / "disabled").touch()
        (state / "mute_until").write_text("9999999999")

        r = run_voice(project, "unmute")

        assert r.returncode == 0, f"stderr={r.stderr}"
        assert (state / "disabled").exists()
        assert not (state / "mute_until").exists()

    def test_idempotent_without_mute_until(self, project):
        r = run_voice(project, "unmute")
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert "ミュートを解除しました" in r.stdout


# ---------------------------------------------------------------------------
# voice off
# ---------------------------------------------------------------------------


class TestVoiceOff:
    def test_creates_disabled_flag(self, project):
        state = project["STATE"]
        r = run_voice(project, "off")
        assert r.returncode == 0
        assert (state / "disabled").exists()

    def test_off_invalidates_session(self, project):
        state = project["STATE"]
        (state / "session.id").write_text("ABC")
        r = run_voice(project, "off")
        assert r.returncode == 0
        new_session = (state / "session.id").read_text().strip()
        assert new_session.startswith("stopped_")


# ---------------------------------------------------------------------------
# voice on
# ---------------------------------------------------------------------------


class TestVoiceOn:
    def test_removes_disabled_flag(self, project):
        state = project["STATE"]
        (state / "disabled").touch()
        r = run_voice(project, "on")
        assert r.returncode == 0
        assert not (state / "disabled").exists()

    def test_removes_mute_until_flag(self, project):
        state = project["STATE"]
        (state / "mute_until").write_text("9999999999")
        r = run_voice(project, "on")
        assert r.returncode == 0
        assert not (state / "mute_until").exists()

    def test_idempotent_when_no_flags(self, project):
        # 何も無くても OK
        r = run_voice(project, "on")
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# voice status
# ---------------------------------------------------------------------------


class TestVoiceStatus:
    def test_idle_by_default(self, project):
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: idle" in r.stdout

    def test_disabled_when_flag_present(self, project):
        state = project["STATE"]
        (state / "disabled").touch()
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: disabled" in r.stdout

    def test_muted_when_mute_until_in_future(self, project):
        state = project["STATE"]
        future = int(time.time()) + 300
        (state / "mute_until").write_text(str(future))
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: muted" in r.stdout

    def test_idle_when_mute_until_expired_and_file_cleaned(self, project):
        state = project["STATE"]
        past = int(time.time()) - 100
        (state / "mute_until").write_text(str(past))
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: idle" in r.stdout
        # 期限切れの mute_until は自動削除される
        assert not (state / "mute_until").exists()

    def test_playing_when_pid_alive(self, project):
        state = project["STATE"]
        proc = _spawn_dummy_process()
        try:
            (state / "playing.pid").write_text(str(proc.pid))
            r = run_voice(project, "status")
            assert r.returncode == 0
            assert "state: playing" in r.stdout
            assert f"pid={proc.pid}" in r.stdout
        finally:
            proc.kill()
            proc.wait()

    def test_idle_when_pid_is_dead(self, project):
        state = project["STATE"]
        # 不存在 PID
        (state / "playing.pid").write_text("99999999")
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: idle" in r.stdout

    def test_idle_when_pid_is_zero(self, project):
        """playing.pid が "0" の場合 alive とみなさない(L-2 回帰テスト)。"""
        state = project["STATE"]
        (state / "playing.pid").write_text("0")
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: idle" in r.stdout

    def test_disabled_takes_precedence_over_mute(self, project):
        # disabled と mute_until が両方あったら disabled が優先
        state = project["STATE"]
        (state / "disabled").touch()
        (state / "mute_until").write_text(str(int(time.time()) + 300))
        r = run_voice(project, "status")
        assert r.returncode == 0
        assert "state: disabled" in r.stdout


# ---------------------------------------------------------------------------
# voice status --json
# ---------------------------------------------------------------------------


class TestVoiceStatusJson:
    @staticmethod
    def _load_result(r):
        assert r.returncode == 0, f"stderr={r.stderr}"
        assert len(r.stdout.splitlines()) == 1
        return json.loads(r.stdout)

    def test_idle_with_missing_queue_directories(self, project):
        r = run_voice(project, "status", "--json")

        assert self._load_result(r) == {
            "state": "idle",
            "mute_until": None,
            "queue": {
                "mode": "off",
                "pending": 0,
                "playing": 0,
                "failed": 0,
            },
        }
        assert r.stdout == (
            '{"state": "idle", "mute_until": null, '
            '"queue": {"mode": "off", "pending": 0, '
            '"playing": 0, "failed": 0}}\n'
        )

    def test_expired_mute_until_is_null_and_cleaned(self, project):
        state = project["STATE"]
        (state / "mute_until").write_text(str(int(time.time()) - 100))

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["state"] == "idle"
        assert result["mute_until"] is None
        assert r.stderr == ""
        assert not (state / "mute_until").exists()

    def test_invalid_mute_until_is_null_with_warning(self, project):
        state = project["STATE"]
        (state / "mute_until").write_text("not-an-epoch")

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["state"] == "idle"
        assert result["mute_until"] is None
        assert "警告" in r.stderr
        assert "not-an-epoch" in r.stderr

    def test_negative_mute_until_is_null_with_warning(self, project):
        state = project["STATE"]
        (state / "mute_until").write_text("-1")

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["mute_until"] is None
        assert "警告" in r.stderr

    def test_disabled_preserves_valid_mute_until_value(self, project):
        state = project["STATE"]
        future = int(time.time()) + 300
        (state / "disabled").touch()
        (state / "mute_until").write_text(str(future))

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["state"] == "disabled"
        assert result["mute_until"] == future

    def test_leading_zero_epoch_is_valid_json_number(self, project):
        state = project["STATE"]
        future = int(time.time()) + 300
        (state / "mute_until").write_text(f"00{future}")

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["state"] == "muted"
        assert result["mute_until"] == future

    def test_stale_playing_pid_is_idle(self, project):
        state = project["STATE"]
        (state / "playing.pid").write_text("99999999")

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["state"] == "idle"

    def test_queue_mode_and_valid_entry_counts(self, project):
        state = project["STATE"]
        (state / "queue_mode").touch()
        for name in ("pending", "playing", "failed"):
            (state / "queue" / name).mkdir(parents=True, exist_ok=True)
        (state / "queue" / "pending" / "100_entry").write_text("pending")
        (state / "queue" / "pending" / "ignored.tmp.1").write_text("temporary")
        (state / "queue" / "playing" / "200_entry").write_text("playing")
        (state / "queue" / "failed" / "300_entry").write_text("failed")
        (state / "queue" / "failed" / "301_entry").write_text("failed")

        r = run_voice(project, "status", "--json")

        result = self._load_result(r)
        assert result["queue"] == {
            "mode": "on",
            "pending": 1,
            "playing": 1,
            "failed": 2,
        }


# ---------------------------------------------------------------------------
# usage / unknown subcommand
# ---------------------------------------------------------------------------


class TestVoiceUsage:
    def test_clean_appears_in_usage(self, project):
        r = run_voice(project)  # 引数無し → usage
        assert r.returncode == 1
        assert "clean" in r.stderr

    def test_all_subcommands_appear_in_usage(self, project):
        r = run_voice(project)
        for cmd in ("stop", "mute", "unmute", "off", "on", "status", "clean"):
            assert cmd in r.stderr, f"usage に {cmd} が無い"

    def test_unknown_subcommand_shows_usage(self, project):
        r = run_voice(project, "bogus")
        assert r.returncode == 1
        assert "unknown command" in r.stderr
