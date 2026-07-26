"""scripts/cmd/say.sh のキュー再生モード統合テスト (B-015 WS-A3 / B-144 WS-A)

queue モードの routing（preempt vs queue）・drain 完了・speaker 尊重・順序再生を
フェイク VOICEVOX engine + フェイク player で検証する。

routing 判定シグナル: queue モードに入ると say.sh が vvread_queue_dirs_init で
${STATE_DIR}/queue/ を作る。preempt パスはこれを作らない。この有無で経路を判別。
"""
import os
import signal
import subprocess
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from conftest import wait_for_file
from test_cmd_say import make_fake_player, _say_env, run_say

REPO = Path(__file__).resolve().parent.parent
CMD_SAY = REPO / "scripts" / "cmd" / "say.sh"
VVREAD = REPO / "bin" / "vvread"


@contextmanager
def managed_process(args, **kwargs):
    """Popen をプロセスグループ付きで起動し、with を抜ける際に確実に終了する。

    `_wait_until` のタイムアウト等で待機が失敗して with を抜けても、drainer と
    fake player 子孫（同一プロセスグループ）を SIGTERM→SIGKILL で停止し
    孤児プロセスを残さない。macOS / Linux 前提。
    """
    proc = subprocess.Popen(args, start_new_session=True, **kwargs)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)


