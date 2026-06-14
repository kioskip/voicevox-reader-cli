"""tests/test_mcp_server.py - vvread MCP server のテスト (B-110)

テストを 2 グループに分ける:
  CLI dispatch / mcp.sh テスト: mcp package 不要（uv sync のみで実行可能）
  MCP SDK テスト: mcp package 必要（uv sync --extra mcp）
                  @pytest.mark.skipif(_MCP_AVAILABLE) でスキップ分岐
                  ※ モジュールレベルの importorskip は全クラスをスキップしてしまうため使わない

非回帰目的: mcp package 未導入状態で既存 CLI が壊れないことも確認。
"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
MCP_SH = REPO / "scripts" / "cmd" / "mcp.sh"
MCP_SERVER = REPO / "scripts" / "mcp_server.py"

# mcp の有無を収集時に判定（モジュール全体のスキップを防ぐため importorskip は使わない）
import importlib.util as _importlib_util
_MCP_AVAILABLE = _importlib_util.find_spec("mcp") is not None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(args, env=None, input=None, timeout=10, **kwargs):
    """subprocess.run ラッパー。stdout / stderr を常にキャプチャ。"""
    base_env = os.environ.copy()
    if env:
        base_env.update(env)
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=base_env,
        input=input,
        timeout=timeout,
        **kwargs,
    )


def _path_env(tmp_path: Path) -> dict:
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        "VVREAD_PROJECT_SETTINGS": str(tmp_path / "no-project-settings.json"),
    }


def _wait_until(pred, timeout=8, interval=0.05) -> bool:
    """pred() が真になるまで polling（固定 sleep を避けるため）。

    timeout 超過時は False を返す（呼び出し側が後続 assert で判定する）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# CLI dispatch / mcp.sh テスト（mcp package 不要）
# ---------------------------------------------------------------------------


def _make_absent_mcp_python(tmp_path: Path) -> Path:
    """mcp の import を失敗させる偽 Python スクリプトを作成する。

    `-c "import mcp"` に対して exit 1、それ以外は実システムの python3 に委譲。
    実 python3 のパスを事前解決してスクリプトに埋め込むことで
    PATH 再帰を回避する。
    """
    import shutil as _shutil
    real_python3 = _shutil.which("python3") or "/usr/bin/python3"
    absent_py = tmp_path / "absent_mcp_python.sh"
    absent_py.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ] && echo "$2" | grep -q "import mcp"; then\n'
        "  exit 1\n"
        "fi\n"
        f'exec "{real_python3}" "$@"\n'
    )
    absent_py.chmod(0o755)
    return absent_py


def _absent_mcp_env(tmp_path: Path) -> dict:
    """全 Python 候補で mcp が見つからない環境変数辞書を返す。

    - VVREAD_PROJECT_DIR=tmp_path (.venv なし)
    - VVREAD_MCP_PYTHON=absent_mcp_python (import mcp 失敗)
    - PATH に absent script を python3 として登録（uv venv の python3 を隠す）
    """
    absent_py = _make_absent_mcp_python(tmp_path)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # python3 として同じスクリプトをリンク
    py3_link = bin_dir / "python3"
    py3_link.symlink_to(absent_py)

    return {
        **_path_env(tmp_path),
        "VVREAD_PROJECT_DIR": str(tmp_path),
        "VVREAD_MCP_PYTHON": str(absent_py),
        "PATH": str(bin_dir) + ":" + os.environ.get("PATH", ""),
    }


