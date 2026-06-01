"""tests/test_smoke.py - End-to-end smoke test (R-017)

vvread の主要 4 コマンド (setup → say → doctor --offline → uninstall) を
共有 state/log/cache の下で連続実行し、ライフサイクル全体が破綻なく回るこ
とを検証する。各コマンド単体は test_cmd_*.py で網羅済みであり、本 test の
役割は「setup の出力 (settings.json + vvread.settings.json) を後段の
say/doctor が正しく拾い、uninstall で hook が消える」という統合動作の保
証にある。

環境隔離:
- VOICEVOX engine: voicevox_mock fixture (本物の HTTP server を localhost に立てる)
- audio player: fake afplay (test_cmd_*.py 流儀の bash stub)
- HOME / state / log / cache / project_dir: tmp_path 配下に分離
- VVREAD_PROJECT_DIR は fake_repo に向け、本物の repo .claude/ への書込を防止

skip 条件: Windows のみ (Git Bash / WSL は対象内、ただし best-effort)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="smoke test targets macOS and Linux first-class; Windows is best-effort",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clean_env(env_extra=None) -> dict:
    """親プロセスの VOICEVOX_* / VVREAD_* を継承させない。"""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def _make_fake_repo(tmp_path: Path) -> Path:
    """fake_repo に最小限の bin/vvread スタブを置く。

    setup の hook 登録時に repo_root の絶対パスを Stop hook command に埋め
    込むため、`<fake_repo>/bin/vvread` が存在する必要がある。実体は実行さ
    れない (smoke 内では本物の REPO/bin/vvread を呼ぶ)。
    """
    fake_repo = tmp_path / "fake_repo"
    (fake_repo / "bin").mkdir(parents=True)
    stub = fake_repo / "bin" / "vvread"
    stub.write_text("#!/bin/bash\nexit 0\n")
    stub.chmod(0o755)
    return fake_repo


def _make_fake_player(bin_dir: Path, name: str = "afplay",
                      touch_on_run: Path | None = None) -> Path:
    """test_cmd_on_stop.py / test_cmd_say.py と同じ流儀の fake player。"""
    path = bin_dir / name
    lines = ["#!/bin/bash"]
    if touch_on_run:
        lines.append(f'touch "{touch_on_run}"')
    lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


def _smoke_env(tmp_path: Path, voicevox_url: str, bin_dir: Path,
               fake_repo: Path) -> dict:
    """smoke test の共通 env。HOME / state / log / cache / project_dir を全
    て tmp_path 配下に向け、本物の repo の状態を一切汚染しない。"""
    return {
        "HOME": str(tmp_path / "home"),
        "VVREAD_PROJECT_DIR": str(fake_repo),
        "VVREAD_SCRIPTS_DIR": str(REPO / "scripts"),
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        # say / doctor が読む。setup は --engine-url で渡す側
        "VOICEVOX_ENGINE": voicevox_url,
        "VOICEVOX_ENGINE_URL": voicevox_url,
        "VOICEVOX_SPEAKER": "0",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "VVREAD_PLAYER": "afplay",
    }


def _run(args: list, env: dict, cwd: Path,
         timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VVREAD), *args],
        env=_clean_env(env),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


class TestSmokeFullFlow:
    """vvread setup → say → doctor --offline → uninstall のフルライフサイクル。

    1 つの test 関数で 4 step を直列に流す (各 step が前 step の副作用に依存
    するため、test を分けると fixture 共有の難度が上がる)。
    """

    def test_full_lifecycle(self, voicevox_mock, tmp_path):
        # ─── 環境準備 ────────────────────────────────────────────────────
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        played = tmp_path / "played.marker"
        _make_fake_player(bin_dir, "afplay", touch_on_run=played)

        fake_repo = _make_fake_repo(tmp_path)
        cwd = tmp_path / "proj"
        cwd.mkdir()
        (tmp_path / "home").mkdir()

        env = _smoke_env(tmp_path, voicevox_mock["url"], bin_dir, fake_repo)

        # ─── Step 1: setup ───────────────────────────────────────────────
        r_setup = _run(
            ["setup", "--yes",
             "--engine-url", voicevox_mock["url"],
             "--no-install-e2k"],
            env=env, cwd=cwd, timeout=30,
        )
        assert r_setup.returncode == 0, (
            f"setup failed: stdout={r_setup.stdout}\nstderr={r_setup.stderr}"
        )

        # hook が project scope (cwd/.claude/settings.local.json) に登録される
        settings_path = cwd / ".claude" / "settings.local.json"
        assert settings_path.exists(), "settings.local.json was not created"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        # voiceClaude の Stop hook が 1 件以上ある (詳細形式は test_hook_install.py で網羅)
        assert any(
            "vvread" in (h.get("command", "")
                         if isinstance(h, dict) else "")
            or any("vvread" in entry.get("command", "")
                   for entry in (h.get("hooks", []) if isinstance(h, dict) else []))
            for h in stop_hooks
        ), f"Stop hook not registered: {stop_hooks!r}"

        # vvread.settings.json に engines が書かれる（engineUrl ではなく engines に統一）
        vvread_settings_path = cwd / "vvread.settings.json"
        assert vvread_settings_path.exists()
        vvread_settings = json.loads(
            vvread_settings_path.read_text(encoding="utf-8")
        )
        normalized_url = voicevox_mock["url"].rstrip("/")
        assert vvread_settings["voicevox"]["engines"] == [normalized_url]
        assert "engineUrl" not in vvread_settings.get("voicevox", {})

        # ─── Step 2: say ─────────────────────────────────────────────────
        r_say = _run(["say", "テスト音声"], env=env, cwd=cwd, timeout=30)
        assert r_say.returncode == 0, (
            f"say failed: stdout={r_say.stdout}\nstderr={r_say.stderr}"
        )
        # 合成 (mock /synthesis) と再生 (fake afplay) が両方発火
        n_synth = sum(1 for req in voicevox_mock["state"].requests
                      if "/synthesis" in req["path"])
        assert n_synth >= 1, "synthesis endpoint was not called"
        assert played.exists(), "fake player was not invoked"

        # ─── Step 3: doctor --offline ────────────────────────────────────
        r_doctor = _run(["doctor", "--offline"], env=env, cwd=cwd, timeout=30)
        assert r_doctor.returncode == 0, (
            f"doctor failed: stdout={r_doctor.stdout}\nstderr={r_doctor.stderr}"
        )
        assert "summary:" in r_doctor.stdout
        # setup で登録された hook が doctor の hooks セクションで認識される
        assert "[hooks]" in r_doctor.stdout

        # ─── Step 4: uninstall ───────────────────────────────────────────
        r_uninstall = _run(["uninstall"], env=env, cwd=cwd, timeout=15)
        assert r_uninstall.returncode == 0, (
            f"uninstall failed: stdout={r_uninstall.stdout}\nstderr={r_uninstall.stderr}"
        )

        # uninstall 後: settings.local.json はファイル自体は残るが、voiceClaude
        # hook は除去されている (空の Stop / hooks 構造は畳まれる仕様)
        if settings_path.exists():
            settings_after = json.loads(
                settings_path.read_text(encoding="utf-8")
            )
            stop_after = settings_after.get("hooks", {}).get("Stop", [])
            for block in stop_after:
                hooks_arr = (block.get("hooks", [])
                             if isinstance(block, dict) else [])
                for h in hooks_arr:
                    cmd = h.get("command", "") if isinstance(h, dict) else ""
                    assert "vvread" not in cmd, (
                        f"voiceClaude hook still present after uninstall: {cmd}"
                    )


# ---------------------------------------------------------------------------
# Doctor-only smoke (no VOICEVOX, no player)
# ---------------------------------------------------------------------------


class TestSmokeDoctorOffline:
    """VOICEVOX も player も無い完全隔離環境で doctor --offline が exit 0
    で完了することを確認する。CI でも常に走る最低限の smoke。"""

    def test_doctor_offline_exits_0_in_isolated_env(self, tmp_path):
        env = {
            "HOME": str(tmp_path / "home"),
            "VVREAD_STATE_DIR": str(tmp_path / "state"),
            "VVREAD_LOG_DIR": str(tmp_path / "log"),
            "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        }
        r = subprocess.run(
            [str(VVREAD), "doctor", "--offline"],
            env=_clean_env(env),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            timeout=30,
        )
        assert r.returncode == 0, (
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
        assert "summary:" in r.stdout