def _enable_queue(env: dict):
    """state ディレクトリに queue_mode フラグを作成して queue モードを有効化。"""
    state = Path(env["VVREAD_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "queue_mode").write_text("")


def _queue_dir(env: dict) -> Path:
    return Path(env["VVREAD_STATE_DIR"]) / "queue"


def _count(env: dict, sub: str) -> int:
    d = _queue_dir(env) / sub
    if not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir()
               if f.is_file() and not f.is_symlink() and ".tmp." not in f.name)


def _audio_query_texts(state) -> list:
    """記録された audio_query リクエストから text パラメータを順番に抽出。"""
    out = []
    for req in state.requests:
        if "/audio_query" not in req["path"]:
            continue
        q = urllib.parse.urlparse(req["path"]).query
        params = urllib.parse.parse_qs(q)
        if "text" in params:
            out.append(params["text"][0])
    return out


# ---------------------------------------------------------------------------
# routing（queue vs preempt）
# ---------------------------------------------------------------------------

class TestRouting:
    def test_queue_flag_routes_to_queue_and_drains(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr
        # queue 経路を通った（queue ディレクトリが作られた）
        assert _queue_dir(env).is_dir()
        # drain 完了でキューは空
        assert _count(env, "pending") == 0
        assert _count(env, "playing") == 0
        # 実際に合成・再生された
        assert marker.exists()
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 1

    def test_no_queue_flag_overrides_to_preempt(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)  # flag on だが --no-queue で上書き

        r = run_say("あいうえお", "--no-queue", env_extra=env)
        assert r.returncode == 0, r.stderr
        # preempt パス: queue ディレクトリは作られない
        assert not _queue_dir(env).is_dir()
        # preempt の痕跡（session.id）
        assert (Path(env["VVREAD_STATE_DIR"]) / "session.id").exists()

    def test_queue_flag_arg_routes_to_queue_without_state_flag(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        # state flag なし + --queue
        r = run_say("あいうえお", "--queue", env_extra=env)
        assert r.returncode == 0, r.stderr
        assert _queue_dir(env).is_dir()

    def test_env_queue_0_overrides_flag(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        env["VVREAD_SAY_QUEUE"] = "0"
        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr
        assert not _queue_dir(env).is_dir()

    def test_env_queue_1_routes_to_queue(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        env["VVREAD_SAY_QUEUE"] = "1"
        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr
        assert _queue_dir(env).is_dir()

    def test_env_queue_invalid_falls_back_to_flag(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        env["VVREAD_SAY_QUEUE"] = "bogus"  # 不正値 → flag(on) へ fallback
        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr
        assert _queue_dir(env).is_dir()


# ---------------------------------------------------------------------------
# speaker 尊重
# ---------------------------------------------------------------------------

class TestSpeaker:
    def test_queue_respects_speaker_flag(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        r = run_say("あいうえお", "--speaker", "11", env_extra=env)
        assert r.returncode == 0, r.stderr
        reqs = [req for req in voicevox_mock["state"].requests
                if "/synthesis" in req["path"] or "/audio_query" in req["path"]]
        assert reqs, "no synth/query requests recorded"
        for req in reqs:
            assert "speaker=11" in req["path"]


# ---------------------------------------------------------------------------
# 順序再生（B-015 の核心）
# ---------------------------------------------------------------------------

class TestOrdering:
    def test_two_says_play_in_order(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        # player を遅延させ、1 件目の drain 中に 2 件目を積む窓を作る
        make_fake_player(bin_dir, "afplay", sleep_seconds=0.4, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        # 1 件目をバックグラウンドで起動（drainer になる）
        with managed_process(
            [str(CMD_SAY), "あいうえお"],
            env=_clean_env_for(env),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as first:
            # 1 件目の audio_query が記録されるまで待つ（drain 開始）
            _wait_until(lambda: any("/audio_query" in r["path"]
                                    for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 2 件目を積む（drainer 稼働中なので enqueue して即終了）
            r2 = run_say("かきくけこ", env_extra=env)
            assert r2.returncode == 0, r2.stderr
            first.wait(timeout=30)

        texts = _audio_query_texts(voicevox_mock["state"])
        # 両方が合成され、1 件目が先
        assert "あいうえお" in texts, texts
        assert "かきくけこ" in texts, texts
        assert texts.index("あいうえお") < texts.index("かきくけこ"), texts


class TestDoubleFire:
    def test_playing_summary_completes_then_full_plays(self, voicevox_mock, tmp_path):
        """(6): 再生中の要約(mcp)は drop されず完走し、全文(hook)が後に再生される。

        再生中（playing）の mcp は hook の eviction 対象外（evict は pending のみ）。
        ＝ jarring cut を避け「要約 → 全文」の順序を保つ。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", sleep_seconds=0.4, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        # 要約(mcp)をバックグラウンドで起動 → drainer になり再生開始
        mcp_env = _clean_env_for(env)
        mcp_env["VVREAD_SAY_SOURCE"] = "mcp"
        mcp_env["VVREAD_SAY_CREATED_MS"] = "99999999999999"
        with managed_process(
            [str(CMD_SAY), "ようやくのよう"],
            env=mcp_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as first:
            _wait_until(lambda: any("/audio_query" in r["path"]
                                    for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 全文(hook)を投入（再生中の mcp は drop されない）
            hook_env = dict(env)
            hook_env["VVREAD_SAY_SOURCE"] = "hook"
            r2 = run_say("ぜんぶんのほう", env_extra=hook_env)
            assert r2.returncode == 0, r2.stderr
            first.wait(timeout=30)

        texts = _audio_query_texts(voicevox_mock["state"])
        assert "ようやくのよう" in texts, texts   # 要約は完走（drop されない）
        assert "ぜんぶんのほう" in texts, texts   # 全文も再生
        assert texts.index("ようやくのよう") < texts.index("ぜんぶんのほう"), texts


class TestStopHalt:
    def test_stop_halts_live_drainer_midway(self, voicevox_mock, tmp_path):
        """vvread stop が再生中 drainer を halt し、残り chunk を再生しない。

        queue_mode フラグは維持され、pending は空になる。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", sleep_seconds=0.5, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        # 3 chunk 以上になる長文
        from test_cmd_say import count_expected_chunks
        text = "テストです。" * 60
        total = count_expected_chunks(text, speaker="3")
        assert total >= 3, total

        with managed_process(
            [str(CMD_SAY), text],
            env=_clean_env_for(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as drainer:
            _wait_until(lambda: any("/audio_query" in r["path"]
                                    for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 再生中に stop（live drainer へ halt signal + player kill）
            stop = subprocess.run(
                [str(VVREAD), "stop"], env=_clean_env_for(env),
                capture_output=True, text=True, timeout=15,
            )
            assert stop.returncode == 0, stop.stderr
            drainer.wait(timeout=30)

        n_synth = sum(1 for r in voicevox_mock["state"].requests
                      if "/synthesis" in r["path"])
        assert n_synth < total, f"halt したのに全 chunk 合成された: {n_synth}/{total}"
        # queue_mode フラグは維持
        assert (Path(env["VVREAD_STATE_DIR"]) / "queue_mode").is_file()
        # pending は空
        assert _count(env, "pending") == 0


class TestSkipIntegration:
    def test_skip_multichunk_advances_to_next_entry(self, voicevox_mock, tmp_path):
        """複数 chunk の entry を skip → 残り chunk を捨て次 entry が再生される。"""
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", sleep_seconds=0.5, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        from test_cmd_say import count_expected_chunks
        long_text = "テストです。" * 60  # 3+ chunk
        total = count_expected_chunks(long_text, speaker="3")
        assert total >= 3, total

        # 1 件目（複数 chunk）を drainer として起動
        with managed_process(
            [str(CMD_SAY), long_text],
            env=_clean_env_for(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as drainer:
            _wait_until(lambda: any("/audio_query" in r["path"]
                                    for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 2 件目（短文）を積む
            r2 = run_say("つぎのこえ", env_extra=env)
            assert r2.returncode == 0, r2.stderr
            # 1 件目再生中に skip
            sk = subprocess.run([str(VVREAD), "queue", "skip"],
                                env=_clean_env_for(env), capture_output=True, text=True, timeout=15)
            assert sk.returncode == 0, sk.stderr
            drainer.wait(timeout=30)

        texts = _audio_query_texts(voicevox_mock["state"])
        # 2 件目（次 entry）が再生される
        assert "つぎのこえ" in texts, texts
        # 1 件目は全 chunk 再生されない（skip で残り chunk を捨てた）
        n_synth_first = sum(1 for t in texts if t != "つぎのこえ")
        assert n_synth_first < total, f"skip したのに全 chunk 合成: {n_synth_first}/{total}"

    def test_skip_single_chunk_does_not_skip_next(self, voicevox_mock, tmp_path):
        """落とし穴回帰: 単一 chunk entry を skip しても次 entry は skip されない。

        consume-and-clear（毎 entry 完了後に token 一致 skip signal を消費）で、
        単一/最終 chunk の skip.request が次 entry へ巻き込まれないことを確認。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", sleep_seconds=0.5, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        # 1 件目: 単一 chunk
        with managed_process(
            [str(CMD_SAY), "いちこめ"],
            env=_clean_env_for(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as drainer:
            _wait_until(lambda: any("/audio_query" in r["path"]
                                    for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 2 件目を積んでから skip
            r2 = run_say("にこめ", env_extra=env)
            assert r2.returncode == 0, r2.stderr
            sk = subprocess.run([str(VVREAD), "queue", "skip"],
                                env=_clean_env_for(env), capture_output=True, text=True, timeout=15)
            assert sk.returncode == 0, sk.stderr
            drainer.wait(timeout=30)

        texts = _audio_query_texts(voicevox_mock["state"])
        # 2 件目は skip 信号に巻き込まれず必ず再生される
        assert "にこめ" in texts, texts


class TestOwnershipDoublePlay:
    """F-114 Phase 2: queue.lock ownership と double-play 境界。"""

    def test_long_playback_keeps_ownership(self, voicevox_mock, tmp_path):
        """長尺再生（> HB_INTERVAL）で ownership を保持し続け、helper が player を
        kill しない（音声が途切れない不変条件・double-play テストの鏡像）。"""
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        # player sleep 1.5s > HB_INTERVAL 1s → 再生中に hb 更新が 1 回以上走る
        make_fake_player(bin_dir, "afplay", sleep_seconds=1.5, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        env["VVREAD_QUEUE_HB_INTERVAL_S"] = "1"
        _enable_queue(env)

        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr
        # entry は完走する（helper が誤って player を kill していない）
        assert _count(env, "playing") == 0
        assert "あいうえお" in _audio_query_texts(voicevox_mock["state"])

    def test_lost_lock_leaves_playing_entry(self, voicevox_mock, tmp_path):
        """再生中に queue.lock を回収されたら（owner token 差し替え）、helper が
        player を停止し、playing entry を残して退場する（rc=4 経路・二重再生回避）。
        entry を failed へ移したり削除したりしない。"""
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", sleep_seconds=3, exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        env["VVREAD_QUEUE_HB_INTERVAL_S"] = "1"
        _enable_queue(env)

        qlock = _queue_dir(env) / "queue.lock"
        with managed_process(
            [str(CMD_SAY), "あいうえお"],
            env=_clean_env_for(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as drainer:
            # 再生開始（owner 確定）まで待つ
            _wait_until(lambda: (qlock / "owner").is_file()
                        and any("/audio_query" in r["path"]
                                for r in voicevox_mock["state"].requests),
                        timeout=10)
            # 別プロセスが回収した想定で owner の token を差し替える
            (qlock / "owner").write_text(f"{os.getpid()}\tFOREIGN_TOKEN\n")
            # drainer は ownership 喪失を検知して退場する（hang しない）
            drainer.wait(timeout=30)

        # playing entry は残る（failed へ移さない・削除しない）
        assert _count(env, "playing") >= 1
        assert _count(env, "failed") == 0


class TestSpinAbort:
    """F-114 Phase 3: empty-pop spin の bounded abort（release-before-abort）。"""

    def test_empty_pop_abort_releases_lock_and_next_say_drains(self, voicevox_mock, tmp_path):
        """pending>0 なのに pop が常に空（mv 失敗）になる wedge を再現し、
        EMPTY_POP_MAX 回で queue.lock を release して abort（hang しない）。
        abort 後に次の say が drainer を取得して drain できる。"""
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        env["VVREAD_QUEUE_EMPTY_POP_MAX"] = "3"
        _enable_queue(env)

        qd = _queue_dir(env)
        for sub in ("pending", "playing", "failed"):
            (qd / sub).mkdir(parents=True, exist_ok=True)
        # pending に entry を 1 件直接注入（pop が playing へ mv しようとする）
        (qd / "pending" / "1000000000000_99999.123.3.cli.r0").write_text("ちゅうにゅう")
        # playing を書込不可にして pop の mv を必ず失敗させる（pending は残る＝空 pop）
        (qd / "playing").chmod(0o500)
        try:
            r = run_say("どらいなー", env_extra=env)
            assert r.returncode == 0, r.stderr  # hang せず終了
            # abort 時に queue.lock を release 済み（残っていない）
            assert not (qd / "queue.lock").exists()
        finally:
            (qd / "playing").chmod(0o700)

        # 次の say が drainer を取得して pending を drain できる
        r2 = run_say("つぎのこえ", env_extra=env)
        assert r2.returncode == 0, r2.stderr
        assert _count(env, "pending") == 0
        assert "つぎのこえ" in _audio_query_texts(voicevox_mock["state"])


# ---------------------------------------------------------------------------
# enqueue ログ text_from= フォーマット
# ---------------------------------------------------------------------------


class TestEnqueueLogFormat:
    """say enqueue ログの text_from= 値に改行・CRLF が埋め込まれないことを確認。"""

    @staticmethod
    def _read_log(tmp_path: Path) -> str:
        log_file = tmp_path / "log" / "speak.log"
        return log_file.read_text(encoding="utf-8") if log_file.exists() else ""

    def _assert_enqueue_line_is_single(self, log: str, label: str) -> None:
        """say enqueue 行と text_from= が同じ行に収まることを確認する。"""
        # ログを改行で分割し、両方のトークンを含む行を探す
        lines = log.split("\n")
        combined = [l for l in lines if "say enqueue" in l and "text_from=" in l]
        assert combined, (
            f"[{label}] 'say enqueue' と 'text_from=' が同一行に見つからない。\n"
            f"ログ:\n{log!r}"
        )

    def test_text_from_strips_leading_newline(self, voicevox_mock, tmp_path):
        """TEXT が改行で始まっても text_from= の後に改行が埋め込まれない。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("\nHello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        log = self._read_log(tmp_path)
        self._assert_enqueue_line_is_single(log, "LF")

    def test_text_from_strips_crlf(self, voicevox_mock, tmp_path):
        """TEXT が CRLF を含んでも text_from= の後に改行が埋め込まれない。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("\r\nHello", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        log = self._read_log(tmp_path)
        self._assert_enqueue_line_is_single(log, "CRLF")

    def test_text_from_strips_esc_sequence(self, voicevox_mock, tmp_path):
        """L-3bash: TEXT に ESC(\\x1b)を含む ANSI エスケープが混入していても
        text_from= に生の ESC が書かれない(Web 由来 untrusted テキストによる
        ログ表示偽装対策)。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("\x1b[31mHello\x1b[0m", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        log = self._read_log(tmp_path)
        assert "\x1b" not in log, "ESC がログに残っている"
        self._assert_enqueue_line_is_single(log, "ESC")

    def test_text_from_strips_c1_control_char(self, voicevox_mock, tmp_path):
        """Codex レビュー指摘: TEXT に U+009B(CSI, UTF-8 では \\xc2\\x9b)のような
        C1 制御文字が混入していても text_from= に生の C1 バイトが書かれない。

        従来の `LC_ALL=C tr -d '[:cntrl:]'` は ASCII 制御バイト(C0 + 0x7F)しか
        除去できず、UTF-8 でエンコードされた C1(U+0080-U+009F)を通していた。
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("\x9b[31mHello\x9b[0m", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        log_file = tmp_path / "log" / "speak.log"
        log_bytes = log_file.read_bytes() if log_file.exists() else b""
        assert b"\xc2\x9b" not in log_bytes, "C1 制御文字(U+009B)がログに残っている"
        log = self._read_log(tmp_path)
        self._assert_enqueue_line_is_single(log, "C1")

    def test_text_from_preserves_japanese(self, voicevox_mock, tmp_path):
        """制御文字除去は日本語などマルチバイト文字を壊さない(L-3bash)。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("こんにちは世界", env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr}"

        log = self._read_log(tmp_path)
        assert "text_from=こんにちは世界" in log


# ---------------------------------------------------------------------------
# drain play ログ（engine 情報）
# ---------------------------------------------------------------------------


class TestDrainLog:
    def test_drain_play_log_includes_engine(self, voicevox_mock, tmp_path):
        """queue drain play ログに engine=URL が含まれる"""
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)

        r = run_say("ドレインログテスト", env_extra=env)
        assert r.returncode == 0, r.stderr

        log_file = tmp_path / "log" / "speak.log"
        wait_for_file(log_file)
        log = log_file.read_text()

        drain_lines = [l for l in log.splitlines() if "say drain play" in l]
        assert drain_lines, f"say drain play が見つからない\n{log}"
        assert any("engine=" in l for l in drain_lines), (
            f"drain play log に engine= が含まれない: {drain_lines}"
        )


# ---------------------------------------------------------------------------
# speed metadata (B-129)
# ---------------------------------------------------------------------------

class TestDrainSpeed:
    """queue drain の speed metadata 解析・cross-entry leak 防止テスト。"""

    def _synth_speed_scales(self, state) -> list:
        """記録された synthesis リクエスト body から speedScale を順番に抽出。"""
        import json
        out = []
        for req in state.requests:
            if "/synthesis" not in req["path"]:
                continue
            raw = req.get("body", b"")
            if isinstance(raw, (bytes, bytearray)) and raw:
                body = json.loads(raw)
                out.append(body.get("speedScale"))
        return out

    def _place_pending(self, env: dict, text: str, seq: int) -> None:
        """pending/ に手動でエントリを配置。

        seq は 1, 2, ... で連番を使う。ゼロパディングして現在の
        ミリ秒タイムスタンプ（~1751000000000, 13 桁）より確実に
        レキシコグラフィック的に小さくするため "000000000000{seq}" を使う。
        _queue_sorted は LC_ALL=C sort（辞書順）なので "0..." < "1..." が成立する。
        """
        qdir = _queue_dir(env)
        pending = qdir / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        ms_str = f"000000000000{seq}"
        entry_name = f"{ms_str}_99999.{seq:05d}.3.cli.r0"
        (pending / entry_name).write_text(text)

    def test_speed_via_queue_flag(self, voicevox_mock, tmp_path):
        """--queue --speed 1.8 で synthesis に speedScale 1.8 が届く。"""
        import json
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)

        r = run_say("あ", "--queue", "--speed", "1.8", env_extra=env)
        assert r.returncode == 0, r.stderr
        scales = self._synth_speed_scales(voicevox_mock["state"])
        assert scales, "synthesis リクエストがない"
        for s in scales:
            assert abs(s - 1.8) < 1e-6, f"speedScale expected 1.8 got {s}"

    def test_drain_invalid_speed_uses_baseline(self, voicevox_mock, tmp_path):
        """#vvread speed=invalid ヘッダーは line 1 を strip してベースライン speed で合成。

        新設計では writer が line 1 を常に制御するため invalid speed でも line 1 を
        strip し body(line 2 以降)を正常に合成する。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        env["VOICEVOX_SPEED"] = "1.5"

        # 無効 speed を持つエントリを早い ms で pending に配置（line 1 = header, line 2 = body）
        header_body = "#vvread speed=hello\n本文です"
        self._place_pending(env, header_body, seq=1)

        # say でドレイナー起動
        r = run_say("あいうえお", env_extra=env)
        assert r.returncode == 0, r.stderr

        # body ("本文です") が synthesis に届いており、speed は baseline (1.5)
        query_texts = _audio_query_texts(voicevox_mock["state"])
        assert any("本文です" in t for t in query_texts), \
            f"body text not synthesized: {query_texts}"
        scales = self._synth_speed_scales(voicevox_mock["state"])
        assert scales, "synthesis リクエストがない"
        assert abs(scales[0] - 1.5) < 1e-6, \
            f"invalid speed should fall back to baseline 1.5, got {scales[0]}"

    def test_body_starting_with_speed_marker_protected(self, voicevox_mock, tmp_path):
        """--speed なしで送った '#vvread speed=N' で始まるテキストが削除されない。

        以前の設計（no-speed = headerless）では '#vvread speed=1.0' で始まる
        テキストが metadata として誤解析されて1行目が消える恐れがあった（Codex P2）。
        新設計では no-speed エントリに '#vvread' ヘッダーを書くため、body の
        '#vvread speed=...' 行は line 2 に来て安全に合成される。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        env["VOICEVOX_SPEED"] = "1.5"

        # --speed なしで body が '#vvread speed=1.0' から始まるテキストを送信
        # _queue_enqueue は '#vvread\n#vvread speed=1.0\n...' と書く
        r = run_say("#vvread speed=1.0 が本文の先頭", "--queue", env_extra=env)
        assert r.returncode == 0, r.stderr

        query_texts = _audio_query_texts(voicevox_mock["state"])
        # sanitize.py が '#' を除去するため '#' なしで body text が届いていること確認
        # (body が空でないことが重要; 行削除されていないことを検証)
        assert any("vvread speed=1.0" in t for t in query_texts), \
            f"body text lost — query_texts: {query_texts}"
        # speed は --speed なしなので baseline (1.5)
        scales = self._synth_speed_scales(voicevox_mock["state"])
        assert scales, "synthesis リクエストがない"
        assert abs(scales[0] - 1.5) < 1e-6, \
            f"speed should stay at baseline 1.5 (no --speed flag), got {scales[0]}"

    def test_speed_invocation_as_drainer_preserves_nospeed_entry_baseline(
        self, voicevox_mock, tmp_path
    ):
        """--speed 1.8 の say が drainer になっても先行する no-speed エントリはベースライン (1.5) で合成。

        Codex P2 #2 回帰テスト。
        修正前: say.sh が QUEUE_MODE 解決より前に export VOICEVOX_SPEED=1.8 するため
        _vvread_drain_one_entry の baseline が 1.8 に汚染される。
        修正後: queue モードでは export を実行しないため baseline は 1.5 のまま。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        env["VOICEVOX_SPEED"] = "1.5"

        # no-speed エントリを事前配置（seq=1 → 最初に処理される）
        self._place_pending(env, "#vvread\nあいうえお", seq=1)

        # --speed 1.8 で drainer として起動
        r = run_say("かきくけこ", "--speed", "1.8", env_extra=env)
        assert r.returncode == 0, r.stderr

        scales = self._synth_speed_scales(voicevox_mock["state"])
        assert len(scales) >= 2, f"synthesis が 2 件以上必要: {scales}"
        # seq=1 (no-speed): baseline 1.5 (drainer の --speed で汚染されていない)
        assert abs(scales[0] - 1.5) < 1e-6, (
            f"no-speed entry should use baseline 1.5, got {scales[0]} "
            f"(bug: drainer --speed polluted baseline)"
        )
        # drainer 自身のエントリ (speed=1.8 via metadata): 1.8
        assert abs(scales[1] - 1.8) < 1e-6, \
            f"drainer entry should use speed 1.8, got {scales[1]}"

    def test_drain_speed_not_leaked_to_next_entry(self, voicevox_mock, tmp_path):
        """entry A (speed=2.0) の後 entry B (new '#vvread' header) は baseline speed (1.5) で合成。

        両エントリを same drainer で処理するため、早い ms の pending エントリを
        手動配置してから say で drainer を起動する。
        """
        bin_dir = tmp_path / "bin"; bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)
        env = _say_env(tmp_path, voicevox_mock["url"], bin_dir)
        _enable_queue(env)
        # ユーザー設定による干渉を排除してベースラインを 1.5 に固定する
        env["VOICEVOX_SPEED"] = "1.5"

        # entry A: speed=2.0 header (seq=1 → 最初に処理)
        self._place_pending(env, "#vvread speed=2.0\nあいうえお", seq=1)
        # entry B: no-speed new format (seq=2 → 次に処理); '#vvread' bare header
        self._place_pending(env, "#vvread\nかきくけこ", seq=2)

        # say で drainer を起動（entry C として ms=current で追加）
        # drainer は A→B→C の順に drain する（ゼロパディング ms が先に来る）
        r = run_say("さしすせそ", env_extra=env)
        assert r.returncode == 0, r.stderr

        scales = self._synth_speed_scales(voicevox_mock["state"])
        assert len(scales) >= 2, f"synthesis が 2 件以上必要: {scales}"
        # entry A: speedScale == 2.0
        assert abs(scales[0] - 2.0) < 1e-6, f"entry A speedScale expected 2.0 got {scales[0]}"
        # entry B: speedScale == 1.5 (baseline; cross-entry leak なら 2.0 になる)
        assert abs(scales[1] - 1.5) < 1e-6, \
            f"entry B speedScale expected 1.5 (no leak from A), got {scales[1]}"


def _clean_env_for(env: dict) -> dict:
    """run_say と同じ _clean_env 相当を Popen 用に組み立てる。"""
    import os
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    base.update(env)
    return base


def _wait_until(pred, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")