class TestMcpShDispatch:
    """bin/vvread mcp → mcp.sh のディスパッチと Python 解決を確認する。"""

    def test_vvread_mcp_subcommand_dispatches(self, tmp_path):
        """bin/vvread mcp が mcp.sh を経由して MCP server を起動すること。

        mcp が利用可能な環境で vvread mcp を起動し、すぐに起動できることを確認する。
        server は stdio をリッスンし続けるため、起動確認後に terminate する。
        """
        proc = subprocess.Popen(
            [str(VVREAD), "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **_path_env(tmp_path)},
        )
        try:
            # 少し待ってプロセスがまだ動いている（クラッシュしていない）ことを確認
            time.sleep(0.5)
            assert proc.poll() is None, (
                f"MCP server がすぐに終了した。"
                f"stderr={proc.stderr.read1(1024).decode(errors='replace') if proc.stderr else ''}"
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def test_mcp_sh_absent_mcp_exits_1_with_hint(self, tmp_path):
        """mcp package 不在時に exit 1 + install 案内が stderr に出ること。"""
        result = _run(
            [str(MCP_SH)],
            env=_absent_mcp_env(tmp_path),
            timeout=5,
        )
        assert result.returncode == 1
        assert "mcp" in result.stderr.lower()
        assert "uv sync" in result.stderr

    def test_mcp_sh_prefers_vvread_mcp_python_over_venv(self, tmp_path):
        """VVREAD_MCP_PYTHON が指定された場合に優先して使われること。"""
        # mcp import を成功させ、mcp_server.py 実行時に CHOSEN を stderr に出す偽 Python
        chosen_py = tmp_path / "chosen_python.sh"
        chosen_py.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "-c" ] && echo "$2" | grep -q "import mcp"; then\n'
            "  exit 0\n"  # mcp import 成功を偽装
            "fi\n"
            "echo 'CHOSEN_PYTHON_USED' >&2\n"
            "exit 0\n"
        )
        chosen_py.chmod(0o755)

        result = _run(
            [str(MCP_SH)],
            env={
                **_path_env(tmp_path),
                "VVREAD_PROJECT_DIR": str(tmp_path),
                "VVREAD_MCP_PYTHON": str(chosen_py),
            },
            timeout=5,
        )
        assert "CHOSEN_PYTHON_USED" in result.stderr

    def test_mcp_server_file_exists(self):
        """scripts/mcp_server.py が存在すること。"""
        assert MCP_SERVER.exists(), f"mcp_server.py not found: {MCP_SERVER}"

    def test_mcp_sh_exists_and_executable(self):
        """scripts/cmd/mcp.sh が存在し実行可能なこと。"""
        assert MCP_SH.exists(), f"mcp.sh not found: {MCP_SH}"
        assert os.access(MCP_SH, os.X_OK), f"mcp.sh is not executable: {MCP_SH}"


class TestNonRegression:
    """mcp package 未導入状態で既存 CLI が従来通り動くことを確認する。"""

    def test_vvread_help_works_without_mcp(self, tmp_path):
        """mcp 未導入でも vvread --help が動作すること。"""
        result = _run(
            [str(VVREAD), "--help"],
            env=_path_env(tmp_path),
            timeout=5,
        )
        assert result.returncode == 0  # --help は exit 0
        assert "Usage: vvread" in result.stderr

    def test_mcp_sh_only_fails_without_mcp(self, tmp_path):
        """mcp.sh だけが mcp 未導入時に案内付きで失敗すること。

        bin/vvread は VVREAD_PROJECT_DIR を自身の場所から export するため、
        absent-mcp テストは mcp.sh を直接呼ぶ形でのみ確実に検証できる。
        """
        result = _run(
            [str(MCP_SH)],
            env=_absent_mcp_env(tmp_path),
            timeout=5,
        )
        assert result.returncode == 1
        assert "mcp" in result.stderr.lower()


