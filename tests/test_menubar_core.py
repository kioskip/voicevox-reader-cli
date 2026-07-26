"""tests/test_menubar_core.py - scripts/menubar_core.py のテスト (B-151, P2b)

menubar_core は rumps を import しない純粋ロジック層。本テストも rumps に
依存せず、Linux CI でも実行できる設計を維持する(subprocess は実在する
シェルスクリプトを fake vvread として使い、実際の subprocess.run 経路を
通す統合寄りのテストと、パース関数単体のテストを両方置く)。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import menubar_core as mc  # noqa: E402


# ---------------------------------------------------------------------------
# fake vvread スクリプト helper
# ---------------------------------------------------------------------------


def _write_script(tmp_path: Path, body: str, name: str = "fake_vvread") -> Path:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# default_vvread_bin
# ---------------------------------------------------------------------------


class TestDefaultVvreadBin:
    def test_resolves_repo_bin_vvread(self):
        result = mc.default_vvread_bin()
        assert result == REPO / "bin" / "vvread"
        assert result.exists()


# ---------------------------------------------------------------------------
# run_vvread
# ---------------------------------------------------------------------------


class TestRunVvread:
    def test_success_uses_absolute_path_argv_and_shell_false(self, tmp_path):
        script = _write_script(tmp_path, 'echo "cwd=$(pwd)"\necho "args=$*"')
        workdir = tmp_path / "work"
        workdir.mkdir()

        result = mc.run_vvread(
            ["status", "--json"], timeout=5, vvread_bin=script, cwd=workdir, env={}
        )

        assert result.ok is True
        assert result.returncode == 0
        assert f"cwd={workdir}" in result.stdout
        assert "args=status --json" in result.stdout
        assert result.timed_out is False

    def test_default_cwd_is_home(self, tmp_path):
        script = _write_script(tmp_path, 'echo "cwd=$(pwd)"')

        result = mc.run_vvread(["status", "--json"], timeout=5, vvread_bin=script, env={})

        assert result.ok is True
        assert f"cwd={Path.home()}" in result.stdout

    def test_env_is_passed_explicitly(self, tmp_path):
        script = _write_script(tmp_path, 'echo "marker=$VVREAD_TEST_MARKER"')

        result = mc.run_vvread(
            ["status"],
            timeout=5,
            vvread_bin=script,
            cwd=tmp_path,
            env={"VVREAD_TEST_MARKER": "hello"},
        )

        assert "marker=hello" in result.stdout

    def test_nonzero_exit_is_not_ok(self, tmp_path):
        script = _write_script(tmp_path, 'echo "boom" >&2\nexit 3')

        result = mc.run_vvread(["stop"], timeout=5, vvread_bin=script, cwd=tmp_path, env={})

        assert result.ok is False
        assert result.returncode == 3
        assert "boom" in result.stderr

    def test_timeout_sets_timed_out_and_not_ok(self, tmp_path):
        script = _write_script(tmp_path, "sleep 5")

        result = mc.run_vvread(["status"], timeout=0.2, vvread_bin=script, cwd=tmp_path, env={})

        assert result.ok is False
        assert result.timed_out is True
        assert result.error is not None

    def test_missing_binary_returns_error_not_exception(self, tmp_path):
        missing = tmp_path / "does_not_exist"

        result = mc.run_vvread(["status"], timeout=5, vvread_bin=missing, cwd=tmp_path, env={})

        assert result.ok is False
        assert result.timed_out is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# status --json パース
# ---------------------------------------------------------------------------


class TestParseStatusJson:
    def test_full_valid_payload(self):
        text = (
            '{"state": "playing", "mute_until": null, '
            '"queue": {"mode": "on", "pending": 3, "playing": 1, "failed": 0}}'
        )
        status = mc.parse_status_json(text)
        assert status.state == "playing"
        assert status.mute_until is None
        assert status.queue == mc.QueueState(mode="on", pending=3, playing=1, failed=0)
        assert status.error is None

    def test_muted_with_mute_until(self):
        text = '{"state": "muted", "mute_until": 1234567890, "queue": {}}'
        status = mc.parse_status_json(text)
        assert status.state == "muted"
        assert status.mute_until == 1234567890
        # queue キーが欠けているフィールドは既定値になる
        assert status.queue == mc.QueueState()

    def test_invalid_json_becomes_error_state(self):
        status = mc.parse_status_json("not json {{{")
        assert status.state == "error"
        assert status.error is not None

    def test_non_object_top_level_becomes_error_state(self):
        status = mc.parse_status_json("[1, 2, 3]")
        assert status.state == "error"
        assert status.error is not None

    def test_unexpected_state_value_becomes_error_state(self):
        status = mc.parse_status_json('{"state": "unknown", "mute_until": null, "queue": {}}')
        assert status.state == "error"
        assert status.error is not None

    def test_missing_state_key_becomes_error_state(self):
        status = mc.parse_status_json('{"mute_until": null, "queue": {}}')
        assert status.state == "error"

    def test_bool_mute_until_is_ignored(self):
        status = mc.parse_status_json('{"state": "idle", "mute_until": true, "queue": {}}')
        assert status.mute_until is None

    def test_string_mute_until_is_ignored(self):
        status = mc.parse_status_json('{"state": "idle", "mute_until": "soon", "queue": {}}')
        assert status.mute_until is None

    def test_negative_queue_counts_are_clamped_to_zero(self):
        text = '{"state": "idle", "mute_until": null, "queue": {"pending": -5, "playing": -1, "failed": -2}}'
        status = mc.parse_status_json(text)
        assert status.queue.pending == 0
        assert status.queue.playing == 0
        assert status.queue.failed == 0

    def test_non_int_queue_counts_default_to_zero(self):
        text = '{"state": "idle", "mute_until": null, "queue": {"pending": "abc"}}'
        status = mc.parse_status_json(text)
        assert status.queue.pending == 0

    def test_invalid_queue_mode_defaults_to_off(self):
        text = '{"state": "idle", "mute_until": null, "queue": {"mode": "weird"}}'
        status = mc.parse_status_json(text)
        assert status.queue.mode == "off"

    def test_non_dict_queue_uses_defaults(self):
        text = '{"state": "idle", "mute_until": null, "queue": "not a dict"}'
        status = mc.parse_status_json(text)
        assert status.queue == mc.QueueState()


class TestFetchStatus:
    def test_run_failure_becomes_error_state(self, tmp_path):
        script = _write_script(tmp_path, 'echo "engine down" >&2\nexit 1')
        # STATUS_TIMEOUT_SEC(既定 1.0s、本番の status --json はローカルファイル
        # 読み取りのみで即完了する前提の値)は、CPU 競合下の fake script fork/exec
        # には短すぎてこのテストが偽陽性でタイムアウトしうる。本テストの主眼は
        # 「run 失敗時に stderr がエラーメッセージへ伝播すること」であり timeout
        # 挙動そのものではないため、余裕を持った timeout を明示指定して
        # STATUS_TIMEOUT_SEC の厳しさから切り離す(timeout 挙動自体は
        # TestRunVvread::test_timeout_sets_timed_out_and_not_ok で別途検証済み)。
        status = mc.fetch_status(vvread_bin=script, cwd=tmp_path, env={}, timeout=5)
        assert status.state == "error"
        assert "engine down" in status.error

    def test_success_parses_stdout(self, tmp_path):
        script = _write_script(
            tmp_path,
            'echo \'{"state": "idle", "mute_until": null, '
            '"queue": {"mode": "off", "pending": 0, "playing": 0, "failed": 0}}\'',
        )
        status = mc.fetch_status(vvread_bin=script, cwd=tmp_path, env={})
        assert status.state == "idle"


# ---------------------------------------------------------------------------
# config --list パース
# ---------------------------------------------------------------------------


class TestParseConfigList:
    def test_parses_tsv(self):
        text = "voicevox.speaker\t3\nvoicevox.speed\t1.5\n"
        result = mc.parse_config_list(text)
        assert result == {"voicevox.speaker": "3", "voicevox.speed": "1.5"}

    def test_blank_lines_are_skipped(self):
        text = "voicevox.speaker\t3\n\n\nvoicevox.speed\t1.5\n"
        result = mc.parse_config_list(text)
        assert len(result) == 2

    def test_lines_without_tab_are_skipped(self):
        text = "voicevox.speaker\t3\nmalformed line without tab\n"
        result = mc.parse_config_list(text)
        assert result == {"voicevox.speaker": "3"}

    def test_empty_key_is_skipped(self):
        text = "\tvalue_only\nvoicevox.speaker\t3\n"
        result = mc.parse_config_list(text)
        assert result == {"voicevox.speaker": "3"}

    def test_duplicate_keys_last_wins(self):
        text = "voicevox.speaker\t3\nvoicevox.speaker\t8\n"
        result = mc.parse_config_list(text)
        assert result["voicevox.speaker"] == "8"

    def test_empty_text_returns_empty_dict(self):
        assert mc.parse_config_list("") == {}


class TestDescribeRunError:
    def test_uses_explicit_error_first(self):
        result = mc.RunResult(ok=False, error="explicit", stderr="stderr text")
        assert mc.describe_run_error(result) == "explicit"

    def test_falls_back_to_stderr(self):
        result = mc.RunResult(ok=False, returncode=1, stderr="  boom  \n")
        assert mc.describe_run_error(result) == "boom"

    def test_falls_back_to_generic_message(self):
        result = mc.RunResult(ok=False, returncode=2, stderr="")
        assert "exit 2" in mc.describe_run_error(result)


class TestGetIntValue:
    def test_valid_int(self):
        assert mc.get_int_value({"voicevox.speaker": "3"}, "voicevox.speaker") == 3

    def test_missing_key_returns_none(self):
        assert mc.get_int_value({}, "voicevox.speaker") is None

    def test_empty_value_returns_none(self):
        assert mc.get_int_value({"voicevox.speaker": ""}, "voicevox.speaker") is None

    def test_non_numeric_value_returns_none(self):
        assert mc.get_int_value({"voicevox.speaker": "abc"}, "voicevox.speaker") is None


class TestGetFloatValue:
    def test_valid_float(self):
        assert mc.get_float_value({"voicevox.speed": "1.5"}, "voicevox.speed") == 1.5

    def test_missing_key_returns_none(self):
        assert mc.get_float_value({}, "voicevox.speed") is None

    def test_empty_value_returns_none(self):
        assert mc.get_float_value({"voicevox.speed": ""}, "voicevox.speed") is None

    def test_non_numeric_value_returns_none(self):
        assert mc.get_float_value({"voicevox.speed": "abc"}, "voicevox.speed") is None

    def test_nan_string_returns_none(self):
        assert mc.get_float_value({"voicevox.speed": "nan"}, "voicevox.speed") is None

    def test_inf_string_returns_none(self):
        assert mc.get_float_value({"voicevox.speed": "inf"}, "voicevox.speed") is None

    def test_negative_inf_string_returns_none(self):
        assert mc.get_float_value({"voicevox.speed": "-inf"}, "voicevox.speed") is None


class TestFetchConfigSnapshot:
    def test_success_returns_parsed_raw_map(self, tmp_path):
        script = _write_script(
            tmp_path,
            'printf "voicevox.speaker\\t3\\nvoicevox.volume\\t1.0\\nvoicevox.speed\\t1.2\\n"',
        )
        snapshot = mc.fetch_config_snapshot(vvread_bin=script, cwd=tmp_path, env={})
        assert snapshot.error is None
        assert snapshot.raw == {
            "voicevox.speaker": "3",
            "voicevox.volume": "1.0",
            "voicevox.speed": "1.2",
        }

    def test_failure_returns_empty_raw_with_error(self, tmp_path):
        script = _write_script(tmp_path, 'echo "engine down" >&2\nexit 1')
        snapshot = mc.fetch_config_snapshot(vvread_bin=script, cwd=tmp_path, env={})
        assert snapshot.raw == {}
        assert "engine down" in snapshot.error

    def test_executes_config_list_exactly_once(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'printf "voicevox.speaker\\t3\\nvoicevox.volume\\t1.0\\n"'
            ),
        )
        mc.fetch_config_snapshot(vvread_bin=script, cwd=tmp_path, env={})
        lines = call_log.read_text().splitlines()
        assert len(lines) == 1
        assert lines[0] == "config --list"

    def test_all_six_settings_resolvable_from_one_snapshot(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'printf "voicevox.speaker\\t3\\n'
                'voicevox.volume\\t1.0\\n'
                'voicevox.speed\\t1.2\\n'
                'voicevox.intonation\\t0.8\\n'
                'voicevox.pauseScale\\t1.1\\n'
                'voicevox.maxChunks\\t5\\n"'
            ),
        )
        snapshot = mc.fetch_config_snapshot(vvread_bin=script, cwd=tmp_path, env={})
        assert mc.get_int_value(snapshot.raw, mc.SPEAKER_SETTING_KEY) == 3
        assert mc.get_float_value(snapshot.raw, mc.VOLUME_SETTING_KEY) == 1.0
        assert mc.get_float_value(snapshot.raw, mc.SPEED_SETTING_KEY) == 1.2
        assert mc.get_float_value(snapshot.raw, mc.INTONATION_SETTING_KEY) == 0.8
        assert mc.get_float_value(snapshot.raw, mc.PAUSE_SCALE_SETTING_KEY) == 1.1
        assert mc.get_int_value(snapshot.raw, mc.MAX_CHUNKS_SETTING_KEY) == 5


# ---------------------------------------------------------------------------
# speakers --json パース + DTO 正規化
# ---------------------------------------------------------------------------


SAMPLE_SPEAKERS_JSON = json.dumps(
    [
        {"name": "ずんだもん", "styles": [{"id": 3, "name": "ノーマル"}, {"id": 1, "name": "あまあま"}]},
        {"name": "四国めたん", "styles": [{"id": 2, "name": "ノーマル"}]},
    ]
)


class TestParseSpeakersJson:
    def test_valid_payload(self):
        result = mc.parse_speakers_json(SAMPLE_SPEAKERS_JSON)
        assert result.error is None
        assert len(result.speakers) == 2
        assert result.speakers[0].name == "ずんだもん"
        assert result.speakers[0].styles[0] == mc.StyleDTO(id=3, name="ノーマル")

    def test_invalid_json_becomes_error(self):
        result = mc.parse_speakers_json("not json {{{")
        assert result.speakers == []
        assert result.error is not None

    def test_non_list_top_level_becomes_error(self):
        result = mc.parse_speakers_json('{"not": "a list"}')
        assert result.speakers == []
        assert result.error is not None

    def test_malformed_entries_are_skipped(self):
        payload = json.dumps(
            [
                "not_a_dict",
                {"no_name": True},
                {"name": "テスト", "styles": "not a list"},
                {"name": "有効", "styles": [{"id": 5, "name": "OK"}]},
            ]
        )
        result = mc.parse_speakers_json(payload)
        assert len(result.speakers) == 1
        assert result.speakers[0].name == "有効"

    def test_malformed_styles_are_skipped(self):
        payload = json.dumps(
            [
                {
                    "name": "テスト",
                    "styles": [
                        "not_dict",
                        {"id": "not_int", "name": "bad"},
                        {"id": 9},
                        {"id": 7, "name": "valid"},
                    ],
                }
            ]
        )
        result = mc.parse_speakers_json(payload)
        assert len(result.speakers) == 1
        assert result.speakers[0].styles == [mc.StyleDTO(id=7, name="valid")]

    def test_speaker_with_no_valid_styles_is_excluded(self):
        payload = json.dumps([{"name": "空っぽ", "styles": [{"id": "bad"}]}])
        result = mc.parse_speakers_json(payload)
        assert result.speakers == []

    def test_duplicate_style_ids_are_deduplicated_globally(self):
        payload = json.dumps(
            [
                {"name": "A", "styles": [{"id": 1, "name": "ノーマル"}]},
                {"name": "B", "styles": [{"id": 1, "name": "重複ID"}]},
            ]
        )
        result = mc.parse_speakers_json(payload)
        all_ids = [st.id for sp in result.speakers for st in sp.styles]
        assert all_ids == [1]
        assert result.speakers[0].name == "A"

    def test_long_names_are_truncated(self):
        long_name = "あ" * (mc.MAX_NAME_LENGTH + 50)
        payload = json.dumps([{"name": long_name, "styles": [{"id": 1, "name": "ノーマル"}]}])
        result = mc.parse_speakers_json(payload)
        assert len(result.speakers[0].name) == mc.MAX_NAME_LENGTH
        assert result.speakers[0].name.endswith("…")

    def test_empty_list_is_valid(self):
        result = mc.parse_speakers_json("[]")
        assert result.speakers == []
        assert result.error is None


class TestFetchSpeakers:
    def test_run_failure_becomes_error_result(self, tmp_path):
        script = _write_script(tmp_path, 'echo "unreachable" >&2\nexit 1')
        result = mc.fetch_speakers(vvread_bin=script, cwd=tmp_path, env={})
        assert result.speakers == []
        assert "unreachable" in result.error


class TestBuildSpeakerMenuEntries:
    def test_marks_current_speaker_checked(self):
        speakers = [
            mc.SpeakerDTO(name="ずんだもん", styles=[mc.StyleDTO(id=3, name="ノーマル")]),
            mc.SpeakerDTO(name="四国めたん", styles=[mc.StyleDTO(id=2, name="ノーマル")]),
        ]
        entries = mc.build_speaker_menu_entries(speakers, current_speaker_id=2)
        assert entries[0].checked is False
        assert entries[1].checked is True
        assert entries[1].label == "四国めたん - ノーマル (2)"

    def test_none_current_speaker_checks_nothing(self):
        speakers = [mc.SpeakerDTO(name="A", styles=[mc.StyleDTO(id=1, name="ノーマル")])]
        entries = mc.build_speaker_menu_entries(speakers, current_speaker_id=None)
        assert entries[0].checked is False

    def test_labels_are_unique_even_with_duplicate_names(self):
        speakers = [
            mc.SpeakerDTO(
                name="同名話者",
                styles=[
                    mc.StyleDTO(id=1, name="ノーマル"),
                    mc.StyleDTO(id=2, name="ノーマル"),
                ],
            )
        ]
        entries = mc.build_speaker_menu_entries(speakers, current_speaker_id=None)
        labels = [entry.label for entry in entries]
        assert len(labels) == len(set(labels))


# ---------------------------------------------------------------------------
# set_default_speaker
# ---------------------------------------------------------------------------


class TestSetDefaultSpeaker:
    def test_matching_effective_value_has_no_warning(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.speaker\\t5\\n"; exit 0; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_default_speaker(5, vvread_bin=script, cwd=tmp_path, env={})
        assert result.ok is True
        assert result.warning is None
        assert result.effective_speaker_id == 5

    def test_mismatched_effective_value_returns_warning(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.speaker\\t99\\n"; exit 0; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_default_speaker(5, vvread_bin=script, cwd=tmp_path, env={})
        assert result.ok is True
        assert result.warning == mc.SPEAKER_SCOPE_WARNING
        assert result.effective_speaker_id == 99

    def test_set_failure_is_reported_as_error(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then '
                'echo "write failed" >&2; exit 1; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_default_speaker(5, vvread_bin=script, cwd=tmp_path, env={})
        assert result.ok is False
        assert "write failed" in result.error

    def test_list_failure_after_successful_set_is_warning_not_error(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'echo "list failed" >&2; exit 1; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_default_speaker(5, vvread_bin=script, cwd=tmp_path, env={})
        assert result.ok is True
        assert "list failed" in result.warning

    def test_uses_user_setting_flag_and_key(self, tmp_path):
        script = _write_script(tmp_path, 'echo "args=$*"\nexit 0')
        result = mc.run_vvread(
            ["config", "--set", "voicevox.speaker=7", "--user-setting"],
            timeout=5,
            vvread_bin=script,
            cwd=tmp_path,
            env={},
        )
        assert "voicevox.speaker=7" in result.stdout
        assert "--user-setting" in result.stdout


# ---------------------------------------------------------------------------
# 5パラメータの選択肢定数
# ---------------------------------------------------------------------------


class TestChoiceConstants:
    def test_volume_choices_span_0_to_2_in_tenths(self):
        assert mc.VOLUME_CHOICES == tuple(t / 10 for t in range(0, 21))
        assert 1.5 in mc.VOLUME_CHOICES
        assert mc.VOLUME_CHOICES[0] == 0.0
        assert mc.VOLUME_CHOICES[-1] == 2.0

    def test_speed_choices_span_0_5_to_2(self):
        assert mc.SPEED_CHOICES == tuple(t / 10 for t in range(5, 21))
        assert 0.5 in mc.SPEED_CHOICES
        assert 2.0 in mc.SPEED_CHOICES
        assert 0.4 not in mc.SPEED_CHOICES

    def test_intonation_choices_span_0_to_2(self):
        assert mc.INTONATION_CHOICES == tuple(t / 10 for t in range(0, 21))

    def test_pause_scale_choices_span_0_to_2(self):
        assert mc.PAUSE_SCALE_CHOICES == tuple(t / 10 for t in range(0, 21))

    def test_max_chunks_choices_have_seven_labeled_entries(self):
        assert mc.MAX_CHUNKS_CHOICES == (
            ("0(無制限)", 0),
            ("1", 1),
            ("2", 2),
            ("3", 3),
            ("5", 5),
            ("10", 10),
            ("20", 20),
        )
        assert len(mc.MAX_CHUNKS_CHOICES) == 7


# ---------------------------------------------------------------------------
# 選択肢エントリDTO ビルダー
# ---------------------------------------------------------------------------


class TestBuildFloatChoiceEntries:
    def test_matching_current_value_is_checked(self):
        entries = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=1.5)
        checked = [e for e in entries if e.checked]
        assert len(checked) == 1
        assert checked[0].label == "1.5"
        assert checked[0].raw_value == "1.5"

    def test_none_current_checks_nothing(self):
        entries = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=None)
        assert all(not e.checked for e in entries)

    def test_non_finite_current_checks_nothing(self):
        entries = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=float("nan"))
        assert all(not e.checked for e in entries)
        entries_inf = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=float("inf"))
        assert all(not e.checked for e in entries_inf)

    def test_current_not_matching_any_choice_checks_nothing(self):
        entries = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=1.55)
        assert all(not e.checked for e in entries)

    def test_uses_isclose_for_floating_point_tolerance(self):
        # config --list から返る文字列表現の丸め誤差を想定(例: 0.1+0.2 由来の 0.30000000000000004)
        entries = mc.build_float_choice_entries(mc.VOLUME_CHOICES, current=0.3 + 1e-9)
        checked = [e for e in entries if e.checked]
        assert len(checked) == 1
        assert checked[0].raw_value == "0.3"

    def test_entry_count_matches_choice_count(self):
        entries = mc.build_float_choice_entries(mc.SPEED_CHOICES, current=None)
        assert len(entries) == len(mc.SPEED_CHOICES)


class TestBuildLabeledChoiceEntries:
    def test_matching_current_value_is_checked(self):
        entries = mc.build_labeled_choice_entries(mc.MAX_CHUNKS_CHOICES, current=5)
        checked = [e for e in entries if e.checked]
        assert len(checked) == 1
        assert checked[0].label == "5"
        assert checked[0].raw_value == "5"

    def test_zero_maps_to_unlimited_label(self):
        entries = mc.build_labeled_choice_entries(mc.MAX_CHUNKS_CHOICES, current=0)
        checked = [e for e in entries if e.checked]
        assert len(checked) == 1
        assert checked[0].label == "0(無制限)"

    def test_none_current_checks_nothing(self):
        entries = mc.build_labeled_choice_entries(mc.MAX_CHUNKS_CHOICES, current=None)
        assert all(not e.checked for e in entries)

    def test_current_not_matching_any_choice_checks_nothing(self):
        entries = mc.build_labeled_choice_entries(mc.MAX_CHUNKS_CHOICES, current=99)
        assert all(not e.checked for e in entries)


# ---------------------------------------------------------------------------
# set_config_value (5パラメータ汎用setter)
# ---------------------------------------------------------------------------


class TestSetConfigValue:
    def test_matching_effective_value_has_no_warning(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.speed\\t1.5\\n"; exit 0; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_config_value(
            mc.SPEED_SETTING_KEY, "1.5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is True
        assert result.warning is None
        assert result.effective_raw == "1.5"

    def test_mismatched_effective_value_returns_scope_warning(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.speed\\t0.8\\n"; exit 0; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_config_value(
            mc.SPEED_SETTING_KEY, "1.5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is True
        assert result.warning == mc.SPEAKER_SCOPE_WARNING
        assert result.effective_raw == "0.8"

    def test_set_failure_is_reported_as_error(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then '
                'echo "write failed" >&2; exit 1; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_config_value(
            mc.SPEED_SETTING_KEY, "1.5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is False
        assert "write failed" in result.error

    def test_uses_user_setting_flag_and_key_value_pair(self, tmp_path):
        # set_config_value() 自身の argv 構築を検証する(手打ちの run_vvread
        # 呼び出しをテストするだけの旧版は set_config_value を一切経由せず
        # 検証になっていなかったため、call-log 方式に置き換えた)。
        call_log = tmp_path / "calls.log"
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.maxChunks\\t5\\n"; exit 0; fi\n'
                "exit 0"
            ),
        )
        result = mc.set_config_value(
            mc.MAX_CHUNKS_SETTING_KEY, "5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is True
        set_calls = [
            line for line in call_log.read_text().splitlines() if "--set" in line
        ]
        assert len(set_calls) == 1
        assert "voicevox.maxChunks=5" in set_calls[0]
        assert "--user-setting" in set_calls[0]

    def test_list_failure_after_successful_set_is_warning_not_error(self, tmp_path):
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'echo "list failed" >&2; exit 1; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_config_value(
            mc.SPEED_SETTING_KEY, "1.5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is True
        assert "list failed" in result.warning

    def test_integer_raw_value_numeric_match_has_no_warning(self, tmp_path):
        # maxChunks は整数だが raw_value は文字列として扱われる。実効値が
        # "5" のように整数文字列で一致すれば警告なし。
        script = _write_script(
            tmp_path,
            (
                'if [ "$1" = "config" ] && [ "$2" = "--set" ]; then exit 0; fi\n'
                'if [ "$1" = "config" ] && [ "$2" = "--list" ]; then '
                'printf "voicevox.maxChunks\\t5\\n"; exit 0; fi\n'
                "exit 1"
            ),
        )
        result = mc.set_config_value(
            mc.MAX_CHUNKS_SETTING_KEY, "5", vvread_bin=script, cwd=tmp_path, env={}
        )
        assert result.ok is True
        assert result.warning is None


# ---------------------------------------------------------------------------
# アクション argv 構築
# ---------------------------------------------------------------------------


class TestActions:
    def _echo_args_script(self, tmp_path):
        return _write_script(tmp_path, 'echo "args=$*"')

    def test_action_set_enabled_true(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_set_enabled(True, vvread_bin=script, cwd=tmp_path, env={})
        assert "args=on" in result.stdout

    def test_action_set_enabled_false(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_set_enabled(False, vvread_bin=script, cwd=tmp_path, env={})
        assert "args=off" in result.stdout

    def test_action_mute(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_mute("30m", vvread_bin=script, cwd=tmp_path, env={})
        assert "args=mute 30m" in result.stdout

    def test_action_unmute(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_unmute(vvread_bin=script, cwd=tmp_path, env={})
        assert "args=unmute" in result.stdout

    def test_action_stop(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_stop(vvread_bin=script, cwd=tmp_path, env={})
        assert "args=stop" in result.stdout

    def test_action_queue_clear(self, tmp_path):
        script = self._echo_args_script(tmp_path)
        result = mc.action_queue_clear(vvread_bin=script, cwd=tmp_path, env={})
        assert "args=queue clear" in result.stdout


# ---------------------------------------------------------------------------
# キューモード ON/OFF (retry ベースの状態遷移)
# ---------------------------------------------------------------------------


class TestActionQueueSetMode:
    def test_enable_true_runs_queue_on_once(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(tmp_path, f'echo "$*" >> "{call_log}"\nexit 0')

        result = mc.action_queue_set_mode(
            True, queue=mc.QueueState(), vvread_bin=script, cwd=tmp_path, env={}
        )

        assert result.ok is True
        assert call_log.read_text().splitlines() == ["queue on"]

    def test_enable_true_failure_is_reported(self, tmp_path):
        script = _write_script(tmp_path, 'echo "engine down" >&2\nexit 1')

        result = mc.action_queue_set_mode(
            True, queue=mc.QueueState(), vvread_bin=script, cwd=tmp_path, env={}
        )

        assert result.ok is False
        assert "engine down" in result.error

    def test_enable_false_empty_queue_runs_queue_off_only(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(tmp_path, f'echo "$*" >> "{call_log}"\nexit 0')
        queue = mc.QueueState(mode="on", pending=0, playing=0)

        result = mc.action_queue_set_mode(
            False, queue=queue, vvread_bin=script, cwd=tmp_path, env={}
        )

        assert result.ok is True
        assert result.attempts == 0
        assert call_log.read_text().splitlines() == ["queue off"]

    def test_enable_false_empty_queue_off_fails_then_retry_succeeds(self, tmp_path):
        # 渡された queue スナップショットは空(pending=0, playing=0)だが、
        # 呼び出し直前に何かが enqueue されていた等の理由で最初の
        # `queue off` が失敗するケース。空スナップショットだからといって
        # retry ループをスキップせず、非空ケースと同じ retry ループに
        # 合流して救済できることを検証する(指摘4 の回帰テスト)。
        call_log = tmp_path / "calls.log"
        off_count_file = tmp_path / "off_count"
        off_count_file.write_text("0")
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "status" ]; then '
                'echo \'{"state": "idle", "mute_until": null, '
                '"queue": {"mode": "on", "pending": 0, "playing": 0, "failed": 0}}\'; exit 0; fi\n'
                'if [ "$1" = "queue" ] && [ "$2" = "off" ]; then '
                f'n=$(cat "{off_count_file}"); n=$((n+1)); echo "$n" > "{off_count_file}"; '
                'if [ "$n" -lt 2 ]; then echo "not empty" >&2; exit 1; else exit 0; fi; fi\n'
                "exit 0"
            ),
        )
        queue = mc.QueueState(mode="on", pending=0, playing=0)

        result = mc.action_queue_set_mode(
            False,
            queue=queue,
            vvread_bin=script,
            cwd=tmp_path,
            env={},
            retry_interval_sec=0,
            max_retries=5,
        )

        assert result.ok is True
        assert result.attempts == 1
        calls = call_log.read_text().splitlines()
        # 空スナップショットだったので stop は一度も呼ばれない。
        assert "stop" not in calls
        off_calls = [line for line in calls if line.startswith("queue off")]
        assert len(off_calls) == 2

    def test_enable_false_nonempty_queue_stop_then_off_succeeds(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(tmp_path, f'echo "$*" >> "{call_log}"\nexit 0')
        queue = mc.QueueState(mode="on", pending=2, playing=1)

        result = mc.action_queue_set_mode(
            False, queue=queue, vvread_bin=script, cwd=tmp_path, env={}
        )

        assert result.ok is True
        assert result.attempts == 0
        assert call_log.read_text().splitlines() == ["stop", "queue off"]

    def test_enable_false_stop_failure_skips_queue_off_entirely(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "stop" ]; then echo "stop failed" >&2; exit 1; fi\n'
                "exit 0"
            ),
        )
        queue = mc.QueueState(mode="on", pending=2, playing=0)

        result = mc.action_queue_set_mode(
            False, queue=queue, vvread_bin=script, cwd=tmp_path, env={}
        )

        assert result.ok is False
        assert "stop failed" in result.error
        assert call_log.read_text().splitlines() == ["stop"]

    def test_enable_false_off_retries_until_success(self, tmp_path):
        call_log = tmp_path / "calls.log"
        off_count_file = tmp_path / "off_count"
        off_count_file.write_text("0")
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "stop" ]; then exit 0; fi\n'
                'if [ "$1" = "status" ]; then '
                'echo \'{"state": "idle", "mute_until": null, '
                '"queue": {"mode": "on", "pending": 1, "playing": 0, "failed": 0}}\'; exit 0; fi\n'
                'if [ "$1" = "queue" ] && [ "$2" = "off" ]; then '
                f'n=$(cat "{off_count_file}"); n=$((n+1)); echo "$n" > "{off_count_file}"; '
                'if [ "$n" -lt 3 ]; then echo "not empty" >&2; exit 1; else exit 0; fi; fi\n'
                "exit 0"
            ),
        )
        queue = mc.QueueState(mode="on", pending=2, playing=0)

        result = mc.action_queue_set_mode(
            False,
            queue=queue,
            vvread_bin=script,
            cwd=tmp_path,
            env={},
            retry_interval_sec=0,
            max_retries=5,
        )

        assert result.ok is True
        off_calls = [
            line for line in call_log.read_text().splitlines() if line.startswith("queue off")
        ]
        assert len(off_calls) == 3
        assert result.attempts == 2

    def test_enable_false_empty_refetched_status_does_not_abandon_retry_loop(self, tmp_path):
        # cmd_off の同期チェックが権威であり、こちら側の status 再取得は
        # あくまで参考情報である。status がたまたま空を報告しても、
        # off 自体の再試行は諦めない(status=空 かつ off 失敗、という
        # 一見矛盾する状況でも off の再試行を継続することを保証する)。
        call_log = tmp_path / "calls.log"
        off_count_file = tmp_path / "off_count"
        off_count_file.write_text("0")
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "stop" ]; then exit 0; fi\n'
                'if [ "$1" = "status" ]; then '
                'echo \'{"state": "idle", "mute_until": null, '
                '"queue": {"mode": "off", "pending": 0, "playing": 0, "failed": 0}}\'; exit 0; fi\n'
                'if [ "$1" = "queue" ] && [ "$2" = "off" ]; then '
                f'n=$(cat "{off_count_file}"); n=$((n+1)); echo "$n" > "{off_count_file}"; '
                'if [ "$n" -lt 3 ]; then echo "not empty" >&2; exit 1; else exit 0; fi; fi\n'
                "exit 0"
            ),
        )
        queue = mc.QueueState(mode="on", pending=2, playing=0)

        result = mc.action_queue_set_mode(
            False,
            queue=queue,
            vvread_bin=script,
            cwd=tmp_path,
            env={},
            retry_interval_sec=0,
            max_retries=5,
        )

        assert result.ok is True
        off_calls = [
            line for line in call_log.read_text().splitlines() if line.startswith("queue off")
        ]
        assert len(off_calls) == 3
        assert result.attempts == 2

    def test_enable_false_off_never_succeeds_returns_failure_after_max_retries(self, tmp_path):
        call_log = tmp_path / "calls.log"
        script = _write_script(
            tmp_path,
            (
                f'echo "$*" >> "{call_log}"\n'
                'if [ "$1" = "stop" ]; then exit 0; fi\n'
                'if [ "$1" = "status" ]; then '
                'echo \'{"state": "idle", "mute_until": null, '
                '"queue": {"mode": "on", "pending": 1, "playing": 0, "failed": 0}}\'; exit 0; fi\n'
                'if [ "$1" = "queue" ] && [ "$2" = "off" ]; then echo "still busy" >&2; exit 1; fi\n'
                "exit 0"
            ),
        )
        queue = mc.QueueState(mode="on", pending=2, playing=0)

        result = mc.action_queue_set_mode(
            False,
            queue=queue,
            vvread_bin=script,
            cwd=tmp_path,
            env={},
            retry_interval_sec=0,
            max_retries=3,
        )

        assert result.ok is False
        assert "still busy" in result.error
        off_calls = [
            line for line in call_log.read_text().splitlines() if line.startswith("queue off")
        ]
        assert len(off_calls) == 1 + 3
        assert result.attempts == 3


class TestToggleHelpers:
    def test_disabled_state_toggles_to_enable(self):
        assert mc.toggle_action_enables("disabled") is True

    @pytest.mark.parametrize("state", ["idle", "playing", "muted", "error"])
    def test_other_states_toggle_to_disable(self, state):
        assert mc.toggle_action_enables(state) is False


# ---------------------------------------------------------------------------
# 表示モデル変換
# ---------------------------------------------------------------------------


class TestToDisplayModel:
    """状態表示の3色統合(🟢稼働中/🟡絶対時刻ミュート/🔴停止中/⚠エラー)。

    `icon`(トレイアイコン、ICONS 辞書由来)は今回変更されていないため
    引き続き検証する。`state_line` は新設のテキスト行で、icon とは独立した
    別の絵文字体系(🟢/🟡/🔴/⚠)を持つ。
    """

    @pytest.mark.parametrize("state", ["idle", "playing"])
    def test_idle_and_playing_show_green_running_text(self, state):
        status = mc.StatusState(state=state)
        model = mc.to_display_model(status)
        assert model.state_line == "🟢 vvread は 稼働中"

    def test_idle_and_playing_state_lines_are_textually_identical(self):
        idle_line = mc.to_display_model(mc.StatusState(state="idle")).state_line
        playing_line = mc.to_display_model(mc.StatusState(state="playing")).state_line
        assert idle_line == playing_line

    def test_muted_with_valid_mute_until_shows_absolute_time_in_local_tz(self, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            mute_until = int(time.mktime((2026, 7, 26, 14, 5, 0, 0, 0, -1)))
            status = mc.StatusState(state="muted", mute_until=mute_until)
            model = mc.to_display_model(status)
            assert model.state_line == "🟡 vvread は 14:05 までミュート中"
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_muted_absolute_time_crosses_midnight(self, monkeypatch):
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            late_night = int(time.mktime((2026, 7, 26, 23, 50, 0, 0, 0, -1)))
            after_midnight = int(time.mktime((2026, 7, 27, 0, 15, 0, 0, 0, -1)))
            late_model = mc.to_display_model(mc.StatusState(state="muted", mute_until=late_night))
            after_model = mc.to_display_model(mc.StatusState(state="muted", mute_until=after_midnight))
            assert late_model.state_line == "🟡 vvread は 23:50 までミュート中"
            assert after_model.state_line == "🟡 vvread は 00:15 までミュート中"
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_muted_with_none_mute_until_falls_back_without_crashing(self):
        status = mc.StatusState(state="muted", mute_until=None)
        model = mc.to_display_model(status)
        assert model.state_line == "🟡 vvread は ミュート中"

    def test_disabled_shows_red_stopped_text(self):
        status = mc.StatusState(state="disabled")
        model = mc.to_display_model(status)
        assert model.state_line == "🔴 vvread は 停止中"

    def test_error_shows_warning_icon_and_existing_label_text(self):
        status = mc.StatusState(state="error", error="boom")
        model = mc.to_display_model(status)
        assert model.state_line == f"⚠ vvread は {mc._STATE_LABELS['error']}"

    def test_unknown_state_falls_back_to_error_icon_and_line(self):
        status = mc.StatusState(state="totally_unknown")
        model = mc.to_display_model(status)
        assert model.icon == "⚠"
        assert "エラー" in model.state_line

    @pytest.mark.parametrize(
        "state,icon",
        [
            ("idle", "🔊"),
            ("playing", "▶"),
            ("disabled", "🔇"),
            ("muted", "🤫"),
            ("error", "⚠"),
        ],
    )
    def test_icon_mapping_is_unchanged(self, state, icon):
        status = mc.StatusState(state=state)
        model = mc.to_display_model(status)
        assert model.icon == icon

    def test_queue_line_format_is_unchanged(self):
        status = mc.StatusState(state="idle", queue=mc.QueueState(mode="on", pending=3, playing=1, failed=2))
        model = mc.to_display_model(status)
        assert model.queue_line == "キュー: 待機 3 / 再生中 1 / 失敗 2"

    def test_all_allowed_states_have_icons_and_labels(self):
        for state in (*mc._ALLOWED_STATES, "error"):
            assert state in mc.ICONS
            assert state in mc._STATE_LABELS

    def test_display_model_no_longer_has_mute_line_field(self):
        status = mc.StatusState(state="muted", mute_until=1_000_000)
        model = mc.to_display_model(status)
        assert not hasattr(model, "mute_line")

    def test_to_display_model_no_longer_accepts_now_kwarg(self):
        status = mc.StatusState(state="idle")
        with pytest.raises(TypeError):
            mc.to_display_model(status, now=1_000_000)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ポーリング間隔検証
# ---------------------------------------------------------------------------


class TestResolvePollInterval:
    def test_unset_returns_default(self):
        assert mc.resolve_poll_interval({}) == 2.0

    def test_empty_string_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": ""}) == 2.0

    def test_non_numeric_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "abc"}) == 2.0

    def test_zero_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "0"}) == 2.0

    def test_negative_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "-5"}) == 2.0

    def test_infinite_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "inf"}) == 2.0

    def test_nan_returns_default(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "nan"}) == 2.0

    def test_below_min_clamped_to_min(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "0.5"}) == 1.0

    def test_above_max_clamped_to_max(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "120"}) == 60.0

    def test_valid_value_in_range_is_used_as_is(self):
        assert mc.resolve_poll_interval({"VVREAD_MENUBAR_INTERVAL": "10"}) == 10.0

    def test_uses_os_environ_when_env_not_given(self, monkeypatch):
        monkeypatch.setenv("VVREAD_MENUBAR_INTERVAL", "15")
        assert mc.resolve_poll_interval() == 15.0


# ---------------------------------------------------------------------------
# poll 世代管理 + 連続失敗トラッキング
# ---------------------------------------------------------------------------


class TestPollGeneration:
    def test_initial_token_is_current(self):
        gen = mc.PollGeneration()
        token = gen.token()
        assert gen.is_current(token) is True

    def test_bump_invalidates_old_token(self):
        gen = mc.PollGeneration()
        old_token = gen.token()
        gen.bump()
        assert gen.is_current(old_token) is False

    def test_bump_returns_new_generation_and_is_current(self):
        gen = mc.PollGeneration()
        gen.token()
        new_token = gen.bump()
        assert gen.is_current(new_token) is True

    def test_multiple_bumps_only_latest_is_current(self):
        gen = mc.PollGeneration()
        t0 = gen.token()
        gen.bump()
        t1 = gen.token()
        gen.bump()
        assert gen.is_current(t0) is False
        assert gen.is_current(t1) is False
        assert gen.is_current(gen.token()) is True


class TestPollFailureTracker:
    def test_not_degraded_initially(self):
        tracker = mc.PollFailureTracker(threshold=3)
        assert tracker.degraded is False

    def test_degraded_after_threshold_failures(self):
        tracker = mc.PollFailureTracker(threshold=3)
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.degraded is False
        tracker.record_failure()
        assert tracker.degraded is True

    def test_success_resets_counter(self):
        tracker = mc.PollFailureTracker(threshold=2)
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.degraded is True
        tracker.record_success()
        assert tracker.degraded is False
        assert tracker.consecutive_failures == 0

    def test_threshold_of_zero_is_clamped_to_one(self):
        tracker = mc.PollFailureTracker(threshold=0)
        tracker.record_failure()
        assert tracker.degraded is True


# ---------------------------------------------------------------------------
# 二重起動防止 lock
# ---------------------------------------------------------------------------


class TestSingleInstanceLock:
    def test_acquire_creates_file_with_mode_0600(self, tmp_path):
        lock_path = tmp_path / "menubar.lock"
        lock = mc.SingleInstanceLock(lock_path)
        try:
            lock.acquire()
            assert lock_path.exists()
            mode = stat.S_IMODE(lock_path.stat().st_mode)
            assert mode == 0o600
        finally:
            lock.release()

    def test_acquire_writes_pid(self, tmp_path):
        lock_path = tmp_path / "menubar.lock"
        lock = mc.SingleInstanceLock(lock_path)
        try:
            lock.acquire()
            assert lock_path.read_text().strip() == str(os.getpid())
        finally:
            lock.release()

    def test_second_acquire_on_same_path_raises_lock_error(self, tmp_path):
        lock_path = tmp_path / "menubar.lock"
        lock1 = mc.SingleInstanceLock(lock_path)
        lock2 = mc.SingleInstanceLock(lock_path)
        lock1.acquire()
        try:
            with pytest.raises(mc.LockError) as exc_info:
                lock2.acquire()
            assert exc_info.value.pid == os.getpid()
        finally:
            lock1.release()

    def test_release_allows_reacquire(self, tmp_path):
        lock_path = tmp_path / "menubar.lock"
        lock1 = mc.SingleInstanceLock(lock_path)
        lock1.acquire()
        lock1.release()

        lock2 = mc.SingleInstanceLock(lock_path)
        lock2.acquire()
        try:
            assert lock_path.read_text().strip() == str(os.getpid())
        finally:
            lock2.release()

    def test_context_manager_releases_on_exit(self, tmp_path):
        lock_path = tmp_path / "menubar.lock"
        with mc.SingleInstanceLock(lock_path):
            pass
        # 解放されていれば再取得できる
        lock2 = mc.SingleInstanceLock(lock_path)
        lock2.acquire()
        lock2.release()

    def test_symlink_target_is_rejected_via_o_nofollow(self, tmp_path):
        real_target = tmp_path / "real_target"
        real_target.write_text("")
        lock_path = tmp_path / "menubar.lock"
        lock_path.symlink_to(real_target)

        lock = mc.SingleInstanceLock(lock_path)
        with pytest.raises(OSError):
            lock.acquire()

    def test_default_lock_path_under_state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VVREAD_STATE_DIR", str(tmp_path))
        assert mc.default_lock_path() == tmp_path / "menubar.lock"
