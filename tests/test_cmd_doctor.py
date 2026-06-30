"""scripts/cmd_doctor.sh + bin/vvread doctor の統合テスト (R-009)

doctor.py 単体は test_doctor.py で網羅。本テストは bash ラッパー + bin/vvread
dispatch + CLI フラグ + 終了コード仕様(R-009 ユーザ指定)に集中する。

ユーザ仕様の終了コード:
  0 = OK / WARN のみ
  1 = ERROR あり
  2 = doctor 自体の使い方エラー / 不正オプション(argparse default)
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VVREAD = REPO / "bin" / "vvread"
CMD_DOCTOR = REPO / "scripts" / "cmd" / "doctor.sh"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _path_env(tmp_path: Path) -> dict:
    return {
        "VVREAD_STATE_DIR": str(tmp_path / "state"),
        "VVREAD_LOG_DIR": str(tmp_path / "log"),
        "VVREAD_CACHE_DIR": str(tmp_path / "cache"),
        "VVREAD_PROJECT_SETTINGS": str(tmp_path / "no-project-settings.json"),
    }


def _clean_env(env_extra=None) -> dict:
    """親プロセスの VOICEVOX_* / VVREAD_* を継承させない"""
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env_extra:
        base.update(env_extra)
    return base


def run_doctor(*args, env_extra=None, cwd=None, timeout=20):
    return subprocess.run(
        [str(CMD_DOCTOR), *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


def run_vvread_doctor(*args, env_extra=None, cwd=None, timeout=20):
    return subprocess.run(
        [str(VVREAD), "doctor", *args],
        env=_clean_env(env_extra),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 終了コード仕様 (R-009 ユーザ指定)
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_offline_run_exits_0_when_no_errors(self, tmp_path):
        """通常の --offline 実行で warning は出るかもしれないが ERROR が無ければ exit 0"""
        env = _path_env(tmp_path)
        # HOME を tmp に向けて user-level hook 検査も独立化
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        # ERROR が無ければ 0(WARN は OK)
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert "summary:" in r.stdout

    def test_required_dep_missing_exits_1(self, tmp_path):
        """必須依存(bash/python3/curl)が PATH に無いと exit 1"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        # 完全に PATH を空にすると cmd_doctor.sh の起動自体できないので、
        # python3 だけ残して他の required (bash も) を取り除く方針で再現する
        empty_dir = tmp_path / "empty_path"
        empty_dir.mkdir()
        # cmd_doctor.sh は bash で起動されるので bash が無いと走らない。
        # 代わりに `vvread doctor` を /usr/bin/python3 経由 + PATH を最小化して
        # 「依存 check 内で bash が見つからない」状況を作る。
        # bin/bash と /usr/local/bin/python3 だけ残す:
        # → 簡易には bash と python3 を確保しつつ curl だけ落とすケースで検証
        env["PATH"] = "/usr/local/bin:/bin:/usr/bin"  # bash + python3 は残す
        # しかし curl は /usr/bin/curl にあるはず。PATH を tmp_path のみにすると
        # bash がない → cmd_doctor.sh が動かない。代替戦略: --offline で
        # engine 系を落とし、curl だけ消す(`type curl` が見つからない PATH)
        # しかし PATH を "/bin" にすると bash + curl 両方 /bin に無い場合がある。
        # 最もテストとして安定する方法: doctor.py を直接 invoke して PATH を
        # 厳密制御する → これは test_doctor.py の TestCheckDependencies で
        # すでにカバー済み。
        # ここでは bash ラッパー経由の exit code 仕様のみ確認する目的のため、
        # この test は skip + 単体は test_doctor.py 任せとする。
        pytest.skip(
            "cmd_doctor.sh が依存解決の前に bash 自身を要求するため、"
            "bash 不在を bash 経由で再現できない。doctor.py 単体の "
            "TestCheckDependencies::test_required_missing_is_error で網羅済み。"
        )

    def test_unknown_flag_exits_2(self, tmp_path):
        """argparse の不正オプションは exit 2"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--bogus-flag", env_extra=env, cwd=tmp_path)
        assert r.returncode == 2

    def test_unknown_scope_exits_2(self, tmp_path):
        """--scope=bogus は argparse choices で reject、exit 2"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--scope", "weird_value", env_extra=env, cwd=tmp_path)
        assert r.returncode == 2

    def test_strict_flag_accepted_but_v01_does_not_change_exit(self, tmp_path):
        """--strict は受理するが、v0.1 では exit code に影響しない(将来 placeholder)"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r1 = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        r2 = run_doctor("--offline", "--strict", env_extra=env, cwd=tmp_path)
        # 両者の exit code が同じ(WARN しか無くても --strict で exit 1 にならない)
        assert r1.returncode == r2.returncode


# ---------------------------------------------------------------------------
# --offline / --scope の挙動
# ---------------------------------------------------------------------------


class TestOfflineAndScope:
    def test_offline_skips_engine_and_claude(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0
        # plain text に "skipped" が現れる
        assert "skipped" in r.stdout
        # engine セクションに skipped 行がある
        # (text なので簡易な部分一致で十分)
        assert "[engine]" in r.stdout
        assert "[claude]" in r.stdout

    def test_scope_runtime_is_default_excludes_dev(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        # default は runtime のみなので gitleaks は出ない
        assert "gitleaks" not in r.stdout
        assert "shellcheck" not in r.stdout

    def test_scope_all_includes_dev_publish(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--offline", "--scope", "all",
                       env_extra=env, cwd=tmp_path)
        assert "gitleaks" in r.stdout
        # dev カテゴリの shellcheck も出る
        assert "shellcheck" in r.stdout


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_output_well_formed(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_doctor("--offline", "--json", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert "items" in payload
        assert "summary" in payload
        assert isinstance(payload["items"], list)
        # summary に各 status のカウントが揃う
        for k in ("OK", "INFO", "WARN", "ERROR"):
            assert k in payload["summary"]
        # 全 section が現れる
        sections = {item["section"] for item in payload["items"]}
        for s in ("paths", "settings", "dependencies", "player",
                  "engine", "claude", "hooks", "vvread"):
            assert s in sections, f"section {s} 未登場"


# ---------------------------------------------------------------------------
# bin/vvread doctor dispatch
# ---------------------------------------------------------------------------


class TestVvreadDispatch:
    def test_bin_vvread_doctor_offline(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_vvread_doctor("--offline", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert "summary:" in r.stdout

    def test_bin_vvread_doctor_unknown_flag_exits_2(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_vvread_doctor("--no-such-flag",
                              env_extra=env, cwd=tmp_path)
        assert r.returncode == 2

    def test_bin_vvread_doctor_help_via_short(self, tmp_path):
        """python argparse の --help は exit 0(usage 表示)"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        r = run_vvread_doctor("--help", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0
        assert "vvread doctor" in r.stdout or "doctor" in r.stdout