# ---------------------------------------------------------------------------
# MCP SDK テスト（mcp package 必要）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp package required: uv sync --extra mcp")
class TestMcpServerTools:
    """MCP SDK を使って mcp_server.py の tool を実際に呼び出すテスト。"""

    @pytest.fixture
    def python_path(self):
        return sys.executable

    def _run_server_python(self, code: str, env: dict = None, timeout: int = 10) -> str:
        """mcp_server.py と同じ Python で code を実行し stdout を返す。"""
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        result = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=full_env, timeout=timeout,
        )
        return result

    def test_mcp_server_imports_cleanly(self):
        """mcp_server.py が import エラーなく読み込めること。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"print('OK')"
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_mcp_server_defines_five_tools(self):
        """mcp_server が 5 つの tool を定義していること。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"tools = mod.mcp._tool_manager._tools; "
            f"print(sorted(tools.keys()))"
        )
        assert result.returncode == 0, result.stderr
        assert "vvread_say" in result.stdout
        assert "vvread_stop" in result.stdout
        assert "vvread_status" in result.stdout
        assert "vvread_speakers" in result.stdout
        assert "vvread_config_set" in result.stdout

    def test_vvread_binary_path_is_install_relative(self):
        """VVREAD パスが mcp_server.py の install 場所から解決されること（CLAUDE_PROJECT_DIR 非依存）。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"print(str(mod.VVREAD))",
            env={"CLAUDE_PROJECT_DIR": "/nonexistent/project"},
        )
        assert result.returncode == 0, result.stderr
        vvread_path = result.stdout.strip()
        assert vvread_path.endswith("bin/vvread"), f"unexpected path: {vvread_path}"
        assert "/nonexistent/" not in vvread_path

    def test_cwd_uses_claude_project_dir(self):
        """CWD が CLAUDE_PROJECT_DIR から解決されること。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"print(str(mod.CWD))",
            env={"CLAUDE_PROJECT_DIR": str(REPO)},
        )
        assert result.returncode == 0, result.stderr
        assert str(REPO) in result.stdout.strip()

    def test_vvread_say_returns_started_immediately(self, tmp_path):
        """vvread_say が即時 'started' を返すこと（subprocess が完了を待たない）。"""
        import asyncio

        sys.path.insert(0, str(REPO / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", str(MCP_SERVER))
        mod = importlib.util.module_from_spec(spec)

        # VVREAD を sleep 10 のダミーに置き換えて即時復帰を検証
        dummy_vvread = tmp_path / "fake_vvread"
        dummy_vvread.write_text("#!/bin/bash\nsleep 10\n")
        dummy_vvread.chmod(0o755)

        spec.loader.exec_module(mod)
        original_vvread = mod.VVREAD
        mod.VVREAD = dummy_vvread

        try:
            start = time.time()
            asyncio.run(
                mod.mcp._tool_manager.call_tool("vvread_say", {"text": "test"})
            )
            elapsed = time.time() - start
            assert elapsed < 2.0, f"vvread_say took too long: {elapsed:.2f}s"
        finally:
            mod.VVREAD = original_vvread

    def test_vvread_say_passes_source_mcp_env(self, tmp_path):
        """vvread_say が Popen に VVREAD_SAY_SOURCE=mcp + CREATED_MS(ms) を渡すこと。"""
        import asyncio
        import io

        sys.path.insert(0, str(REPO / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_server", str(MCP_SERVER))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        captured = {}

        class FakePopen:
            def __init__(self, *args, **kwargs):
                captured["env"] = kwargs.get("env")
                self.stdin = io.BytesIO()

            def wait(self):
                return 0

        original = mod.subprocess.Popen
        mod.subprocess.Popen = FakePopen
        try:
            asyncio.run(
                mod.mcp._tool_manager.call_tool("vvread_say", {"text": "hi"})
            )
        finally:
            mod.subprocess.Popen = original

        env = captured.get("env")
        assert env is not None, "Popen env was not passed"
        assert env.get("VVREAD_SAY_SOURCE") == "mcp"
        created = env.get("VVREAD_SAY_CREATED_MS", "")
        assert created.isdigit(), created
        assert len(created) >= 12, created

    def test_vvread_say_preempts_previous_playback(self, tmp_path, voicevox_mock, monkeypatch):
        """vvread_say (MCP 経由) が既存の再生を preempt すること。

        start_new_session=True で起動した子プロセスが playing.pid / session.id を
        更新し、旧 playback プロセスを kill できることを確認する。

        test_cmd_say.py::TestStopOldPlayback::test_say_kills_existing_playing_pid
        と同じ検証を MCP 経由（Popen + start_new_session=True）で行う。
        """
        import asyncio

        # 偽プレイヤーを作成
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake_player = bin_dir / "afplay"
        fake_player.write_text("#!/bin/bash\nsleep 5\n")
        fake_player.chmod(0o755)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # 旧 playback プロセスをシミュレート
        old_proc = subprocess.Popen(["sleep", "60"])
        try:
            (state_dir / "playing.pid").write_text(str(old_proc.pid))
            (state_dir / "session.id").write_text("OLD_SESSION_mcp_preempt_test")

            # mcp_server.py の vvread_say が生成するサブプロセス（vvread say）に
            # 必要な環境変数を os.environ に注入する。
            # Popen は env=None（os.environ 継承）なので、事前パッチが有効。
            env_patch = {
                "VVREAD_STATE_DIR": str(state_dir),
                "VVREAD_LOG_DIR": str(tmp_path / "log"),
                "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
                "VVREAD_PROJECT_SETTINGS": str(tmp_path / "no-project.json"),
                "VOICEVOX_ENGINE_URL": voicevox_mock["url"],
                "VOICEVOX_ENGINES": voicevox_mock["url"],
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "VVREAD_PLAYER": "afplay",
            }

            sys.path.insert(0, str(REPO / "scripts"))
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"mcp_server_preempt_{id(tmp_path)}", str(MCP_SERVER)
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # env / VVREAD は monkeypatch で差し替え（例外時も自動復元・テスト分離）
            monkeypatch.setattr(mod, "VVREAD", VVREAD)
            for k, v in env_patch.items():
                monkeypatch.setenv(k, v)

            asyncio.run(
                mod.mcp._tool_manager.call_tool("vvread_say", {"text": "テスト"})
            )
            # Popen は即時復帰。vvread say が playing.pid を kill するまで待つ
            try:
                old_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "旧 playback が MCP 経由の vvread_say 起動後に kill されなかった。"
                    "start_new_session=True で preemption が壊れている可能性。"
                )

            # session.id が更新されるまで polling（固定 sleep を避ける）
            def _session_changed():
                try:
                    s = (state_dir / "session.id").read_text().strip()
                except OSError:
                    return False
                return s not in ("", "OLD_SESSION_mcp_preempt_test")

            _wait_until(_session_changed, timeout=5)
            new_session = (state_dir / "session.id").read_text().strip()
            assert new_session != "OLD_SESSION_mcp_preempt_test", (
                "session.id が更新されなかった（vvread say が kill 後に異常終了した可能性）"
            )
        finally:
            if old_proc.poll() is None:
                old_proc.kill()
                old_proc.wait()

    def test_vvread_speakers_tool_has_no_engine_url_param(self):
        """vvread_speakers は engine_url 引数を持たないこと（primary Engine 固定）。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util, inspect; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"sig = inspect.signature(mod.vvread_speakers); "
            f"print(list(sig.parameters.keys()))"
        )
        assert result.returncode == 0, result.stderr
        # engine_url パラメータが存在しないことを確認
        assert "engine_url" not in result.stdout

    def test_vvread_config_set_docstring_mentions_user_explicit(self):
        """vvread_config_set docstring に 'explicitly' が含まれること。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"print(mod.vvread_config_set.__doc__)"
        )
        assert result.returncode == 0, result.stderr
        assert "explicitly" in result.stdout

    def test_vvread_config_set_allowlist_excludes_engine_url(self):
        """_CONFIG_ALLOWLIST に engineUrl / engines が含まれないこと。"""
        result = self._run_server_python(
            f"import sys; sys.path.insert(0, '{REPO}/scripts'); "
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}'); "
            f"mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); "
            f"print(list(mod._CONFIG_ALLOWLIST.keys()))"
        )
        assert result.returncode == 0, result.stderr
        assert "voicevox.engineUrl" not in result.stdout
        assert "voicevox.engines" not in result.stdout
        assert "voicevox.speaker" in result.stdout

    def test_tool_annotations_exposed_via_tools_list(self, tmp_path):
        """tools/list で destructiveHint / readOnlyHint が露出すること。"""
        import textwrap
        code = textwrap.dedent(f"""
import sys
sys.path.insert(0, '{REPO}/scripts')
import asyncio, importlib.util, json
spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

async def run():
    async with create_connected_server_and_client_session(mod.mcp) as client:
        result = await client.list_tools()
        tools = {{t.name: t for t in result.tools}}
        ann_config = tools.get('vvread_config_set')
        ann_speakers = tools.get('vvread_speakers')
        print(json.dumps({{
            'config_destructive': ann_config.annotations.destructiveHint if ann_config and ann_config.annotations else None,
            'speakers_readonly': ann_speakers.annotations.readOnlyHint if ann_speakers and ann_speakers.annotations else None,
        }}))

asyncio.run(run())
""")
        result = self._run_server_python(code)
        # SDK 未導入は class-level skipif で除外済み。SDK 導入済みで probe が
        # 失敗した場合は skip せず fail にする（annotations 回帰を skip に隠さない）。
        assert result.returncode == 0, (
            f"annotations probe failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
        import json as _json
        data = _json.loads(result.stdout.strip())
        assert data.get("config_destructive") is True, (
            f"vvread_config_set.destructiveHint が true でない: {data}"
        )
        assert data.get("speakers_readonly") is True, (
            f"vvread_speakers.readOnlyHint が true でない: {data}"
        )

    def _run_logging_probe(self):
        """logging で WARNING を意図的に 2 件発生させる probe を実行し result を返す。

        probe は root logger と 'mcp' logger に warning を出し、stdout には
        STDOUT_CLEAN のみを print する。warning は検証用に意図的に発生させている
        （正しい構成なら stderr へ、stdout には混入しないはず）。
        """
        code = textwrap.dedent(f"""
import sys
sys.path.insert(0, '{REPO}/scripts')
import importlib.util, logging

spec = importlib.util.spec_from_file_location('mcp_server', '{MCP_SERVER}')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# WARNING ログを意図的に 2 件出す（stderr へ流れるべき・stdout に混入しないこと）
logging.getLogger().warning('test warning')
logging.getLogger('mcp').warning('mcp warning')

# stdout には何も出ていないことを確認
print('STDOUT_CLEAN')
""")
        return self._run_server_python(code)

    def test_mcp_stdout_not_polluted_by_logging(self, tmp_path):
        """MCP server の stdout に通常ログが混入しないこと。"""
        result = self._run_logging_probe()
        assert result.stdout == "STDOUT_CLEAN\n", f"stdout polluted: {result.stdout!r}"

    def test_mcp_warning_is_written_to_stderr(self, tmp_path):
        """意図的に出した WARNING が stderr に出力されること（stdout ではなく）。"""
        result = self._run_logging_probe()
        assert "warning" in result.stderr.lower(), f"stderr missing warning: {result.stderr!r}"
