#!/usr/bin/env python3
"""scripts/menubar_core.py - vvread menubar のロジック層 (B-151, P2b)

rumps を import しない純粋ロジック層。CLI runner・JSON/TSV パース・表示モデル
変換・ポーリング間隔検証・poll の in-flight/世代管理・speaker DTO 正規化・
二重起動 lock・話者(既定話者)設定変更をすべてここに置き、pytest で完全カバーする。
`scripts/menubar.py`(rumps adapter)はこのモジュールを呼び出すだけの薄い層にする。

CLI 契約(P1 で確定済み):
  - `vvread status --json`:
      {"state": "disabled"|"muted"|"playing"|"idle",
       "mute_until": <epoch int>|null,
       "queue": {"mode": "on"|"off", "pending": int, "playing": int, "failed": int}}
  - `vvread config --list`: TSV(`key\tvalue`)。既定話者は `voicevox.speaker`。
  - `vvread speakers --json`: [{"name": str, "styles": [{"id": int, "name": str}]}]

subprocess 実行規約:
  - 絶対パスの bin/vvread + argv 配列 + shell=False 固定。
  - cwd は $HOME に固定する(menubar がどのディレクトリから起動されても、
    プロジェクト単位の vvread.settings.json を誤って拾わないようにするため)。
  - env は明示的に dict として渡す(暗黙の継承に頼らない)。既定話者の書き込みは
    `--user-setting` を使うが、env 側の VOICEVOX_SPEAKER 等や
    VVREAD_PROJECT_SETTINGS でカスケードが変わる可能性は残るため、
    set_default_speaker() は書き込み直後に config --list を再読して
    反映を検証し、不一致なら警告状態を返す(env を個別に剥がすのではなく
    「実際に有効になった値」を確認する設計)。
"""
from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import paths as _paths

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

STATUS_TIMEOUT_SEC = 1.0
ACTION_TIMEOUT_SEC = 5.0

_ALLOWED_STATES = ("disabled", "muted", "playing", "idle")

ICONS: Dict[str, str] = {
    "idle": "🔊",
    "playing": "▶",
    "disabled": "🔇",
    "muted": "🤫",
    "error": "⚠",
}

_STATE_LABELS: Dict[str, str] = {
    "idle": "待機中",
    "playing": "再生中",
    "disabled": "オフ",
    "muted": "ミュート中",
    "error": "状態取得エラー",
}

# ミュートのサブメニュー選択肢: (表示ラベル, `vvread mute` の duration 引数)
MUTE_DURATIONS: Tuple[Tuple[str, str], ...] = (
    ("5分", "5m"),
    ("30分", "30m"),
    ("1時間", "1h"),
)

DEFAULT_POLL_INTERVAL_SEC = 2.0
MIN_POLL_INTERVAL_SEC = 1.0
MAX_POLL_INTERVAL_SEC = 60.0
POLL_INTERVAL_ENV = "VVREAD_MENUBAR_INTERVAL"

DEFAULT_POLL_FAILURE_THRESHOLD = 3

# speaker / style 名の防御的切り詰め上限(異常に長い名前でメニューが崩れるのを防ぐ)
MAX_NAME_LENGTH = 60

SPEAKER_SETTING_KEY = "voicevox.speaker"
SPEAKER_SCOPE_WARNING = "他スコープの設定が優先されています"

VOLUME_SETTING_KEY = "voicevox.volume"
SPEED_SETTING_KEY = "voicevox.speed"
INTONATION_SETTING_KEY = "voicevox.intonation"
PAUSE_SCALE_SETTING_KEY = "voicevox.pauseScale"
MAX_CHUNKS_SETTING_KEY = "voicevox.maxChunks"


def _tenths_range(start_tenths: int, stop_tenths: int) -> Tuple[float, ...]:
    """整数 tenths(0.1刻みを表す整数)から浮動小数点誤差のない選択肢タプルを生成する。"""
    return tuple(t / 10 for t in range(start_tenths, stop_tenths + 1))


