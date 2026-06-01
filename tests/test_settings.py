"""scripts/settings.py のテスト (R-025)

R-025 の最小実装範囲を網羅:
- cascade priority(env > project > user > default)各レイヤー
- JSONC line comment 対応 + string 内の `//` を保護
- 不正 JSON / 型変換失敗 → parse_errors 蓄積 + default fallback
- 不明キー → unknown_keys 蓄積、known キー解決は止まらない(forward compat)
- dot path get + --with-origin JSON 出力
- list --json 出力(values / unknown_keys / parse_errors / sources)
- ファイル不在は parse_errors に上げない(normal 経路)

注意: settings.load() は user_path / project_path 注入 + env 注入で
完全に独立な世界を作れる設計。テスト中はその注入を使い、本物の
~/Library/... は触らない。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import settings as settings_module  # noqa: E402

SCRIPT = REPO / "scripts" / "settings.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> tuple[Path, Path]:
    """user / project の settings.json パスを返す(ファイルはまだ作らない)"""
    user = tmp_path / "user" / "settings.json"
    project = tmp_path / "proj" / "vvread.settings.json"
    user.parent.mkdir(parents=True, exist_ok=True)
    project.parent.mkdir(parents=True, exist_ok=True)
    return user, project


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _load(tmp_path: Path, *, env=None, user_data=None, project_data=None,
          user_text=None, project_text=None):
    """テスト用 load() ラッパー。

    *_data : dict を JSON 化して書く
    *_text : 生テキストを直接書く(JSONC / 不正 JSON テスト用)
    env    : OS env 完全注入(継承させない)
    """
    user_path, project_path = _make_paths(tmp_path)
    if user_data is not None:
        _write_json(user_path, user_data)
    elif user_text is not None:
        user_path.write_text(user_text, encoding="utf-8")
    if project_data is not None:
        _write_json(project_path, project_data)
    elif project_text is not None:
        project_path.write_text(project_text, encoding="utf-8")
    return settings_module.load(
        cwd=tmp_path,
        env=env if env is not None else {},
        user_path=user_path,
        project_path=project_path,
    )


# ---------------------------------------------------------------------------
# default 解決
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_all_schema_keys_resolve_to_defaults_when_no_inputs(self, tmp_path):
        s = _load(tmp_path)
        # SCHEMA の全キーが値を持つ
        assert set(s.values.keys()) == set(settings_module.SCHEMA.keys())
        for k, rv in s.values.items():
            # voicevox.engines は post-processing で engineUrl から派生するため "derived"
            if k == "voicevox.engines":
                assert rv.origin.source == "derived", (
                    f"{k} should be derived, got {rv.origin.source}"
                )
            else:
                assert rv.origin.source == "default", (
                    f"{k} should be default, got {rv.origin.source}"
                )

    def test_engine_url_default_is_127_not_localhost(self, tmp_path):
        """R-025 backlog 確定事項: localhost は IPv6 binding で詰まる事例があり
        127.0.0.1 を default にする"""
        s = _load(tmp_path)
        assert s.get("voicevox.engineUrl").value == "http://127.0.0.1:50021"

    def test_no_parse_errors_no_unknown_keys_no_sources(self, tmp_path):
        s = _load(tmp_path)
        assert s.parse_errors == []
        assert s.unknown_keys == []
        assert s.sources == []


# ---------------------------------------------------------------------------
# env override
# ---------------------------------------------------------------------------


class TestEnvOverride:
    def test_env_overrides_default(self, tmp_path):
        s = _load(tmp_path,
                  env={"VOICEVOX_SPEAKER": "11"})
        rv = s.get("voicevox.speaker")
        assert rv.value == 11
        assert rv.origin.source == "env"
        assert rv.origin.detail == "VOICEVOX_SPEAKER"

    def test_env_string_to_float(self, tmp_path):
        s = _load(tmp_path, env={"VOICEVOX_SPEED": "1.7"})
        rv = s.get("voicevox.speed")
        assert rv.value == pytest.approx(1.7)
        assert rv.origin.source == "env"

    def test_env_overrides_project_and_user(self, tmp_path):
        s = _load(
            tmp_path,
            env={"VOICEVOX_SPEAKER": "99"},
            user_data={"voicevox": {"speaker": 1}},
            project_data={"voicevox": {"speaker": 2}},
        )
        rv = s.get("voicevox.speaker")
        assert rv.value == 99
        assert rv.origin.source == "env"

    def test_env_with_invalid_value_falls_back_with_parse_error(self, tmp_path):
        s = _load(tmp_path,
                  env={"VOICEVOX_SPEAKER": "not_an_int"})
        # default に fallback
        rv = s.get("voicevox.speaker")
        assert rv.value == 3
        assert rv.origin.source == "default"
        # parse_errors に env 名が記録される
        assert any("VOICEVOX_SPEAKER" == src for src, _ in s.parse_errors)


# ---------------------------------------------------------------------------
# project / user cascade
# ---------------------------------------------------------------------------


class TestProjectUserCascade:
    def test_user_only_applied(self, tmp_path):
        s = _load(tmp_path, user_data={"voicevox": {"speaker": 7}})
        rv = s.get("voicevox.speaker")
        assert rv.value == 7
        assert rv.origin.source == "user"

    def test_project_only_applied(self, tmp_path):
        s = _load(tmp_path, project_data={"voicevox": {"speaker": 8}})
        rv = s.get("voicevox.speaker")
        assert rv.value == 8
        assert rv.origin.source == "project"

    def test_project_wins_over_user(self, tmp_path):
        s = _load(
            tmp_path,
            user_data={"voicevox": {"speaker": 1}},
            project_data={"voicevox": {"speaker": 2}},
        )
        rv = s.get("voicevox.speaker")
        assert rv.value == 2
        assert rv.origin.source == "project"

    def test_partial_override_falls_back_per_key(self, tmp_path):
        """user で speed のみ、project で speaker のみ設定 → 各々の出所が違う"""
        s = _load(
            tmp_path,
            user_data={"voicevox": {"speed": 1.2}},
            project_data={"voicevox": {"speaker": 5}},
        )
        assert s.get("voicevox.speed").origin.source == "user"
        assert s.get("voicevox.speaker").origin.source == "project"
        # それ以外は default
        assert s.get("voicevox.pitch").origin.source == "default"

    def test_sources_list_records_loaded_files(self, tmp_path):
        s = _load(
            tmp_path,
            user_data={"voicevox": {"speaker": 1}},
            project_data={"voicevox": {"speaker": 2}},
        )
        # 両ファイル絶対パスが sources に入る
        user_path, project_path = _make_paths(tmp_path)
        assert str(user_path) in s.sources
        assert str(project_path) in s.sources


# ---------------------------------------------------------------------------
# JSONC
# ---------------------------------------------------------------------------


class TestJsonc:
    def test_line_comment_at_start_of_line(self, tmp_path):
        text = """{
  // 速度を上げる
  "voicevox": { "speed": 1.8 }
}"""
        s = _load(tmp_path, project_text=text)
        assert s.get("voicevox.speed").value == pytest.approx(1.8)
        assert s.get("voicevox.speed").origin.source == "project"
        assert s.parse_errors == []

    def test_trailing_line_comment(self, tmp_path):
        text = """{
  "voicevox": {
    "speaker": 11  // 春日部つむぎ
  }
}"""
        s = _load(tmp_path, project_text=text)
        assert s.get("voicevox.speaker").value == 11
        assert s.parse_errors == []

    def test_double_slash_inside_string_preserved(self, tmp_path):
        """文字列リテラル内の `//` はコメントと誤検出しない(URL 等)"""
        text = """{
  "voicevox": { "engineUrl": "http://127.0.0.1:50021" }
  // 上記がデフォルト
}"""
        s = _load(tmp_path, project_text=text)
        assert s.get("voicevox.engineUrl").value == "http://127.0.0.1:50021"
        assert s.parse_errors == []

    def test_escaped_quote_in_string(self, tmp_path):
        """エスケープされた quote で string が早期終了しない"""
        text = r'''{
  "voicevox": { "engineUrl": "http://example.com/\"path\"//x" }
}'''
        s = _load(tmp_path, project_text=text)
        # エスケープ quote 経由で string を抜けない → // は string 内なので保持
        assert s.get("voicevox.engineUrl").value == 'http://example.com/"path"//x'


# ---------------------------------------------------------------------------
# 不正 JSON / 型不一致
# ---------------------------------------------------------------------------


class TestErrors:
    def test_invalid_json_records_parse_error_and_keeps_defaults(self, tmp_path):
        s = _load(tmp_path, project_text="{ not valid")
        assert any("invalid JSON" in m for _, m in s.parse_errors)
        # default は健在
        assert s.get("voicevox.speaker").value == 3
        assert s.get("voicevox.speaker").origin.source == "default"

    def test_top_level_array_records_error(self, tmp_path):
        s = _load(tmp_path, project_text="[1, 2, 3]")
        assert any("top-level must be an object" in m
                   for _, m in s.parse_errors)

    def test_type_mismatch_falls_back_to_next_layer(self, tmp_path):
        """project の値が型変換不能 → user に fallback、parse_errors に記録"""
        s = _load(
            tmp_path,
            user_data={"voicevox": {"speaker": 5}},
            project_data={"voicevox": {"speaker": "not_an_int"}},
        )
        # project が拒否され user が採用される
        rv = s.get("voicevox.speaker")
        assert rv.value == 5
        assert rv.origin.source == "user"
        # parse_errors に project の問題が記録
        assert any("voicevox.speaker" in m for _, m in s.parse_errors)

    def test_user_unreadable_file_silent_when_missing(self, tmp_path):
        """ファイル不在は parse_errors には上げない"""
        # user_path / project_path を完全 fresh(_load の helper を使わない)
        user_path = tmp_path / "user_no.json"
        project_path = tmp_path / "proj_no.json"
        s = settings_module.load(
            cwd=tmp_path, env={},
            user_path=user_path, project_path=project_path,
        )
        assert s.parse_errors == []
        assert s.sources == []

    def test_int_type_does_not_accept_bool(self, tmp_path):
        """target=int で value=True は int(True)=1 にしない"""
        s = _load(tmp_path,
                  project_data={"voicevox": {"speaker": True}})
        rv = s.get("voicevox.speaker")
        assert rv.value == 3  # default
        assert rv.origin.source == "default"


# ---------------------------------------------------------------------------
# 不明キー(forward compat)
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_unknown_key_collected_known_keys_unaffected(self, tmp_path):
        s = _load(
            tmp_path,
            project_data={
                "voicevox": {"speaker": 5},
                "future": {"newFeature": "x"},
            },
        )
        # known キーは正常解決
        assert s.get("voicevox.speaker").value == 5
        assert s.get("voicevox.speaker").origin.source == "project"
        # unknown_keys に future.newFeature が記録される
        assert any(k == "future.newFeature" for _, k in s.unknown_keys)

    def test_unknown_keys_from_both_files(self, tmp_path):
        s = _load(
            tmp_path,
            user_data={"weird": {"a": 1}},
            project_data={"weird": {"b": 2}},
        )
        keys = [k for _, k in s.unknown_keys]
        assert "weird.a" in keys
        assert "weird.b" in keys


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(*args, env=None, cwd=None):
    """settings.py を subprocess 起動。env は完全置換(VOICEVOX_*/VVREAD_* を継承させない)。
    cwd を省略すると /tmp に落ちる — リポジトリルートに vvread.settings.json があっても
    デフォルト値テストが壊れないよう隔離する。
    """
    base = {k: v for k, v in os.environ.items()
            if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
    if env:
        base.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=base,
        capture_output=True,
        text=True,
        cwd=cwd if cwd is not None else tempfile.gettempdir(),
    )


class TestCli:
    def test_get_plain(self, tmp_path):
        r = _run_cli("get", "voicevox.engineUrl", cwd=str(tmp_path))
        assert r.returncode == 0
        assert r.stdout.strip() == "http://127.0.0.1:50021"

    def test_get_with_origin_json(self, tmp_path):
        r = _run_cli("get", "voicevox.engineUrl", "--with-origin", cwd=str(tmp_path))
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert payload["value"] == "http://127.0.0.1:50021"
        # engines が user/project settings に設定されていれば "derived"、なければ "default"
        assert payload["origin"] in ("default", "derived", "user", "project")

    def test_get_env_overrides_default_via_cli(self, tmp_path):
        r = _run_cli("get", "voicevox.speaker", "--with-origin",
                     env={"VOICEVOX_SPEAKER": "8"}, cwd=str(tmp_path))
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert payload["value"] == 8
        assert payload["origin"] == "env"
        assert payload["detail"] == "VOICEVOX_SPEAKER"

    def test_get_unknown_key_exits_1(self, tmp_path):
        r = _run_cli("get", "no.such.key", cwd=str(tmp_path))
        assert r.returncode == 1
        assert "unknown key" in r.stderr

    def test_list_plain(self, tmp_path):
        r = _run_cli("list", cwd=str(tmp_path))
        assert r.returncode == 0
        # 各 SCHEMA キーが列挙される
        for key in settings_module.SCHEMA.keys():
            assert key in r.stdout

    def test_list_json(self, tmp_path):
        r = _run_cli("list", "--json", cwd=str(tmp_path))
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert "values" in payload
        assert "unknown_keys" in payload
        assert "parse_errors" in payload
        assert "sources" in payload
        # 全 SCHEMA キーが values に含まれる
        for key in settings_module.SCHEMA.keys():
            assert key in payload["values"]
            # origin は user/project settings の有無で変わるためロバストに検証
            assert payload["values"][key]["origin"] in (
                "default", "derived", "user", "project", "env"
            )


class TestCliEnv:
    def test_env_emits_all_schema_vars(self, tmp_path):
        """env サブコマンドが SCHEMA の全 env_var を出力する"""
        r = _run_cli("env", cwd=str(tmp_path))
        assert r.returncode == 0
        for key, (default, env_var, _) in settings_module.SCHEMA.items():
            if env_var:
                assert env_var in r.stdout, f"{env_var} not found in env output"

    def test_env_uses_project_settings(self, tmp_path):
        """cwd の vvread.settings.json が反映される"""
        proj = tmp_path / "vvread.settings.json"
        proj.write_text('{"voicevox": {"speaker": 77}}', encoding="utf-8")
        r = _run_cli("env", cwd=str(tmp_path))
        assert r.returncode == 0
        assert "VOICEVOX_SPEAKER='77'" in r.stdout

    def test_env_env_overrides_project(self, tmp_path):
        """env 変数が project settings より優先される"""
        proj = tmp_path / "vvread.settings.json"
        proj.write_text('{"voicevox": {"speaker": 77}}', encoding="utf-8")
        r = _run_cli("env", env={"VOICEVOX_SPEAKER": "99"}, cwd=str(tmp_path))
        assert r.returncode == 0
        assert "VOICEVOX_SPEAKER='99'" in r.stdout

    def test_env_legacy_voicevox_engine_fallback(self, tmp_path):
        """VOICEVOX_ENGINE (legacy) が VOICEVOX_ENGINE_URL として出力される (S-008)"""
        r = _run_cli("env", env={"VOICEVOX_ENGINE": "http://192.168.1.1:50021"}, cwd=str(tmp_path))
        assert r.returncode == 0
        assert "VOICEVOX_ENGINE_URL='http://192.168.1.1:50021'" in r.stdout

    def test_env_output_is_eval_safe(self, tmp_path):
        """shlex.quote で値がクォートされ eval できる"""
        r = _run_cli("env", env={"VOICEVOX_ENGINE_URL": "http://127.0.0.1:50021"}, cwd=str(tmp_path))
        assert r.returncode == 0
        assert "VOICEVOX_ENGINE_URL='http://127.0.0.1:50021'" in r.stdout

    def test_env_output_has_export_prefix(self, tmp_path):
        """env サブコマンドの全出力行が 'export ' で始まる(R-037)"""
        r = _run_cli("env", cwd=str(tmp_path))
        assert r.returncode == 0
        for line in r.stdout.strip().splitlines():
            assert line.startswith("export "), f"missing export prefix: {line!r}"

    def test_env_vars_visible_to_subprocess(self, tmp_path):
        """eval した変数が子プロセスの os.environ に伝播する(R-037 end-to-end)"""
        proj = tmp_path / "vvread.settings.json"
        proj.write_text('{"voicevox": {"maxChars": 123}}', encoding="utf-8")
        base = {k: v for k, v in os.environ.items()
                if not (k.startswith("VOICEVOX_") or k.startswith("VVREAD_"))}
        script = (
            f'eval "$({sys.executable} {SCRIPT} env)"; '
            "printenv VOICEVOX_MAX_CHARS"
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           cwd=str(tmp_path), env=base)
        assert r.returncode == 0
        assert r.stdout.strip() == "123"


class TestChunkingSchema:
    """R-036: settings.json に追加した chunking 系 3 キーのテスト"""

    def test_chunking_defaults(self, tmp_path):
        s = _load(tmp_path)
        assert s.get("voicevox.chunkChars").value == 200
        assert s.get("voicevox.chunkHardMax").value == 400
        assert s.get("voicevox.inlineCodeLimit").value == 25

    def test_chunking_project_override(self, tmp_path):
        s = _load(tmp_path, project_data={
            "voicevox": {"chunkChars": 150, "chunkHardMax": 300, "inlineCodeLimit": 20}
        })
        assert s.get("voicevox.chunkChars").value == 150
        assert s.get("voicevox.chunkHardMax").value == 300
        assert s.get("voicevox.inlineCodeLimit").value == 20

    def test_chunking_env_override(self, tmp_path):
        s = _load(tmp_path, env={
            "VOICEVOX_CHUNK_CHARS": "100",
            "VOICEVOX_CHUNK_HARD_MAX": "250",
            "VOICEVOX_INLINE_CODE_LIMIT": "10",
        })
        assert s.get("voicevox.chunkChars").value == 100
        assert s.get("voicevox.chunkHardMax").value == 250
        assert s.get("voicevox.inlineCodeLimit").value == 10

    def test_chunking_env_in_env_output(self):
        """settings.py env が 3 つの新キーを出力する"""
        r = _run_cli("env")
        assert r.returncode == 0
        assert "VOICEVOX_CHUNK_CHARS=" in r.stdout
        assert "VOICEVOX_CHUNK_HARD_MAX=" in r.stdout
        assert "VOICEVOX_INLINE_CODE_LIMIT=" in r.stdout


# ---------------------------------------------------------------------------
# voicevox.engines (B-122)
# ---------------------------------------------------------------------------


class TestEngines:
    def test_engines_derived_from_engine_url_when_unset(self, tmp_path):
        """engines 未設定 → engineUrl から 1 要素配列として派生。origin=derived"""
        s = _load(tmp_path)
        rv = s.get("voicevox.engines")
        assert rv is not None
        assert rv.value == ["http://127.0.0.1:50021"]
        assert rv.origin.source == "derived"
        assert rv.origin.detail == "voicevox.engineUrl"

    def test_engines_from_project_settings(self, tmp_path):
        """project settings に engines 配列 → リストとして解決"""
        s = _load(tmp_path, project_data={
            "voicevox": {"engines": ["http://127.0.0.1:50021", "http://127.0.0.1:50022"]}
        })
        rv = s.get("voicevox.engines")
        assert rv.value == ["http://127.0.0.1:50021", "http://127.0.0.1:50022"]
        assert rv.origin.source == "project"

    def test_engines_from_env_semicolon_split(self, tmp_path):
        """env VOICEVOX_ENGINES='http://a;http://b' → 2 要素リスト"""
        s = _load(tmp_path, env={
            "VOICEVOX_ENGINES": "http://127.0.0.1:50021;http://127.0.0.1:50022"
        })
        rv = s.get("voicevox.engines")
        assert rv.value == ["http://127.0.0.1:50021", "http://127.0.0.1:50022"]
        assert rv.origin.source == "env"

    def test_engines_validation_removes_empty_and_duplicates(self, tmp_path):
        """空文字・重複・trailing slash の除外 + parse_errors への警告積み"""
        s = _load(tmp_path, project_data={
            "voicevox": {"engines": [
                "http://127.0.0.1:50021/",  # trailing slash → 除去
                "",                          # 空文字 → 除外 + warning
                "http://127.0.0.1:50022",
                "http://127.0.0.1:50021",   # 重複 → 除去
            ]}
        })
        rv = s.get("voicevox.engines")
        assert rv.value == ["http://127.0.0.1:50021", "http://127.0.0.1:50022"]
        # 空文字の除外で警告が積まれる
        assert any("voicevox.engines" in msg for _, msg in s.parse_errors)

    def test_engines_env_export_semicolon_separated(self):
        """settings.py env が VOICEVOX_ENGINES を ; 区切りで出力する"""
        r = _run_cli("env", env={"VOICEVOX_ENGINES": "http://127.0.0.1:50021;http://127.0.0.1:50022"})
        assert r.returncode == 0
        assert "VOICEVOX_ENGINES='http://127.0.0.1:50021;http://127.0.0.1:50022'" in r.stdout


# ---------------------------------------------------------------------------
# U-117: normalize_engines / canonicalize_settings_dict
# ---------------------------------------------------------------------------


class TestNormalizeEngines:
    def test_trailing_slash_removed(self):
        normalized, errors = settings_module.normalize_engines(["http://127.0.0.1:50021/"])
        assert normalized == ["http://127.0.0.1:50021"]
        assert errors == []

    def test_duplicates_removed_order_preserved(self):
        urls = ["http://a:50021", "http://b:50022", "http://a:50021"]
        normalized, errors = settings_module.normalize_engines(urls)
        assert normalized == ["http://a:50021", "http://b:50022"]
        assert errors == []

    def test_invalid_scheme_excluded(self):
        normalized, errors = settings_module.normalize_engines(["ftp://127.0.0.1:50021"])
        assert normalized == []
        assert len(errors) == 1
        assert "ftp://127.0.0.1:50021" in errors[0]

    def test_no_netloc_excluded(self):
        normalized, errors = settings_module.normalize_engines(["http://"])
        assert normalized == []
        assert len(errors) == 1

    def test_empty_string_excluded(self):
        normalized, errors = settings_module.normalize_engines(["", "http://127.0.0.1:50021"])
        assert normalized == ["http://127.0.0.1:50021"]
        assert len(errors) == 1

    def test_non_string_excluded(self):
        normalized, errors = settings_module.normalize_engines([123, "http://127.0.0.1:50021"])
        assert normalized == ["http://127.0.0.1:50021"]
        assert len(errors) == 1

    def test_https_allowed(self):
        normalized, errors = settings_module.normalize_engines(["https://example.com:50021"])
        assert normalized == ["https://example.com:50021"]
        assert errors == []

    def test_partial_invalid_keeps_valid(self):
        normalized, errors = settings_module.normalize_engines([
            "ftp://bad.example",
            "http://127.0.0.1:50021",
        ])
        assert normalized == ["http://127.0.0.1:50021"]
        assert len(errors) == 1


class TestCanonicalizeSettingsDict:
    def test_engine_url_only_converts_to_engines(self):
        data = {"voicevox": {"engineUrl": "http://127.0.0.1:50021"}}
        result = settings_module.canonicalize_settings_dict(data)
        assert result["voicevox"]["engines"] == ["http://127.0.0.1:50021"]
        assert "engineUrl" not in result["voicevox"]

    def test_engines_wins_over_engine_url(self):
        data = {"voicevox": {
            "engineUrl": "http://old:50021",
            "engines": ["http://new:50021", "http://new2:50022"],
        }}
        result = settings_module.canonicalize_settings_dict(data)
        assert result["voicevox"]["engines"] == ["http://new:50021", "http://new2:50022"]
        assert "engineUrl" not in result["voicevox"]

    def test_engines_only_unchanged(self):
        data = {"voicevox": {"engines": ["http://127.0.0.1:50021"]}}
        result = settings_module.canonicalize_settings_dict(data)
        assert result["voicevox"]["engines"] == ["http://127.0.0.1:50021"]
        assert "engineUrl" not in result["voicevox"]

    def test_neither_key_no_change(self):
        data = {"voicevox": {"speaker": 3}}
        result = settings_module.canonicalize_settings_dict(data)
        assert result == {"voicevox": {"speaker": 3}}

    def test_engines_empty_list_raises(self):
        import pytest
        data = {"voicevox": {"engines": []}}
        with pytest.raises(ValueError, match="1件以上"):
            settings_module.canonicalize_settings_dict(data)

    def test_engines_invalid_url_raises(self):
        import pytest
        data = {"voicevox": {"engines": ["ftp://bad"]}}
        with pytest.raises(ValueError, match="有効な URL が1件もありません"):
            settings_module.canonicalize_settings_dict(data)

    def test_trailing_slash_normalized(self):
        data = {"voicevox": {"engines": ["http://127.0.0.1:50021/"]}}
        result = settings_module.canonicalize_settings_dict(data)
        assert result["voicevox"]["engines"] == ["http://127.0.0.1:50021"]

    def test_original_data_not_mutated(self):
        data = {"voicevox": {"engineUrl": "http://127.0.0.1:50021", "speaker": 3}}
        original_copy = {"voicevox": {"engineUrl": "http://127.0.0.1:50021", "speaker": 3}}
        settings_module.canonicalize_settings_dict(data)
        assert data == original_copy


class TestEnginesRuntimeExport:
    def test_engines_only_derives_engine_url(self, tmp_path):
        """engines のみ設定 → engineUrl が engines[0] として解決される。"""
        s = _load(tmp_path, project_data={
            "voicevox": {"engines": ["http://127.0.0.1:50022"]}
        })
        rv_engines = s.get("voicevox.engines")
        rv_url = s.get("voicevox.engineUrl")
        assert rv_engines.value == ["http://127.0.0.1:50022"]
        assert rv_url.value == "http://127.0.0.1:50022"
        assert rv_url.origin.source == "derived"

    def test_engine_url_env_not_overridden_by_project_engines(self, tmp_path):
        """VOICEVOX_ENGINE_URL env は project の engines より優先される。"""
        s = _load(tmp_path,
                  env={"VOICEVOX_ENGINE_URL": "http://127.0.0.1:1"},
                  project_data={"voicevox": {"engines": ["http://127.0.0.1:50021"]}})
        rv_url = s.get("voicevox.engineUrl")
        # env から来た engineUrl は engines[0] で上書きされない
        assert rv_url.value == "http://127.0.0.1:1"
        assert rv_url.origin.source == "env"

    def test_engines_and_engine_url_both_set_engines_wins(self, tmp_path):
        """project に engineUrl + engines が両方ある → engines 優先、engineUrl=engines[0]。"""
        s = _load(tmp_path, project_data={
            "voicevox": {
                "engineUrl": "http://old:50021",
                "engines": ["http://new:50021", "http://new2:50022"],
            }
        })
        rv_url = s.get("voicevox.engineUrl")
        rv_engines = s.get("voicevox.engines")
        assert rv_engines.value == ["http://new:50021", "http://new2:50022"]
        assert rv_url.value == "http://new:50021"
        assert rv_url.origin.source == "derived"

    def test_env_engine_url_export_matches_env_value(self):
        """VOICEVOX_ENGINE_URL env 設定時、env の値が VOICEVOX_ENGINE_URL として出力される。"""
        r = _run_cli("env", env={"VOICEVOX_ENGINE_URL": "http://127.0.0.1:50022"})
        assert r.returncode == 0
        assert "VOICEVOX_ENGINE_URL='http://127.0.0.1:50022'" in r.stdout
