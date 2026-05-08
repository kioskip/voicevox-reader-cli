"""lib_log.sh のテスト

ローテーション挙動 (T-004) と log_info / log_debug の出力レベルを検証する。
bash 関数を subprocess 経由で source して呼び出すスタイル(test_lib_notify.py
と同じ流儀)。
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_LOG = REPO / "scripts" / "lib" / "log.sh"


def run_bash(env: dict, script: str):
    """env を export した上で lib_log.sh を source し、script を実行"""
    base = os.environ.copy()
    base.update(env)
    full = f'set -e; source "{LIB_LOG}"; {script}'
    return subprocess.run(
        ["bash", "-c", full],
        env=base,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def env(tmp_path):
    """共通 fixture: LOG_DIR を tmp_path に向け、ログレベル INFO で起動"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return {
        "LOG_DIR": str(log_dir),
        "VOICEVOX_LOG_LEVEL": "INFO",
        # 既存環境の VOICEVOX_LOG_FILE / VOICEVOX_LOG_MAX_BYTES を意図せず継承
        # しないよう、明示的に空にする(各テストが必要なら上書きする)
        "VOICEVOX_LOG_FILE": "",
        "VOICEVOX_LOG_MAX_BYTES": "",
    }


# ---------------------------------------------------------------------------
# 通常出力(回帰テスト)
# ---------------------------------------------------------------------------


class TestLogWrite:
    def test_log_info_writes_to_default_path(self, env, tmp_path):
        """LOG_FILE 既定値(${LOG_DIR}/speak.log)に書かれる"""
        env.pop("VOICEVOX_LOG_FILE")  # default を使う
        r = run_bash(env, 'log_info "hello"')
        assert r.returncode == 0, r.stderr
        log = tmp_path / "logs" / "speak.log"
        assert log.exists()
        assert "hello" in log.read_text()

    def test_log_debug_suppressed_at_info_level(self, env, tmp_path):
        """LOG_LEVEL=INFO の時 log_debug は何も書かない"""
        env.pop("VOICEVOX_LOG_FILE")
        r = run_bash(env, 'log_debug "should not appear"')
        assert r.returncode == 0, r.stderr
        log = tmp_path / "logs" / "speak.log"
        # debug 出力が抑制されている = ファイルが空 or 存在しない
        assert not log.exists() or log.read_text() == ""

    def test_log_off_skips_write(self, env, tmp_path):
        """LOG_LEVEL=OFF の時は書き込みが走らない"""
        env["VOICEVOX_LOG_LEVEL"] = "OFF"
        env.pop("VOICEVOX_LOG_FILE")
        r = run_bash(env, 'log_info "nope"')
        assert r.returncode == 0, r.stderr
        # ログファイルは作成されない(LOG_DIR は fixture が事前作成済)
        assert not (tmp_path / "logs" / "speak.log").exists()


# ---------------------------------------------------------------------------
# T-004: ローテーション
# ---------------------------------------------------------------------------


class TestRotation:
    def _seed_log(self, path: Path, n_bytes: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * n_bytes)

    def test_rotates_when_size_exceeds_threshold(self, env, tmp_path):
        """サイズ超過 → 既存 LOG_FILE が LOG_FILE.1 に退避される"""
        log = tmp_path / "speak.log"
        self._seed_log(log, 200)  # 200 バイト
        env["VOICEVOX_LOG_FILE"] = str(log)
        env["VOICEVOX_LOG_MAX_BYTES"] = "100"  # 100 バイトで rotate
        # source 時に rotate されるので、source 後のログ出力は新しいファイル側
        r = run_bash(env, 'log_info "after-rotate"')
        assert r.returncode == 0, r.stderr
        # .1 に既存内容(x*200)が退避
        assert log.with_suffix(".log.1").exists()
        assert log.with_suffix(".log.1").read_bytes() == b"x" * 200
        # 新しい LOG_FILE には after-rotate 行のみ
        new_content = log.read_text()
        assert "after-rotate" in new_content
        assert "x" * 200 not in new_content

    def test_does_not_rotate_under_threshold(self, env, tmp_path):
        """閾値未満なら rotate しない(既存ファイルに追記される)"""
        log = tmp_path / "speak.log"
        self._seed_log(log, 50)
        env["VOICEVOX_LOG_FILE"] = str(log)
        env["VOICEVOX_LOG_MAX_BYTES"] = "100"
        r = run_bash(env, 'log_info "appended"')
        assert r.returncode == 0, r.stderr
        # .1 はできていない
        assert not log.with_suffix(".log.1").exists()
        # 元の内容 + 追記内容がそのまま
        content = log.read_bytes()
        assert content.startswith(b"x" * 50)
        assert b"appended" in content

    def test_rotation_overwrites_previous_backup(self, env, tmp_path):
        """.1 が既にある状態で再度 rotate しても上書きされる(履歴 1 世代)"""
        log = tmp_path / "speak.log"
        backup = tmp_path / "speak.log.1"
        backup.write_bytes(b"old-backup-content")
        self._seed_log(log, 200)
        env["VOICEVOX_LOG_FILE"] = str(log)
        env["VOICEVOX_LOG_MAX_BYTES"] = "100"
        r = run_bash(env, 'log_info "after"')
        assert r.returncode == 0, r.stderr
        # .1 は古いバックアップではなく直前の LOG_FILE 内容で上書きされている
        assert backup.read_bytes() == b"x" * 200
        assert b"old-backup-content" not in backup.read_bytes()

    def test_default_threshold_does_not_rotate_small_log(self, env, tmp_path):
        """デフォルト 10 MiB なら通常運用サイズでは rotate されない"""
        log = tmp_path / "speak.log"
        self._seed_log(log, 1024)  # 1 KB
        env["VOICEVOX_LOG_FILE"] = str(log)
        env.pop("VOICEVOX_LOG_MAX_BYTES")  # default
        r = run_bash(env, 'log_info "small"')
        assert r.returncode == 0, r.stderr
        assert not log.with_suffix(".log.1").exists()

    def test_zero_max_bytes_disables_rotation(self, env, tmp_path):
        """LOG_MAX_BYTES=0 を渡すと rotate を完全に無効化(運用上の escape hatch)"""
        log = tmp_path / "speak.log"
        self._seed_log(log, 10000)  # かなり大きい
        env["VOICEVOX_LOG_FILE"] = str(log)
        env["VOICEVOX_LOG_MAX_BYTES"] = "0"
        r = run_bash(env, 'log_info "no-rotate"')
        assert r.returncode == 0, r.stderr
        assert not log.with_suffix(".log.1").exists()
        # 元のサイズが温存された上に追記されている
        assert len(log.read_bytes()) > 10000

    def test_no_existing_log_does_nothing(self, env, tmp_path):
        """LOG_FILE が存在しない初回起動は rotate しない(エラーにもならない)"""
        log = tmp_path / "speak.log"
        env["VOICEVOX_LOG_FILE"] = str(log)
        env["VOICEVOX_LOG_MAX_BYTES"] = "10"
        r = run_bash(env, 'log_info "first"')
        assert r.returncode == 0, r.stderr
        assert log.exists()  # 新規作成
        assert not log.with_suffix(".log.1").exists()
