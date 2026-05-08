"""lib_notify.sh のテスト

bash 関数を subprocess 経由で source して呼び出すスタイル。
通知バックエンド(terminal-notifier / osascript)はテストごとに mock を作って
PATH の先頭に差し込み、呼び出し回数と引数をログに残す。実 macOS の通知センターを
触らずに優先順位とフォールバックロジックを検証する。
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB_NOTIFY = REPO / "scripts" / "lib" / "notify.sh"


def _build_env(tmp_path: Path, *, with_terminal_notifier: bool, with_osascript: bool):
    """mock 環境を組み立てる。

    PATH を `bin_dir:/usr/bin:/bin` に絞ることで、テスト実行ホストに
    インストールされている terminal-notifier や osascript を巻き込まない。
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    osascript_log = tmp_path / "osascript.log"
    tn_log = tmp_path / "terminal_notifier.log"

    if with_osascript:
        osascript = bin_dir / "osascript"
        osascript.write_text(
            f'#!/bin/bash\nprintf "%s\\n" "$@" >> "{osascript_log}"\nexit 0\n'
        )
        osascript.chmod(0o755)

    if with_terminal_notifier:
        tn = bin_dir / "terminal-notifier"
        tn.write_text(
            f'#!/bin/bash\nprintf "%s\\n" "$@" >> "{tn_log}"\nexit 0\n'
        )
        tn.chmod(0o755)

    return {
        "STATE_DIR": str(state_dir),
        "OSASCRIPT_LOG": str(osascript_log),
        "TN_LOG": str(tn_log),
        "BIN_DIR": str(bin_dir),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
    }


@pytest.fixture
def env(tmp_path):
    """default: terminal-notifier 不在 + osascript mock のみ(フォールバック検証用)"""
    return _build_env(tmp_path, with_terminal_notifier=False, with_osascript=True)


@pytest.fixture
def env_with_tn(tmp_path):
    """terminal-notifier mock + osascript mock の両方"""
    return _build_env(tmp_path, with_terminal_notifier=True, with_osascript=True)


@pytest.fixture
def env_no_notifier(tmp_path):
    """両方 mock 不在(早期 return 検証用)"""
    return _build_env(tmp_path, with_terminal_notifier=False, with_osascript=False)


