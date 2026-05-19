"""scripts/cmd_on_stop.sh のテスト (R-006)

vvread on-stop は Stop hook 用エントリ。stdin で hook JSON を受け取り、
parse_transcript.py で最後の assistant text を抽出 → cmd_say.sh へ subprocess
dispatch する。状態判定 (disabled / mute_until) と engine health check は本
コマンド内で行う(cmd_say には押し込まない)。

カバー範囲:
- 状態系: disabled / mute_until 未来 / mute_until 過去 (= 自動解除)
- engine health check: bogus URL → notify_error + exit 0(silent)
- 健常系: hook JSON + transcript → cmd_say が呼ばれて synth/play が起きる
- 入力エラー: 空 stdin / 不正 JSON / transcript 不在 → silent exit 0
- 引数: 未知引数で usage / -h で usage
- bin/vvread on-stop dispatch
"""
import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

from conftest import wait_for_file

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_ON_STOP = REPO / "scripts" / "cmd" / "on_stop.sh"


def _path_env(tmp_path: Path) -> dict:
    """on-stop 系テスト用の最小 env。VVREAD_PROJECT_DIR を tmp_path に向け
    本物の repo 直下 tmp/ への legacy migration を確実に no-op 化する。
    VVREAD_SCRIPTS_DIR は本物の scripts/ が必要なので明示。"""
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        "VVREAD_PROJECT_DIR": str(tmp_path),
        "VVREAD_SCRIPTS_DIR": str(REPO / "scripts"),
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
):
    path = bin_dir / name
    lines = ["#!/bin/bash"]
    if args_log:
        lines.append(f'printf "%s\\n" "$@" >> "{args_log}"')
    if touch_on_run:
        lines.append(f'touch "{touch_on_run}"')
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def write_transcript(path: Path, text: str) -> Path:
    """単一 assistant entry の transcript を書き出す"""
    entry = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def run_on_stop(payload: bytes | str, *args, env_extra=None, timeout=15):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return subprocess.run(
        [str(CMD_ON_STOP), *args],
        input=payload,
        env=_clean_env(env_extra),
        capture_output=True,
        timeout=timeout,
    )


def run_vvread_on_stop(payload: bytes | str, *args, env_extra=None, timeout=15):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return subprocess.run(
        [str(VVREAD), "on-stop", *args],
        input=payload,
        env=_clean_env(env_extra),
        capture_output=True,
        timeout=timeout,
    )


def _on_stop_env(tmp_path: Path, voicevox_url: str, bin_dir: Path,
                 player: str = "afplay") -> dict:
    """on-stop 実行に必要な共通 env。

    VOICEVOX_ENGINE_URL = ベース URL（on_stop 内で /version を付加）。
    VOICEVOX_ENGINE = VOICEVOX_ENGINE_URL の旧エイリアス（後方互換）。

    VVREAD_PROJECT_DIR を tmp_path に向け、`vvread_migrate_legacy_tmp` が参照
    する `${VVREAD_PROJECT_DIR}/tmp` を不在化(no-op 化)する。代わりに
    VVREAD_SCRIPTS_DIR は本物の scripts/ を指す必要があるので明示。これで
    本物の repo 直下の `tmp/logs/speak.log` や `tmp/cache/*.wav` がテスト
    tmp_path に流入するのを防ぐ(test_voice.py と同じ設計)。
    """
    env = _path_env(tmp_path)
    # S-008: VOICEVOX_ENGINE_URL はベース URL。on_stop 内で /version を付加。
    env["VOICEVOX_ENGINE_URL"] = voicevox_url
    env["VOICEVOX_ENGINE"] = voicevox_url
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["VVREAD_PLAYER"] = player
    env["VVREAD_PROJECT_DIR"] = str(tmp_path)
    env["VVREAD_SCRIPTS_DIR"] = str(REPO / "scripts")
    return env


# ---------------------------------------------------------------------------
# 引数 validation
# ---------------------------------------------------------------------------