VOLUME_CHOICES: Tuple[float, ...] = _tenths_range(0, 20)  # 0.0〜2.0
SPEED_CHOICES: Tuple[float, ...] = _tenths_range(5, 20)  # 0.5〜2.0(既存 --speed バリデーションと一致)
INTONATION_CHOICES: Tuple[float, ...] = _tenths_range(0, 20)  # 0.0〜2.0
PAUSE_SCALE_CHOICES: Tuple[float, ...] = _tenths_range(0, 20)  # 0.0〜2.0

# maxChunks のサブメニュー選択肢: (表示ラベル, 実際の値)。MUTE_DURATIONS と同じ
# 「表示ラベル, 値」タプルパターン。
MAX_CHUNKS_CHOICES: Tuple[Tuple[str, int], ...] = (
    ("0(無制限)", 0),
    ("1", 1),
    ("2", 2),
    ("3", 3),
    ("5", 5),
    ("10", 10),
    ("20", 20),
)


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------


def default_vvread_bin() -> Path:
    """このファイルの位置から repo 直下の bin/vvread 絶対パスを解決する。"""
    return Path(__file__).resolve().parent.parent / "bin" / "vvread"


@dataclass
class RunResult:
    """`bin/vvread` サブプロセス実行結果。

    ok: returncode == 0 を意味する(timeout / OSError の場合は False)。
    error: timeout / 起動失敗などプロセス自体が完走しなかった理由(該当時のみ)。
    """
    ok: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: Optional[str] = None