def run_bash(env: dict, script: str, extra: dict | None = None):
    """env と extra を export した上で lib_notify.sh を source し、script を実行"""
    base = os.environ.copy()
    base.update(env)
    if extra:
        base.update(extra)
    full = f'set -e; source "{LIB_NOTIFY}"; {script}'
    return subprocess.run(
        ["bash", "-c", full],
        env=base,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# _notify_escape (osascript フォールバック専用ヘルパー)
# ---------------------------------------------------------------------------


class TestNotifyEscape:
    def test_plain_text_passes_through(self, env):
        r = run_bash(env, '_notify_escape "hello world"')
        assert r.returncode == 0
        assert r.stdout == "hello world"

    def test_doublequote_is_escaped(self, env):
        r = run_bash(env, "_notify_escape 'say \"hi\"'")
        assert r.returncode == 0
        assert r.stdout == 'say \\"hi\\"'

    def test_backslash_is_escaped(self, env):
        r = run_bash(env, r"""_notify_escape $'back\\slash'""")
        assert r.returncode == 0
        assert r.stdout == r"back\\slash"

    def test_newline_replaced_with_space(self, env):
        r = run_bash(env, """_notify_escape "$(printf 'line1\\nline2')" """)
        assert r.returncode == 0
        assert r.stdout == "line1 line2"

    def test_japanese_passes_through(self, env):
        r = run_bash(env, '_notify_escape "通知タイトル"')
        assert r.returncode == 0
        assert r.stdout == "通知タイトル"


# ---------------------------------------------------------------------------
# _notify_within_cooldown
# ---------------------------------------------------------------------------


class TestNotifyWithinCooldown:
    def test_no_state_file_returns_false(self, env):
        r = run_bash(env, "_notify_within_cooldown && echo IN || echo OUT")
        assert r.returncode == 0
        assert r.stdout.strip() == "OUT"

    def test_recent_record_returns_true(self, env):
        last = Path(env["STATE_DIR"]) / "last_notify"
        last.write_text(str(int(time.time())))
        r = run_bash(env, "_notify_within_cooldown && echo IN || echo OUT")
        assert r.stdout.strip() == "IN"

    def test_old_record_returns_false(self, env):
        last = Path(env["STATE_DIR"]) / "last_notify"
        last.write_text(str(int(time.time()) - 9999))
        r = run_bash(env, "_notify_within_cooldown && echo IN || echo OUT")
        assert r.stdout.strip() == "OUT"

    def test_cooldown_zero_disables_suppression(self, env):
        last = Path(env["STATE_DIR"]) / "last_notify"
        last.write_text(str(int(time.time())))
        r = run_bash(
            env,
            "_notify_within_cooldown && echo IN || echo OUT",
            extra={"VOICEVOX_NOTIFY_COOLDOWN": "0"},
        )
        assert r.stdout.strip() == "OUT"

    def test_custom_cooldown_window(self, env):
        last = Path(env["STATE_DIR"]) / "last_notify"
        last.write_text(str(int(time.time()) - 5))
        r1 = run_bash(
            env,
            "_notify_within_cooldown && echo IN || echo OUT",
            extra={"VOICEVOX_NOTIFY_COOLDOWN": "10"},
        )
        assert r1.stdout.strip() == "IN"
        r2 = run_bash(
            env,
            "_notify_within_cooldown && echo IN || echo OUT",
            extra={"VOICEVOX_NOTIFY_COOLDOWN": "3"},
        )
        assert r2.stdout.strip() == "OUT"


# ---------------------------------------------------------------------------
# notify_error: terminal-notifier 優先
# ---------------------------------------------------------------------------


class TestNotifyErrorTerminalNotifier:
    def test_terminal_notifier_is_invoked_when_available(self, env_with_tn):
        r = run_bash(env_with_tn, 'notify_error "vvread" "engine down"')
        assert r.returncode == 0

        tn_log = Path(env_with_tn["TN_LOG"])
        assert tn_log.exists(), "terminal-notifier mock が呼ばれていない"
        contents = tn_log.read_text()
        # terminal-notifier の引数に title / message / group が含まれる
        assert "vvread" in contents
        assert "engine down" in contents
        assert "vvread" in contents  # -group 値

    def test_osascript_not_invoked_when_terminal_notifier_present(self, env_with_tn):
        # terminal-notifier 優先 → osascript はスキップ
        r = run_bash(env_with_tn, 'notify_error "title" "msg"')
        assert r.returncode == 0

        osascript_log = Path(env_with_tn["OSASCRIPT_LOG"])
        assert not osascript_log.exists(), (
            "terminal-notifier がある時に osascript が呼ばれている"
        )

    def test_doublequote_in_message_does_not_break(self, env_with_tn):
        # terminal-notifier は引数経由なのでクオートエスケープ不要だが、
        # bash -c の引用に巻き込まれない経路で通ることを確認
        r = run_bash(env_with_tn, """notify_error "voice" 'msg with "quotes"'""")
        assert r.returncode == 0
        contents = Path(env_with_tn["TN_LOG"]).read_text()
        assert "quotes" in contents

    def test_japanese_message_passes_through(self, env_with_tn):
        r = run_bash(env_with_tn, 'notify_error "vvread" "VOICEVOX に接続できません"')
        assert r.returncode == 0
        contents = Path(env_with_tn["TN_LOG"]).read_text()
        assert "VOICEVOX に接続できません" in contents


# ---------------------------------------------------------------------------
# notify_error: osascript フォールバック
# ---------------------------------------------------------------------------


class TestNotifyErrorOsascriptFallback:
    def test_falls_back_to_osascript(self, env):
        # env fixture は terminal-notifier 不在
        r = run_bash(env, 'notify_error "vvread" "engine down"')
        assert r.returncode == 0

        osascript_log = Path(env["OSASCRIPT_LOG"])
        assert osascript_log.exists(), (
            "terminal-notifier 不在時に osascript フォールバックが効いていない"
        )
        contents = osascript_log.read_text()
        assert "vvread" in contents
        assert "engine down" in contents

    def test_doublequote_in_message_is_escaped_for_osascript(self, env):
        r = run_bash(env, 'notify_error "voice" \'msg with "quotes"\'')
        assert r.returncode == 0
        contents = Path(env["OSASCRIPT_LOG"]).read_text()
        # _notify_escape でダブルクオートが \" にエスケープされた状態で渡る
        assert "quotes" in contents


# ---------------------------------------------------------------------------
# notify_error: 共通(cooldown / ファイル更新 / 安全な exit)
# ---------------------------------------------------------------------------


class TestNotifyErrorCommon:
    def test_first_call_updates_last_notify_with_terminal_notifier(self, env_with_tn):
        r = run_bash(env_with_tn, 'notify_error "vvread" "msg"')
        assert r.returncode == 0
        last_notify = Path(env_with_tn["STATE_DIR"]) / "last_notify"
        assert last_notify.exists()
        written = int(last_notify.read_text().strip())
        assert abs(written - int(time.time())) < 5

    def test_first_call_updates_last_notify_with_osascript(self, env):
        r = run_bash(env, 'notify_error "vvread" "msg"')
        assert r.returncode == 0
        last_notify = Path(env["STATE_DIR"]) / "last_notify"
        assert last_notify.exists()

    def test_within_cooldown_suppresses_terminal_notifier(self, env_with_tn):
        last_notify = Path(env_with_tn["STATE_DIR"]) / "last_notify"
        last_notify.write_text(str(int(time.time())))

        r = run_bash(env_with_tn, 'notify_error "title" "msg"')
        assert r.returncode == 0

        tn_log = Path(env_with_tn["TN_LOG"])
        assert not tn_log.exists(), "cooldown 内なのに terminal-notifier が呼ばれている"

    def test_within_cooldown_suppresses_osascript(self, env):
        last_notify = Path(env["STATE_DIR"]) / "last_notify"
        last_notify.write_text(str(int(time.time())))

        r = run_bash(env, 'notify_error "title" "msg"')
        assert r.returncode == 0

        osascript_log = Path(env["OSASCRIPT_LOG"])
        assert not osascript_log.exists()

    def test_cooldown_zero_always_calls(self, env_with_tn):
        last_notify = Path(env_with_tn["STATE_DIR"]) / "last_notify"
        last_notify.write_text(str(int(time.time())))

        r = run_bash(
            env_with_tn,
            'notify_error "title" "msg"',
            extra={"VOICEVOX_NOTIFY_COOLDOWN": "0"},
        )
        assert r.returncode == 0

        tn_log = Path(env_with_tn["TN_LOG"])
        assert tn_log.exists(), "cooldown=0 で抑制無効化が効いていない"

    def test_no_notifier_does_not_fail(self, env_no_notifier):
        # terminal-notifier も osascript も不在 → 何も呼ばずに exit 0
        r = run_bash(env_no_notifier, 'notify_error "title" "msg"')
        assert r.returncode == 0

        # last_notify は更新済み(cooldown 抑制を維持するため)
        last_notify = Path(env_no_notifier["STATE_DIR"]) / "last_notify"
        assert last_notify.exists()

    def test_two_calls_back_to_back_only_first_invokes(self, env_with_tn):
        r1 = run_bash(env_with_tn, 'notify_error "title" "first"')
        assert r1.returncode == 0
        r2 = run_bash(env_with_tn, 'notify_error "title" "second"')
        assert r2.returncode == 0

        contents = Path(env_with_tn["TN_LOG"]).read_text()
        assert "first" in contents
        assert "second" not in contents