# ---------------------------------------------------------------------------
# settings 経由の表示(env override 経由)
# ---------------------------------------------------------------------------


class TestSettingsIntegration:
    def test_env_override_visible_in_output(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        env["VOICEVOX_SPEAKER"] = "8"
        r = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0
        # voicevox.speaker の行に env が現れる（U-121: 変数名は短縮形で非表示）
        assert "voicevox.speaker" in r.stdout
        assert "[env]" in r.stdout
        assert "VOICEVOX_SPEAKER" not in r.stdout

    def test_unknown_setting_warned(self, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        # cwd の vvread.settings.json に不明キーを置く
        settings_file = tmp_path / "vvread.settings.json"
        settings_file.write_text(json.dumps({"future": {"new_thing": 1}}))
        # _path_env では VVREAD_PROJECT_SETTINGS が non-existent に設定されるため、
        # このテストではテスト用の settings ファイルを明示的に指定する（R-115）
        env["VVREAD_PROJECT_SETTINGS"] = str(settings_file)
        r = run_doctor("--offline", env_extra=env, cwd=tmp_path)
        assert r.returncode == 0
        assert "unknown_key" in r.stdout
        assert "future.new_thing" in r.stdout


# ---------------------------------------------------------------------------
# engine 経路(voicevox_mock 統合)
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    def test_engine_reachable_via_mock(self, voicevox_mock, tmp_path):
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        env["VOICEVOX_ENGINE_URL"] = voicevox_mock["url"]
        # default speakers payload は id 0..3 を含む
        env["VOICEVOX_SPEAKER"] = "0"
        # offline 無し = engine セクションが走る
        r = run_doctor(env_extra=env, cwd=tmp_path)
        # ERROR が無いはず → 0
        assert r.returncode == 0, (
            f"stdout={r.stdout[-1000:]}\nstderr={r.stderr[-1000:]}"
        )
        # engine reachable 行が OK
        assert "engine" in r.stdout
        # speaker=0 が available と表示される
        assert "target_speaker" in r.stdout

    def test_engine_speaker_not_found_exits_1(self, voicevox_mock, tmp_path):
        """speaker=999 は default mock payload に無い → ERROR で exit 1"""
        env = _path_env(tmp_path)
        env["HOME"] = str(tmp_path / "home")
        env["VOICEVOX_ENGINE_URL"] = voicevox_mock["url"]
        env["VOICEVOX_SPEAKER"] = "999"
        r = run_doctor(env_extra=env, cwd=tmp_path)
        assert r.returncode == 1
        assert "999" in r.stdout