def run_vvread(
    argv: Sequence[str],
    *,
    timeout: float,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> RunResult:
    """絶対パスの bin/vvread を shell=False で実行する。

    cwd は既定で $HOME に固定する。env は既定で os.environ のコピーを明示的に
    渡す(呼び出し側が任意に上書き可能)。argv は配列のまま渡し、シェル展開しない。
    """
    binary = vvread_bin if vvread_bin is not None else default_vvread_bin()
    run_cwd = cwd if cwd is not None else Path.home()
    run_env = dict(os.environ) if env is None else dict(env)

    command = [str(binary)] + list(argv)
    try:
        proc = subprocess.run(  # noqa: S603 - argv 配列 + shell=False 固定
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(run_cwd),
            env=run_env,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        return RunResult(
            ok=False,
            timed_out=True,
            error=f"vvread の応答がタイムアウトしました ({timeout}s)",
            stdout=stdout,
            stderr=stderr,
        )
    except OSError as e:
        return RunResult(ok=False, error=f"vvread を起動できませんでした: {e}")

    return RunResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def describe_run_error(result: RunResult) -> str:
    """RunResult から UI 表示用の 1 行エラーメッセージを組み立てる。

    menubar.py(rumps adapter)からもメニュー内エラー表示のために呼ばれる
    公開 API(先頭 `_` を付けない)。
    """
    if result.error:
        return result.error
    stderr = (result.stderr or "").strip()
    if stderr:
        return stderr
    return f"vvread がエラーを返しました (exit {result.returncode})"


# ---------------------------------------------------------------------------
# status --json パース
# ---------------------------------------------------------------------------


@dataclass
class QueueState:
    mode: str = "off"
    pending: int = 0
    playing: int = 0
    failed: int = 0


@dataclass
class StatusState:
    """`vvread status --json` のパース結果。

    state は "disabled"/"muted"/"playing"/"idle" に加え、パース失敗や
    subprocess 失敗を表す "error" を取り得る(例外は投げない)。
    """
    state: str
    mute_until: Optional[int] = None
    queue: QueueState = field(default_factory=QueueState)
    error: Optional[str] = None


def _safe_optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    return 0


def _parse_queue(raw: Any) -> QueueState:
    if not isinstance(raw, dict):
        return QueueState()
    mode = raw.get("mode")
    if mode not in ("on", "off"):
        mode = "off"
    return QueueState(
        mode=mode,
        pending=_safe_count(raw.get("pending")),
        playing=_safe_count(raw.get("playing")),
        failed=_safe_count(raw.get("failed")),
    )


def parse_status_json(text: str) -> StatusState:
    """`vvread status --json` の 1 行出力をパースする。

    壊れた入力(不正 JSON / 非 object / 未知の state)は例外を投げず、
    state="error" + error メッセージに変換する。
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return StatusState(state="error", error="vvread status --json: invalid JSON")

    if not isinstance(data, dict):
        return StatusState(state="error", error="vvread status --json: unexpected payload type")

    state = data.get("state")
    if not isinstance(state, str) or state not in _ALLOWED_STATES:
        return StatusState(state="error", error=f"vvread status --json: unexpected state {state!r}")

    return StatusState(
        state=state,
        mute_until=_safe_optional_int(data.get("mute_until")),
        queue=_parse_queue(data.get("queue")),
    )


def fetch_status(
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = STATUS_TIMEOUT_SEC,
) -> StatusState:
    """`vvread status --json` を実行してパース済み状態を返す(例外を投げない)。"""
    result = run_vvread(
        ["status", "--json"], timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env
    )
    if not result.ok:
        return StatusState(state="error", error=describe_run_error(result))
    return parse_status_json(result.stdout)


# ---------------------------------------------------------------------------
# config --list (TSV) パース
# ---------------------------------------------------------------------------


def parse_config_list(text: str) -> Dict[str, str]:
    """`vvread config --list` の TSV(`key\\tvalue`)をパースする。

    壊れた行(タブなし・空キー)は無視する。同じキーが複数回出た場合は後勝ち。
    """
    result: Dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        key, value = parts
        if not key:
            continue
        result[key] = value
    return result


def get_int_value(config_map: Dict[str, str], key: str) -> Optional[int]:
    """config_map から整数値を取り出す。欠損・変換不能なら None(例外は投げない)。"""
    raw = config_map.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def get_float_value(config_map: Dict[str, str], key: str) -> Optional[float]:
    """config_map から浮動小数点値を取り出す。

    欠損・変換不能なら None(例外は投げない)。`math.isfinite()` で
    `nan`/`inf` を弾き、壊れた設定ファイルに由来する非有限値も None
    として扱う(get_int_value と同じ防御規約)。
    """
    raw = config_map.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value):
        return None
    return value


@dataclass
class ConfigSnapshot:
    """`vvread config --list` を1回実行した結果をまとめて保持するスナップショット。

    話者・音量・スピード・抑揚・pauseScale・maxChunks の6設定すべてを、
    設定ごとに個別 subprocess を起動することなくこの1回の呼び出しから
    解決できるようにするためのもの。呼び出し側は `get_int_value(snapshot.raw, key)`
    / `get_float_value(snapshot.raw, key)` で個別の値を取り出す。
    """
    raw: Dict[str, str]
    error: Optional[str] = None


def fetch_config_snapshot(
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
) -> ConfigSnapshot:
    """`vvread config --list` を1回だけ実行してパース済み設定マップを返す。

    例外は投げない。失敗時は `ConfigSnapshot(raw={}, error=...)` を返す。
    """
    result = run_vvread(
        ["config", "--list"], timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env
    )
    if not result.ok:
        return ConfigSnapshot(raw={}, error=describe_run_error(result))
    return ConfigSnapshot(raw=parse_config_list(result.stdout))


# ---------------------------------------------------------------------------
# speakers --json パース + DTO 正規化
# ---------------------------------------------------------------------------


@dataclass
class StyleDTO:
    id: int
    name: str


@dataclass
class SpeakerDTO:
    name: str
    styles: List[StyleDTO] = field(default_factory=list)


@dataclass
class SpeakersResult:
    speakers: List[SpeakerDTO] = field(default_factory=list)
    error: Optional[str] = None


def _truncate_name(name: str) -> str:
    if len(name) <= MAX_NAME_LENGTH:
        return name
    return name[: MAX_NAME_LENGTH - 1] + "…"


def parse_speakers_json(text: str) -> SpeakersResult:
    """`vvread speakers --json` をパースし、防御的に正規化した DTO を返す。

    - 壊れた入力(不正 JSON / 非 list)は例外を投げず error 付きの空リストにする。
    - 不正なエントリ(name/styles 欠損・型不一致)はスキップする。
    - style id はグローバルに重複除外する(既定話者選択で ID を一意キーとして
      使うため、speaker をまたいだ重複があると選択が曖昧になる)。
    - 異常に長い名前は MAX_NAME_LENGTH で切り詰める。
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return SpeakersResult(error="vvread speakers --json: invalid JSON")

    if not isinstance(data, list):
        return SpeakersResult(error="vvread speakers --json: unexpected payload type")

    seen_ids: set = set()
    speakers: List[SpeakerDTO] = []
    for sp in data:
        if not isinstance(sp, dict):
            continue
        name = sp.get("name")
        styles_raw = sp.get("styles")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(styles_raw, list):
            continue

        styles: List[StyleDTO] = []
        for st in styles_raw:
            if not isinstance(st, dict):
                continue
            st_id = st.get("id")
            st_name = st.get("name")
            if isinstance(st_id, bool) or not isinstance(st_id, int):
                continue
            if not isinstance(st_name, str) or not st_name:
                continue
            if st_id in seen_ids:
                continue
            seen_ids.add(st_id)
            styles.append(StyleDTO(id=st_id, name=_truncate_name(st_name)))

        if styles:
            speakers.append(SpeakerDTO(name=_truncate_name(name), styles=styles))

    return SpeakersResult(speakers=speakers)


def fetch_speakers(
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
) -> SpeakersResult:
    """`vvread speakers --json` を実行してパース済み話者一覧を返す(例外を投げない)。"""
    result = run_vvread(
        ["speakers", "--json"], timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env
    )
    if not result.ok:
        return SpeakersResult(error=describe_run_error(result))
    return parse_speakers_json(result.stdout)


@dataclass
class SpeakerMenuEntry:
    label: str
    style_id: int
    checked: bool


def build_speaker_menu_entries(
    speakers: Sequence[SpeakerDTO], current_speaker_id: Optional[int]
) -> List[SpeakerMenuEntry]:
    """既定話者サブメニュー用の表示行データを組み立てる(rumps 非依存)。

    label には末尾に style id を付与し(例: "四国めたん - ノーマル (2)")、
    speaker 名・style 名が(切り詰め後の衝突を含め)完全一致しても一意になる
    ようにする。rumps.MenuItem は title(=label)をキーとして扱うため、
    重複すると `menubar.py` 側の `insert_before()` が失敗しうる。
    """
    entries: List[SpeakerMenuEntry] = []
    for sp in speakers:
        for st in sp.styles:
            entries.append(
                SpeakerMenuEntry(
                    label=f"{sp.name} - {st.name} ({st.id})",
                    style_id=st.id,
                    checked=(current_speaker_id is not None and st.id == current_speaker_id),
                )
            )
    return entries


# ---------------------------------------------------------------------------
# 既定話者の設定 + 反映検証
# ---------------------------------------------------------------------------


@dataclass
class SetSpeakerResult:
    ok: bool
    warning: Optional[str] = None
    error: Optional[str] = None
    effective_speaker_id: Optional[int] = None


def set_default_speaker(
    speaker_id: int,
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
) -> SetSpeakerResult:
    """`vvread config --set voicevox.speaker=N --user-setting` を実行し、
    直後に `config --list` を再読して反映を確認する。

    project settings 等より優先度の低いスコープに書き込むため、書き込みが
    成功しても実効値が変わらない(project 側が勝つ)ことがある。その場合は
    ok=True のまま warning を返す(書き込み自体は成功しているため)。
    """
    set_result = run_vvread(
        ["config", "--set", f"{SPEAKER_SETTING_KEY}={speaker_id}", "--user-setting"],
        timeout=timeout,
        vvread_bin=vvread_bin,
        cwd=cwd,
        env=env,
    )
    if not set_result.ok:
        return SetSpeakerResult(ok=False, error=describe_run_error(set_result))

    list_result = run_vvread(
        ["config", "--list"], timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env
    )
    if not list_result.ok:
        # 書き込み自体は成功しているので ok=False にはせず、確認できなかった旨を警告にする。
        return SetSpeakerResult(
            ok=True,
            warning=f"設定は保存しましたが反映確認に失敗しました: {describe_run_error(list_result)}",
        )

    config_map = parse_config_list(list_result.stdout)
    effective = get_int_value(config_map, SPEAKER_SETTING_KEY)
    if effective == speaker_id:
        return SetSpeakerResult(ok=True, effective_speaker_id=effective)
    return SetSpeakerResult(
        ok=True,
        warning=SPEAKER_SCOPE_WARNING,
        effective_speaker_id=effective,
    )


# ---------------------------------------------------------------------------
# 5パラメータ(音量/スピード/抑揚/pauseScale/maxChunks)の選択肢エントリ + 汎用setter
# ---------------------------------------------------------------------------


@dataclass
class ConfigChoiceEntry:
    label: str
    raw_value: str  # `vvread config --set` に渡す文字列(例: "1.5", "0")
    checked: bool


def build_float_choice_entries(
    choices: Sequence[float], current: Optional[float]
) -> List[ConfigChoiceEntry]:
    """音量/スピード/抑揚/pauseScale の選択肢メニュー行データを組み立てる。

    current との比較は math.isclose(current, choice, rel_tol=0, abs_tol=1e-6)
    で行う(`config --list` が返す浮動小数点の文字列表現が選択肢と完全一致
    しない可能性があるため、文字列比較ではなく数値許容誤差比較にする)。
    current が None または非有限(nan/inf)なら すべて checked=False。
    raw_value / label は f"{c:.1f}" で生成する。
    """
    entries: List[ConfigChoiceEntry] = []
    comparable_current: Optional[float] = (
        current if (current is not None and math.isfinite(current)) else None
    )
    for choice in choices:
        checked = comparable_current is not None and math.isclose(
            comparable_current, choice, rel_tol=0, abs_tol=1e-6
        )
        label = f"{choice:.1f}"
        entries.append(ConfigChoiceEntry(label=label, raw_value=label, checked=checked))
    return entries


def build_labeled_choice_entries(
    choices: Sequence[Tuple[str, int]], current: Optional[int]
) -> List[ConfigChoiceEntry]:
    """maxChunks 用の選択肢メニュー行データを組み立てる。

    choices は (label, value) のタプル列(`MAX_CHUNKS_CHOICES` を想定)。
    current と value が一致(int比較)すれば checked=True。
    """
    entries: List[ConfigChoiceEntry] = []
    for label, value in choices:
        checked = current is not None and current == value
        entries.append(ConfigChoiceEntry(label=label, raw_value=str(value), checked=checked))
    return entries


def _raw_values_numerically_equal(raw_value: str, effective_raw: Optional[str]) -> bool:
    """set_config_value() の実効値確認用。float() へキャストした許容誤差比較。

    effective_raw が欠損、またはどちらかが数値変換できない場合は不一致扱い
    (安全側: override警告を出す)。
    """
    if effective_raw is None:
        return False
    try:
        return math.isclose(float(raw_value), float(effective_raw), rel_tol=0, abs_tol=1e-6)
    except (ValueError, TypeError):
        return False


@dataclass
class SetConfigResult:
    ok: bool
    warning: Optional[str] = None
    error: Optional[str] = None
    effective_raw: Optional[str] = None  # 書込み後に config --list を再読した生の文字列値


def set_config_value(
    key: str,
    raw_value: str,
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
) -> SetConfigResult:
    """`vvread config --set {key}={raw_value} --user-setting` を実行し、
    直後に `config --list` を再読して実効値を確認する(`set_default_speaker`
    と同じ契約: 話者専用ロジックはそのまま独立させ、この5パラメータ
    (音量/スピード/抑揚/pauseScale/maxChunks)用に別の汎用関数として新設)。

    書込み自体が失敗すれば ok=False。書込みは成功したが実効値が raw_value
    と数値として一致しない場合(他スコープがoverrideしている場合)は
    ok=True のまま `SPEAKER_SCOPE_WARNING` を再利用した警告を返す。
    数値一致判定は文字列比較ではなく float() へキャストした許容誤差比較で
    行う(maxChunks は整数だが raw_value は常に文字列として扱う)。
    """
    set_result = run_vvread(
        ["config", "--set", f"{key}={raw_value}", "--user-setting"],
        timeout=timeout,
        vvread_bin=vvread_bin,
        cwd=cwd,
        env=env,
    )
    if not set_result.ok:
        return SetConfigResult(ok=False, error=describe_run_error(set_result))

    list_result = run_vvread(
        ["config", "--list"], timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env
    )
    if not list_result.ok:
        # 書き込み自体は成功しているので ok=False にはせず、確認できなかった旨を警告にする。
        return SetConfigResult(
            ok=True,
            warning=f"設定は保存しましたが反映確認に失敗しました: {describe_run_error(list_result)}",
        )

    config_map = parse_config_list(list_result.stdout)
    effective_raw = config_map.get(key)
    if _raw_values_numerically_equal(raw_value, effective_raw):
        return SetConfigResult(ok=True, effective_raw=effective_raw)
    return SetConfigResult(ok=True, warning=SPEAKER_SCOPE_WARNING, effective_raw=effective_raw)


# ---------------------------------------------------------------------------
# アクション(fire-and-forget 系サブコマンド)
# ---------------------------------------------------------------------------


def run_action(
    argv: Sequence[str],
    *,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
) -> RunResult:
    return run_vvread(argv, timeout=timeout, vvread_bin=vvread_bin, cwd=cwd, env=env)


def action_set_enabled(enabled: bool, **kwargs: Any) -> RunResult:
    return run_action(["on"] if enabled else ["off"], **kwargs)


def action_mute(duration: str, **kwargs: Any) -> RunResult:
    return run_action(["mute", duration], **kwargs)


def action_unmute(**kwargs: Any) -> RunResult:
    return run_action(["unmute"], **kwargs)


def action_stop(**kwargs: Any) -> RunResult:
    return run_action(["stop"], **kwargs)


def action_queue_clear(**kwargs: Any) -> RunResult:
    return run_action(["queue", "clear"], **kwargs)


# ---------------------------------------------------------------------------
# キューモード ON/OFF (retry ベースの状態遷移)
# ---------------------------------------------------------------------------


@dataclass
class QueueModeChangeResult:
    """`action_queue_set_mode()` の結果。

    attempts: `vvread queue off` を再試行した回数(初回の off 試行は含まない)。
    渡された `queue` スナップショットが空/非空のいずれであっても、初回の
    off 試行が成功すれば(stop の有無に関わらず)0 のままになる。初回の
    off が失敗した場合は空/非空どちらの経路でも同じ retry ループに合流
    するため、空スナップショットのケースでも 1 以上になり得る。
    """
    ok: bool
    error: Optional[str] = None
    attempts: int = 0


def action_queue_set_mode(
    enable: bool,
    *,
    queue: QueueState,
    vvread_bin: Optional[Path] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = ACTION_TIMEOUT_SEC,
    max_retries: int = 3,
    retry_interval_sec: float = 0.5,
) -> QueueModeChangeResult:
    """キューモードの ON/OFF を切り替える。

    enable=True は `vvread queue on` を1回実行するだけ(拒否条件がないため)。

    enable=False は、渡された `queue`(呼び出し時点で保持している最新の
    QueueState。数秒古い可能性がある)が空でなければ `vvread stop` を先に
    実行する(`stop` 自体が失敗した場合は off を試さず即座に失敗を返す)。
    空/非空にかかわらず、その後 `vvread queue off` を1回試みる。

    `scripts/cmd/queue.sh` の `cmd_off` は pending/playing ディレクトリの
    件数を同期的にチェックして拒否する一方、`stop` がトリガーする
    `_queue_stop_if_active`(drainer への停止要求)は非同期のため、stop
    直後でもディレクトリの実削除が完了していない可能性がある。加えて、
    呼び出し時点で渡された `queue` スナップショット自体が実際のディレクトリ
    状態より古いことがあり、「空のはず」の場合でも呼び出し直前に何かが
    enqueue されていた可能性がある。そのため、空/非空どちらの経路でも
    最初の off が失敗した場合は同じ retry ループに合流させる:
    `fetch_status()` で最新状態を取り直しつつ `retry_interval_sec` 秒
    待ってから再度 off を試行し、これを最大 `max_retries` 回繰り返す。
    それでも成功しなければ最後の失敗理由を `error` に入れて ok=False を
    返す(例外は投げない)。
    """
    run_kwargs: Dict[str, Any] = dict(vvread_bin=vvread_bin, cwd=cwd, env=env, timeout=timeout)

    if enable:
        result = run_vvread(["queue", "on"], **run_kwargs)
        if result.ok:
            return QueueModeChangeResult(ok=True)
        return QueueModeChangeResult(ok=False, error=describe_run_error(result))

    if queue.pending > 0 or queue.playing > 0:
        stop_result = run_vvread(["stop"], **run_kwargs)
        if not stop_result.ok:
            return QueueModeChangeResult(ok=False, error=describe_run_error(stop_result), attempts=0)

    off_result = run_vvread(["queue", "off"], **run_kwargs)
    if off_result.ok:
        return QueueModeChangeResult(ok=True, attempts=0)

    last_error = describe_run_error(off_result)
    attempts = 0
    for _ in range(max_retries):
        current = fetch_status(vvread_bin=vvread_bin, cwd=cwd, env=env)
        # 取得失敗(state == "error")は「まだ空だと確認できていない」の
        # 安全側として busy 扱いにする。空だと確認できた場合でも off 自体は
        # 必ず再試行する(off の拒否可否を最終的に決めるのは cmd_off 側の
        # 同期チェックであり、こちらの status 取得はあくまで参考情報のため)。
        still_busy = current.state == "error" or current.queue.pending > 0 or current.queue.playing > 0
        if still_busy and retry_interval_sec > 0:
            time.sleep(retry_interval_sec)
        off_result = run_vvread(["queue", "off"], **run_kwargs)
        attempts += 1
        if off_result.ok:
            return QueueModeChangeResult(ok=True, attempts=attempts)
        last_error = describe_run_error(off_result)

    return QueueModeChangeResult(ok=False, error=last_error, attempts=attempts)


def toggle_action_enables(state: str) -> bool:
    """次にトグルを押したとき enable すべきか(True)/disable すべきか(False)。

    disabled のときのみ enable 方向。idle/playing/muted はもちろん、
    state 取得に失敗した "error" のときも安全側(disable 方向)にする。
    """
    return state == "disabled"


# ---------------------------------------------------------------------------
# 表示モデル変換
# ---------------------------------------------------------------------------


@dataclass
class DisplayModel:
    icon: str
    state_line: str
    queue_line: str


def to_display_model(status: StatusState) -> DisplayModel:
    """StatusState を menubar 表示用モデルに変換する。

    state_line は idle/playing/muted/disabled/error の 5 状態を
    🟢/🟡/🔴/⚠ の 3 色(+エラー)へ統合した 1 行のテキストで、
    メニューバー本体のトレイアイコン(`icon`/`ICONS`)とは独立した表現である。
    - idle/playing → 🟢「vvread は 稼働中」(両状態でテキスト完全一致)。
    - muted → 🟡「vvread は {HH:MM} までミュート中」(ローカル時刻の絶対表示)。
      mute_until が None(壊れたデータ)の場合はクラッシュさせず時刻部分を
      省略した文言にフォールバックする。
    - disabled → 🔴「vvread は 停止中」。
    - error → 既存の `_STATE_LABELS["error"]` 文言を流用し ⚠ プレフィックス。
    """
    icon = ICONS.get(status.state, ICONS["error"])
    queue = status.queue
    queue_line = f"キュー: 待機 {queue.pending} / 再生中 {queue.playing} / 失敗 {queue.failed}"

    if status.state in ("idle", "playing"):
        state_line = "🟢 vvread は 稼働中"
    elif status.state == "muted":
        if status.mute_until is None:
            state_line = "🟡 vvread は ミュート中"
        else:
            hhmm = time.strftime("%H:%M", time.localtime(status.mute_until))
            state_line = f"🟡 vvread は {hhmm} までミュート中"
    elif status.state == "disabled":
        state_line = "🔴 vvread は 停止中"
    else:
        state_line = f"⚠ vvread は {_STATE_LABELS['error']}"

    return DisplayModel(icon=icon, state_line=state_line, queue_line=queue_line)


# ---------------------------------------------------------------------------
# ポーリング間隔の検証
# ---------------------------------------------------------------------------


def resolve_poll_interval(env: Optional[Dict[str, str]] = None) -> float:
    """VVREAD_MENUBAR_INTERVAL を検証する。

    - 未設定 / 空文字 → 既定 2 秒。
    - 数値に変換できない、または非有限(inf/nan)、または 0 以下 → 既定 2 秒。
    - 正の有限値 → 1〜60 秒に clamp して採用(既定値へのフォールバックはしない)。
    """
    source = env if env is not None else os.environ
    raw = source.get(POLL_INTERVAL_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_POLL_INTERVAL_SEC
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return DEFAULT_POLL_INTERVAL_SEC
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_POLL_INTERVAL_SEC
    return min(max(value, MIN_POLL_INTERVAL_SEC), MAX_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# poll の in-flight / 世代管理 + 連続失敗トラッキング
# ---------------------------------------------------------------------------


class PollGeneration:
    """action 実行後に古い poll 結果を破棄するための世代カウンタ。

    rumps.Timer のコールバックは action のクリック処理とは独立に呼ばれる。
    action 実行直後に即時 refresh すると、その直前に発行されていた poll の
    結果が遅れて到着し表示を巻き戻す恐れがある。token() で発行した世代と
    is_current() を比較し、古い結果は adapter 側で捨てる。
    """

    def __init__(self) -> None:
        self._generation = 0

    def token(self) -> int:
        """新しい poll を開始する前に呼び、その poll の世代トークンを返す。"""
        return self._generation

    def bump(self) -> int:
        """action 実行後に呼び、世代を進めて古い poll 結果を無効化する。"""
        self._generation += 1
        return self._generation

    def is_current(self, token: int) -> bool:
        return token == self._generation


class PollFailureTracker:
    """連続 poll 失敗回数を数え、閾値超で degraded(⚠ タイトル)にする state machine。"""

    def __init__(self, threshold: int = DEFAULT_POLL_FAILURE_THRESHOLD) -> None:
        self._threshold = max(threshold, 1)
        self._consecutive_failures = 0

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def degraded(self) -> bool:
        return self._consecutive_failures >= self._threshold


# ---------------------------------------------------------------------------
# 二重起動防止 lock
# ---------------------------------------------------------------------------


class LockError(Exception):
    """ロック取得に失敗した(既に起動中と推定される)。"""

    def __init__(self, message: str, pid: Optional[int] = None) -> None:
        super().__init__(message)
        self.pid = pid


def default_lock_path() -> Path:
    return _paths.state_dir() / "menubar.lock"


def _read_pid(fd: int) -> Optional[int]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 32).decode("utf-8", "ignore").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


class SingleInstanceLock:
    """fcntl.flock(LOCK_EX | LOCK_NB) によるプロセス存続中の二重起動防止。

    open は O_NOFOLLOW(symlink 攻撃防止) + mode 0600(共有ホストでの覗き見防止)。
    PID はロック判定には使わず、案内表示用にファイル内へ書き込むのみ
    (stale PID の誤判定を避けるため、判定は flock 自体に委ねる)。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path if path is not None else default_lock_path()
        self._fd: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(str(self._path), flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise
            pid = _read_pid(fd)
            os.close(fd)
            message = "vvread menubar は既に起動中です"
            if pid is not None:
                message += f" (pid={pid})"
            raise LockError(message, pid=pid) from e

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()