class TestArgsValidation:
    def test_help_flag_shows_usage(self, tmp_path):
        r = run_on_stop(b"", "-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert b"Usage: vvread on-stop" in r.stderr

    def test_unknown_arg_shows_usage(self, tmp_path):
        r = run_on_stop(b"", "--bogus", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert b"unknown argument" in r.stderr


# ---------------------------------------------------------------------------
# 状態判定: disabled / mute_until
# ---------------------------------------------------------------------------


class TestDisabledFlag:
    def test_disabled_flag_skips_everything(self, tmp_path):
        """disabled flag があれば health check も transcript parse も走らない"""
        env = _path_env(tmp_path)
        # bogus engine URL を設定しておいても呼ばれない(早期 return の証明)
        env["VOICEVOX_ENGINE_URL"] = "http://127.0.0.1:1/never"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "disabled").write_text("")

        r = run_on_stop(json.dumps({"transcript_path": "/no/such"}),
                        env_extra=env)
        assert r.returncode == 0
        # notify_error も呼ばれない(stderr 空)
        assert r.stderr == b""


class TestMuteUntil:
    def test_future_mute_until_skips(self, tmp_path):
        env = _path_env(tmp_path)
        env["VOICEVOX_ENGINE_URL"] = "http://127.0.0.1:1/never"
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # 60 秒後まで mute
        future = int(time.time()) + 60
        (state_dir / "mute_until").write_text(str(future))

        r = run_on_stop(json.dumps({"transcript_path": "/no/such"}),
                        env_extra=env)
        assert r.returncode == 0
        # mute_until は残る(まだ有効)
        assert (state_dir / "mute_until").exists()

    def test_expired_mute_until_is_removed_and_continues(self, voicevox_mock, tmp_path):
        """期限切れの mute_until は削除され、health check 以降に進む"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # 過去時刻
        past = int(time.time()) - 60
        (state_dir / "mute_until").write_text(str(past))

        transcript = tmp_path / "t.jsonl"
        write_transcript(transcript, "ミュート期限切れ後の応答")

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(json.dumps({"transcript_path": str(transcript)}),
                        env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr!r}"

        # 期限切れ flag は削除されている
        assert not (state_dir / "mute_until").exists()
        # cmd_say が呼ばれて player も呼ばれている
        assert marker.exists()


# ---------------------------------------------------------------------------
# engine health check
# ---------------------------------------------------------------------------


class TestEngineHealthCheck:
    def test_unreachable_engine_silent_exit_0(self, tmp_path):
        """engine 不通でも hook 自体は exit 0(notify_error は出してログだけ)"""
        env = _path_env(tmp_path)
        # 確実に届かないアドレス + 短い curl タイムアウト(1s, on_stop 内蔵)
        # S-008: ベース URL を渡す。on_stop 内で /version を付加。
        env["VOICEVOX_ENGINE_URL"] = "http://127.0.0.1:1"
        # 通知抑制 (terminal-notifier が無くても cooldown 制御で安全に no-op)

        r = run_on_stop(json.dumps({"transcript_path": "/no/such"}),
                        env_extra=env)
        assert r.returncode == 0
        # log は LOG_DIR に出るので、log file の中に "engine unreachable" が
        # 入っていることを確認(stderr ではなく)
        log_file = tmp_path / "log" / "speak.log"
        wait_for_file(log_file)
        content = log_file.read_text()
        assert "engine unreachable" in content


# ---------------------------------------------------------------------------
# 健常系: transcript → cmd_say
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_dispatches_to_cmd_say_on_health(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        transcript = tmp_path / "t.jsonl"
        write_transcript(transcript, "こんにちはテスト応答")

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(json.dumps({"transcript_path": str(transcript)}),
                        env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr!r}"

        # health check + cmd_say 経由で synth が呼ばれた
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth >= 1
        # player も呼ばれた
        assert marker.exists()

    def test_uses_last_assistant_text(self, voicevox_mock, tmp_path):
        """transcript に複数 assistant entry がある場合、最後の text が渡る"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        transcript = tmp_path / "t.jsonl"
        # 複数 entry を JSONL で書く
        with open(transcript, "w", encoding="utf-8") as f:
            for text in ("古い応答1", "古い応答2", "最終応答テキスト"):
                entry = {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(json.dumps({"transcript_path": str(transcript)}),
                        env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr!r}"

        # VOICEVOX audio_query は `text=...&speaker=...` を URL クエリで渡すため
        # request の path に text が URL-encoded で現れる(body は空)。
        # 最終 entry のみが synth に渡る(過去の entry は無視される)ことを
        # path の URL-encoded text で直接検証する。
        all_paths = "\n".join(
            req["path"] for req in voicevox_mock["state"].requests
        )
        last_enc = urllib.parse.quote("最終応答テキスト", safe="")
        old1_enc = urllib.parse.quote("古い応答1", safe="")
        old2_enc = urllib.parse.quote("古い応答2", safe="")
        assert last_enc in all_paths, (
            "最終 entry の text が VOICEVOX に渡っていない"
        )
        assert old1_enc not in all_paths, "古い entry が誤って渡っている"
        assert old2_enc not in all_paths, "古い entry が誤って渡っている"


# ---------------------------------------------------------------------------
# 入力エラー(silent exit 0)
# ---------------------------------------------------------------------------


class TestInputErrors:
    def test_empty_stdin_silent_exit_0(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(b"", env_extra=env)
        assert r.returncode == 0
        # synth は呼ばれない
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 0

    def test_invalid_json_silent_exit_0(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(b"not_json{garbage", env_extra=env)
        assert r.returncode == 0
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 0

    def test_transcript_path_missing_silent_exit_0(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_on_stop(json.dumps({"transcript_path": str(tmp_path / "no.jsonl")}),
                        env_extra=env)
        assert r.returncode == 0
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 0


# ---------------------------------------------------------------------------
# stdin I/O 制約(env override 経由)
# ---------------------------------------------------------------------------


class TestStdinLimits:
    def test_max_bytes_env_truncates_processing(self, voicevox_mock, tmp_path):
        """VVREAD_ON_STOP_MAX_BYTES を小さく設定して oversize 経路を踏ませる"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        make_fake_player(bin_dir, "afplay", exit_code=0)

        transcript = tmp_path / "t.jsonl"
        write_transcript(transcript, "発話されない応答")

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        # 100 byte 上限。transcript_path だけで超過する payload を渡す
        env["VVREAD_ON_STOP_MAX_BYTES"] = "100"

        big_payload = json.dumps({
            "transcript_path": str(transcript),
            "padding": "x" * 500,
        })
        r = run_on_stop(big_payload, env_extra=env)
        assert r.returncode == 0
        # oversize で abandon されるので synth は呼ばれない
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth == 0

        # log file に parse_transcript_warning が記録される
        log_file = tmp_path / "log" / "speak.log"
        wait_for_file(log_file)
        assert "parse_transcript_warning" in log_file.read_text()


# ---------------------------------------------------------------------------
# bin/vvread on-stop dispatch
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_vvread_on_stop_dispatches(self, voicevox_mock, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran.marker"
        make_fake_player(bin_dir, "afplay", touch_on_run=marker, exit_code=0)

        transcript = tmp_path / "t.jsonl"
        write_transcript(transcript, "ディスパッチ確認")

        env = _on_stop_env(tmp_path, voicevox_mock["url"], bin_dir)
        r = run_vvread_on_stop(json.dumps({"transcript_path": str(transcript)}),
                               env_extra=env)
        assert r.returncode == 0, f"stderr={r.stderr!r}"
        assert marker.exists()

    def test_vvread_on_stop_help(self, tmp_path):
        r = run_vvread_on_stop(b"", "-h", env_extra=_path_env(tmp_path))
        assert r.returncode == 1
        assert b"Usage: vvread on-stop" in r.stderr
