"""scripts/parse_transcript.py のテスト (S-002 / R-006 同梱)

Stop hook ペイロードから最後の assistant text を抽出する Python helper。
exit code は常に 0(hook 文脈で fail させない)、エラーは stderr の warning
+ 空 stdout で表現する設計。

カバー範囲:
- 正常系: 単一 / 複数 assistant / content=str / content=list / 末尾が空 entry
- 入力エラー系: 空 stdin / 不正 JSON / object 以外 / transcript_path 欠落
- ファイル系: transcript ファイル不在 / 中途半端な JSONL を含む / 全 entry が user
- I/O 制約: --max-bytes 超過 / --timeout 超過
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "parse_transcript.py"


def run_parse(
    payload: bytes | str,
    *args: str,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess:
    """parse_transcript.py を subprocess で起動。stdin に payload を渡す。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=payload,
        capture_output=True,
        timeout=timeout,
    )


def write_transcript(path: Path, entries: list[dict]) -> Path:
    """JSONL transcript を書き出すヘルパー。"""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def assistant_text_entry(text: str) -> dict:
    """`type=assistant` + content list with type=text のエントリ"""
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def assistant_str_content_entry(text: str) -> dict:
    """`content` が str のエントリ(古い形式)"""
    return {
        "type": "assistant",
        "message": {"content": text},
    }


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


class TestNormalExtraction:
    def test_single_assistant_returns_text(self, tmp_path):
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [assistant_text_entry("こんにちは")],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "こんにちは"

    def test_multiple_assistants_returns_last(self, tmp_path):
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant_text_entry("最初の応答"),
                {"type": "user", "message": {"content": "user msg"}},
                assistant_text_entry("二番目の応答"),
                assistant_text_entry("最後の応答"),
            ],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "最後の応答"

    def test_content_as_string_handled(self, tmp_path):
        # 古い形式: content が直接 string
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [assistant_str_content_entry("文字列直書きテキスト")],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "文字列直書きテキスト"

    def test_content_list_with_mixed_types(self, tmp_path):
        # tool_use や thinking ブロック等が混じる場合、type=text のみ拾う
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [{
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "internal..."},
                        {"type": "text", "text": "ユーザー向けテキスト1"},
                        {"type": "tool_use", "id": "abc", "name": "X"},
                        {"type": "text", "text": "ユーザー向けテキスト2"},
                    ]
                },
            }],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        out = r.stdout.decode("utf-8")
        assert "ユーザー向けテキスト1" in out
        assert "ユーザー向けテキスト2" in out
        assert "internal" not in out

    def test_last_empty_assistant_keeps_previous(self, tmp_path):
        # 末尾の assistant entry が空 text(tool 呼び出しのみ等)→ 直前の有効
        # text を返す(現行 on_stop.sh の挙動を維持)
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant_text_entry("有効な応答"),
                {
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "tool_use", "id": "x", "name": "X"}
                    ]},
                },
            ],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "有効な応答"

    def test_corrupt_jsonl_line_skipped(self, tmp_path):
        # 1 行壊れていても他行は生かす(append-only ログの末尾 truncation 対策)
        path = tmp_path / "t.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(assistant_text_entry("生きている応答")) + "\n")
            f.write("{bogus json line\n")
            f.write(json.dumps({"type": "user", "message": {"content": "u"}}) + "\n")
        payload = json.dumps({"transcript_path": str(path)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "生きている応答"

    def test_no_assistant_entries(self, tmp_path):
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [{"type": "user", "message": {"content": "u1"}}],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout == b""


# ---------------------------------------------------------------------------
# 入力エラー
# ---------------------------------------------------------------------------


class TestInputErrors:
    def test_empty_stdin_silent_exit_0(self):
        r = run_parse(b"")
        assert r.returncode == 0
        assert r.stdout == b""
        # 空入力は warning 不要(明示的 silent)
        assert r.stderr == b""

    def test_invalid_json_warns_and_exits_0(self):
        r = run_parse(b"not_json{garbage")
        assert r.returncode == 0
        assert r.stdout == b""
        assert b"invalid JSON" in r.stderr

    def test_json_array_not_object(self):
        r = run_parse(b"[1, 2, 3]")
        assert r.returncode == 0
        assert r.stdout == b""
        assert b"not a JSON object" in r.stderr

    def test_payload_without_transcript_path(self):
        r = run_parse(json.dumps({"unrelated": "field"}))
        assert r.returncode == 0
        assert r.stdout == b""
        # transcript_path 不在は単に空 stdout、warning 出さない(普通のケース)
        assert r.stderr == b""

    def test_transcript_path_not_string(self):
        r = run_parse(json.dumps({"transcript_path": 123}))
        assert r.returncode == 0
        assert r.stdout == b""

    def test_transcript_file_missing(self, tmp_path):
        payload = json.dumps({"transcript_path": str(tmp_path / "no.jsonl")})
        r = run_parse(payload)
        assert r.returncode == 0
        assert r.stdout == b""
        assert b"transcript not found" in r.stderr


# ---------------------------------------------------------------------------
# I/O 制約
# ---------------------------------------------------------------------------


class TestIoLimits:
    def test_oversize_payload_warns_and_exits_0(self):
        # 200 byte payload, max-bytes=100 → 超過判定
        big_payload = b'{"transcript_path": "' + b"x" * 200 + b'"}'
        r = run_parse(big_payload, "--max-bytes", "100")
        assert r.returncode == 0
        assert r.stdout == b""
        assert b"exceeds max-bytes" in r.stderr

    def test_oversize_does_not_open_transcript(self, tmp_path):
        # 超過時は JSON parse すらしないので transcript の中身に依存しない
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [assistant_text_entry("読まれない")],
        )
        # transcript_path を含む長い payload(100 byte 超え)
        big_payload = json.dumps({
            "transcript_path": str(transcript),
            "padding": "x" * 500,
        }).encode("utf-8")
        r = run_parse(big_payload, "--max-bytes", "100")
        assert r.returncode == 0
        assert r.stdout == b""

    def test_timeout_warns_and_exits_0(self):
        """stdin を閉じずに放置すると --timeout 後に warning + exit 0。

        communicate() は stdin を閉じてしまい select が即 EOF を返すため使わない。
        stdin の write 端は子プロセス起動後に親側で開いたまま放置 →
        script の select が timeout する → script が自前で exit する → wait で回収。
        """
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--timeout", "0.3"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            t0 = time.monotonic()
            # communicate ではなく wait で待つ(stdin を閉じない)
            proc.wait(timeout=5)
            elapsed = time.monotonic() - t0
            out = proc.stdout.read() if proc.stdout else b""
            err = proc.stderr.read() if proc.stderr else b""
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()

        assert proc.returncode == 0
        assert out == b""
        assert b"timed out" in err
        # 0.3s timeout 設定なので 2s 以内には抜けているはず
        assert elapsed < 2.0, f"timeout took too long: {elapsed:.2f}s"

    def test_default_timeout_does_not_fire_for_fast_input(self, tmp_path):
        """入力が即時に来るケースでは default timeout が発火しない(回帰)"""
        transcript = write_transcript(
            tmp_path / "t.jsonl",
            [assistant_text_entry("即時応答")],
        )
        payload = json.dumps({"transcript_path": str(transcript)})
        # default --timeout=10s を使う
        r = run_parse(payload, timeout=5.0)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "即時応答"
        assert b"timed out" not in r.stderr
