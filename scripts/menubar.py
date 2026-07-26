#!/usr/bin/env python3
"""scripts/menubar.py - vvread menubar: rumps adapter (B-151, P2b / menubar-redesign T2)

rumps 製の macOS メニューバー常駐 UI。ロジックは全て menubar_core に委譲し、
本ファイルは Timer 駆動・メニュー描画・クリック→core 呼び出しのみを担う
薄い adapter に保つ(pytest 対象外。手動テスト手順書は doc/reference/03-menubar-manual-test.md)。

起動経路: scripts/cmd/menubar.sh が rumps 導入済みの Python でこのファイルを exec する。

メニュー構成:
  状態行 / キュー行 / エラー行(表示のみ、エラー時のみ)
  ──────────────
  読み上げ(トグル) / キューモード(トグル) / 一時ミュート ▸(5分/30分/1時間/解除)
  ──────────────
  現在再生中を停止 / キューをクリア / デフォルト設定 ▸(話者 ▸ / 音量 ▸ / スピード ▸ /
    抑揚 ▸ / 句読点ポーズ ▸ / 最大チャンク数 ▸)
  ──────────────
  vvread menubarを終了

読み上げ / キューモードのトグルは現時点ではチェックマーク方式(NSSwitch へのピル型スイッチ
化は後続タスク)。両トグルとも「desired-state を明示して駆動」「action 実行中は項目を
無効化」「action 専用の世代トークンで多重実行時の古い結果を破棄」という土台を実装し、
NSSwitch 化してもそのまま乗せられるようにしてある(`_run_guarded_action` 参照)。

スレッドモデル(Codex ブランチレビュー指摘 #1 対応):
  rumps の Timer コールバック・メニュークリックコールバックはすべて Cocoa の
  メインスレッド(run loop)で実行される。menubar_core の CLI 呼び出しは同期
  subprocess.run(status timeout 1s / action・speakers timeout 5s)のため、
  そのままメインスレッドで呼ぶとメニュー UI 全体が最大 5 秒フリーズし、
  30 秒ごとの話者 retry のたびに再発する。
  そのため CLI 呼び出しは必ず `_run_async()` 経由でワーカースレッド
  (都度起動の daemon thread)へ逃がし、結果反映(UI 更新)は
  `PyObjCTools.AppHelper.callAfter()` でメインスレッドへマーシャルする。
  ワーカースレッド側は menubar_core の純粋関数(subprocess 呼び出し +
  dataclass 返却)のみを呼び、self(App インスタンス)の属性には一切触れない
  設計にすることで、追加のロックなしにスレッド安全性を担保している
  (world 状態への書き込みはすべて `done` コールバック内、つまりメインスレッド
  上でのみ発生する)。世代管理(PollGeneration)・連続失敗カウンタ
  (PollFailureTracker)もこの理由でメインスレッドに閉じており、
  menubar_core.py 側の変更は不要と判断した。

「MenuItem を使い回し clear() しない」設計(Codex ブランチレビュー指摘 #2 対応):
  rumps 0.4.0 の Menu.clear() は native の NSMenuItem は削除するが、
  NSApp._ns_to_py_and_callback に登録された旧 MenuItem/コールバック参照は
  解除しない。低頻度 retry のたびに clear()+再構築すると無限にリークするため、
  以後は一切 clear()/削除をせず、キー(style_id / raw_value)を使い回して
  title/state/hidden をその場で更新する設計にする(新しいキーが現れたときだけ
  .add()/insert_before() を呼ぶ。追加回数は「アプリ起動中に観測した distinct
  キーの総数」で頭打ちになるため、無限リークにならない)。既定話者サブメニュー
  (`_speaker_items`)で確立したこのパターンを、音量/スピード/抑揚/句読点ポーズ/
  最大チャンク数の 5 パラメータにも `_ScalarSubmenu` として共通化して適用する。
"""
from __future__ import annotations

import signal
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import rumps
from PyObjCTools import AppHelper

import menubar_core as mc

# SIGTERM/SIGINT で確実に applicationWillTerminate_ (→ events.before_quit) を
# 経由させるため、rumps.quit_application() を呼ぶだけの薄いハンドラにする。
# 実際の後始末(Timer 停止・flock 解放)は before_quit フックに集約する
# (quit_application() は NSApplication.terminate_ を呼び、通常はそのまま
#  プロセスが終了するため、app.run() の戻り値やその後のコードは実行される
#  保証がない — 後始末は必ず「終了が起きる前」のフックで行う)。

# Engine 未接続などで話者一覧/設定スナップショット取得が失敗したときの低頻度
# retry 間隔(秒)。通常の status poll(既定 2 秒、最大 60 秒)とは独立させ、
# 「デフォルト設定」サブメニュー(話者 + 5 パラメータ)の取得は「起動時 + 手動
# 再読み込み + この低頻度 retry」のみに限定する(plan 確定値 4)。
_SPEAKER_RETRY_SEC = 30.0

_SPEAKER_RELOAD_KEY = "再読み込み"


# entries_fn は選択肢リストに加えて、現在値の表示用文字列(Optional[str])も返す。
# 「取得できない/非有限」な場合は None(状態行を出さない)。取得できた場合は
# `config --list` が返した生の文字列をそのまま使う(浮動小数点として再整形すると
# 0.1 刻み外の値(例: "1.55")が丸められて意味が変わってしまうため)。
_EntriesFn = Callable[[mc.ConfigSnapshot], Tuple[List[mc.ConfigChoiceEntry], Optional[str]]]


def _float_entries_fn(choices: Tuple[float, ...], key: str) -> _EntriesFn:
    """音量/スピード/抑揚/句読点ポーズ用の entries_fn を組み立てる(`_ScalarSubmenu` へ渡す)。"""

    def fn(snapshot: mc.ConfigSnapshot) -> Tuple[List[mc.ConfigChoiceEntry], Optional[str]]:
        current = mc.get_float_value(snapshot.raw, key)
        entries = mc.build_float_choice_entries(choices, current)
        display = snapshot.raw.get(key) if current is not None else None
        return entries, display

    return fn


def _labeled_entries_fn(choices: Tuple[Tuple[str, int], ...], key: str) -> _EntriesFn:
    """最大チャンク数用の entries_fn を組み立てる(`_ScalarSubmenu` へ渡す)。"""

    def fn(snapshot: mc.ConfigSnapshot) -> Tuple[List[mc.ConfigChoiceEntry], Optional[str]]:
        current = mc.get_int_value(snapshot.raw, key)
        entries = mc.build_labeled_choice_entries(choices, current)
        display = snapshot.raw.get(key) if current is not None else None
        return entries, display

    return fn


class _ScalarSubmenu:
    """音量/スピード/抑揚/句読点ポーズ/最大チャンク数の共通サブメニュー実装。

    既定話者サブメニュー(`_speaker_items`)と同じ「MenuItem を使い回し、
    clear() しない」設計: `apply()` を呼ぶたびに entries_fn(snapshot) で
    最新の選択肢を計算し、新しい raw_value が現れたときだけ `.add()` で
    新規 MenuItem を作る。以後は title/state/hidden をその場で更新するだけ
    (静的な選択肢リストのため通常は初回の apply() で全項目が出揃う)。

    加えて、既定話者サブメニューの `_speaker_status_item`(「読み込み中…」「取得失敗: ...」等を
    表示する専用 MenuItem)と同じパターンで、現在値が選択肢のどれとも一致しない(選択肢範囲外・
    非有限・0.1 刻み非一致)場合に限り「現在値: {値}(選択肢外)」を表示する状態行
    (`self._status_item`)を持つ。この項目も他の entry と同様に一度作ったら削除せず、
    hidden の切り替えだけで表示/非表示を制御する(advisor レビュー指摘: 課題1)。
    """

    def __init__(
        self,
        title: str,
        setting_key: str,
        entries_fn: _EntriesFn,
        on_select: Callable[[str, str], None],
    ) -> None:
        self.item = rumps.MenuItem(title)
        self.setting_key = setting_key
        self._entries_fn = entries_fn
        self._on_select = on_select
        self._entry_items: Dict[str, rumps.MenuItem] = {}
        # 選択肢のどれもチェックされない場合にのみ表示する状態行。既定話者サブメニューの
        # `_speaker_status_item` と同様、常に存在させ hidden で出し分ける(clear() しない設計)。
        self._status_item = rumps.MenuItem("")
        self._status_item.hidden = True
        self.item.add(self._status_item)

    def apply(self, snapshot: mc.ConfigSnapshot) -> None:
        entries, current_display = self._entries_fn(snapshot)
        seen_raw_values = set()
        any_checked = False
        for entry in entries:
            seen_raw_values.add(entry.raw_value)
            menu_item = self._entry_items.get(entry.raw_value)
            if menu_item is None:
                menu_item = rumps.MenuItem(
                    entry.label, callback=self._make_handler(entry.raw_value)
                )
                self.item.add(menu_item)
                self._entry_items[entry.raw_value] = menu_item
            menu_item.title = entry.label
            menu_item.state = 1 if entry.checked else 0
            menu_item.hidden = False
            if entry.checked:
                any_checked = True

        for raw_value, menu_item in self._entry_items.items():
            if raw_value not in seen_raw_values:
                menu_item.hidden = True

        if not any_checked and current_display is not None:
            self._status_item.title = f"現在値: {current_display}(選択肢外)"
            self._status_item.hidden = False
        else:
            self._status_item.hidden = True

    def _make_handler(self, raw_value: str) -> Callable[[rumps.MenuItem], None]:
        def handler(_sender: rumps.MenuItem) -> None:
            self._on_select(self.setting_key, raw_value)

        return handler


class VvreadMenubarApp(rumps.App):
    def __init__(self, lock: mc.SingleInstanceLock) -> None:
        super().__init__("vvread", title=mc.ICONS["idle"], quit_button=None)
        self._lock = lock
        self._generation = mc.PollGeneration()
        # トグル(読み上げ/キューモード)専用と、config 選択(話者+5パラメータ)
        # 専用の世代カウンタを分離する(Codex ブランチレビュー指摘 #2)。
        # 1個の世代トークンを両系統で共有すると、一方の action 実行中に
        # 他方が bump() した瞬間、先発の action の完了コールバック
        # (on_result: エラー表示・_refresh_status(force=True) 等)が
        # is_current() 判定で丸ごとスキップされてしまう。
        self._toggle_action_generation = mc.PollGeneration()
        self._config_action_generation = mc.PollGeneration()
        self._failures = mc.PollFailureTracker()
        # トグル 2 種それぞれの「実行中」フラグ(Codex ブランチレビュー指摘 #3)。
        # `_apply_status()` は status poll のたびに呼ばれ、対応するトグルの
        # フラグが立っている間は set_callback() による再有効化をスキップする
        # (state によるチェックマーク更新自体はフラグに関わらず行ってよい)。
        self._toggle_action_inflight: bool = False
        self._queue_mode_action_inflight: bool = False
        # config 選択(話者+5パラメータ)操作の直列化フラグ(Codex ブランチ
        # レビュー指摘 #1)。既に実行中の config 操作があれば新しいクリックは
        # 無視する(`vvread config --set` は同じ設定ファイルへ read-modify-write
        # するため、2 つを並行起動すると書き込み内容とチェックマークが
        # 食い違いうる)。
        self._config_action_inflight: bool = False
        self._last_status: mc.StatusState = mc.StatusState(state="idle")
        self._ever_succeeded: bool = False
        self._speakers_result: mc.SpeakersResult = mc.SpeakersResult()
        self._current_speaker_id: Optional[int] = None
        self._config_snapshot: mc.ConfigSnapshot = mc.ConfigSnapshot(raw={})
        self._cleaned_up = False

        # ----- メニュー項目(表示専用) -----
        self.state_item = rumps.MenuItem("状態: -")
        self.queue_item = rumps.MenuItem("キュー: -")
        self.error_item = rumps.MenuItem("")
        self.error_item.hidden = True

        # ----- メニュー項目(操作: 読み上げ / キューモード / 一時ミュート) -----
        # トグル 2 種は ON/OFF をチェックマーク(`item.state`)に加えてタイトル
        # 文字列でも表示する(ユーザー要望: 状態を文字でも見えるように)。
        # 初期タイトルは `self._last_status = mc.StatusState(state="idle")` に
        # 対応する自然な初期値(読み上げ ON / キューモード OFF)。実際の値は
        # 起動直後の `_apply_status()` で即座に上書きされる。
        self.toggle_item = rumps.MenuItem("読み上げ ON", callback=self._on_toggle)
        self.queue_mode_item = rumps.MenuItem(
            "キューモード OFF", callback=self._on_queue_mode_toggle
        )

        self.mute_submenu = rumps.MenuItem("一時ミュート")
        for label, duration in mc.MUTE_DURATIONS:
            self.mute_submenu.add(
                rumps.MenuItem(label, callback=self._make_mute_handler(duration))
            )
        self.mute_submenu.add(rumps.separator)
        self.mute_submenu.add(rumps.MenuItem("解除", callback=self._on_unmute))

        # ----- メニュー項目(操作: 停止 / クリア / デフォルト設定) -----
        self.stop_item = rumps.MenuItem("現在再生中を停止", callback=self._on_stop)
        self.queue_clear_item = rumps.MenuItem("キューをクリア", callback=self._on_queue_clear)

        self.speaker_submenu = rumps.MenuItem("話者")
        self._speaker_items: Dict[int, rumps.MenuItem] = {}
        self._init_speaker_submenu()

        self.volume_submenu = _ScalarSubmenu(
            "音量",
            mc.VOLUME_SETTING_KEY,
            _float_entries_fn(mc.VOLUME_CHOICES, mc.VOLUME_SETTING_KEY),
            self._on_config_choice_selected,
        )
        self.speed_submenu = _ScalarSubmenu(
            "スピード",
            mc.SPEED_SETTING_KEY,
            _float_entries_fn(mc.SPEED_CHOICES, mc.SPEED_SETTING_KEY),
            self._on_config_choice_selected,
        )
        self.intonation_submenu = _ScalarSubmenu(
            "抑揚",
            mc.INTONATION_SETTING_KEY,
            _float_entries_fn(mc.INTONATION_CHOICES, mc.INTONATION_SETTING_KEY),
            self._on_config_choice_selected,
        )
        self.pause_scale_submenu = _ScalarSubmenu(
            "句読点ポーズ",
            mc.PAUSE_SCALE_SETTING_KEY,
            _float_entries_fn(mc.PAUSE_SCALE_CHOICES, mc.PAUSE_SCALE_SETTING_KEY),
            self._on_config_choice_selected,
        )
        self.max_chunks_submenu = _ScalarSubmenu(
            "最大チャンク数",
            mc.MAX_CHUNKS_SETTING_KEY,
            _labeled_entries_fn(mc.MAX_CHUNKS_CHOICES, mc.MAX_CHUNKS_SETTING_KEY),
            self._on_config_choice_selected,
        )

        self.default_settings_submenu = rumps.MenuItem("デフォルト設定")
        self.default_settings_submenu.add(self.speaker_submenu)
        self.default_settings_submenu.add(self.volume_submenu.item)
        self.default_settings_submenu.add(self.speed_submenu.item)
        self.default_settings_submenu.add(self.intonation_submenu.item)
        self.default_settings_submenu.add(self.pause_scale_submenu.item)
        self.default_settings_submenu.add(self.max_chunks_submenu.item)

        self.quit_item = rumps.MenuItem("vvread menubarを終了", callback=self._on_quit)

        self.menu = [
            self.state_item,
            self.queue_item,
            self.error_item,
            rumps.separator,
            self.toggle_item,
            self.queue_mode_item,
            self.mute_submenu,
            rumps.separator,
            self.stop_item,
            self.queue_clear_item,
            self.default_settings_submenu,
            rumps.separator,
            self.quit_item,
        ]

        # ----- Timer -----
        # status --json のみを resolve_poll_interval() の間隔でポーリングする
        # (話者一覧/設定スナップショットはここでは絶対にポーリングしない)。
        interval = mc.resolve_poll_interval()
        self.status_timer = rumps.Timer(self._on_status_poll, interval)
        self.status_timer.start()

        self.speaker_retry_timer = rumps.Timer(self._on_speaker_retry, _SPEAKER_RETRY_SEC)
        self.speaker_retry_timer.start()

        # 後始末は rumps の before_quit フックに一本化する。SIGTERM ハンドラ /
        # 終了メニュー / 例外時のいずれも最終的に quit_application() を呼び、
        # applicationWillTerminate_ が before_quit を emit する経路を通る。
        rumps.events.before_quit.register(self._cleanup)

        self._refresh_status(force=True)
        self._refresh_default_settings()

    # ------------------------------------------------------------------
    # 非同期実行ヘルパー(Codex 指摘 #1: Cocoa メインスレッドをブロックしない)
    # ------------------------------------------------------------------

    def _run_async(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        *,
        on_failure: Optional[Callable[[], None]] = None,
    ) -> None:
        """`work` をワーカースレッドで実行し、結果を `done` へメインスレッドで渡す。

        `work` は menubar_core の純粋関数のみを呼び、self(App)の属性に触れない
        こと(スレッド安全性の前提)。`done` はメインスレッド上で呼ばれるため、
        ここでメニュー項目や self の状態を自由に更新してよい。
        `work` が例外を送出した場合はメニュー内エラー表示に落とし、`done` は
        呼ばない(menubar_core の関数は通常例外を投げない設計のため、この経路は
        主に保険)。`on_failure` を渡した場合、この例外経路でも(メインスレッドで)
        呼ばれる。ガード系ヘルパー(`_run_guarded_action` /
        `_run_generation_guarded_action`)が「実行中」フラグを確実に解除するために
        使う: `done` が一切呼ばれない例外経路でフラグを解除し忘れると、対象の
        トグル/config 操作が以後ずっとロックされたままになってしまう。
        """

        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 - ワーカースレッドを落とさない
                message = str(e)

                def on_error() -> None:
                    self._show_error(message)
                    if on_failure is not None:
                        on_failure()

                AppHelper.callAfter(on_error)
                return
            AppHelper.callAfter(done, result)

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # action 専用の世代ガード(menubar-redesign T2: 多重実行防止の共通土台)
    # ------------------------------------------------------------------
    #
    # `self._generation`(PollGeneration)は status poll の世代管理専用であり、
    # action の多重実行防止には使わない(意味が異なるものを混在させない)。
    # action 側は独立した操作系統ごとに別々の `mc.PollGeneration` インスタンスを
    # 持つ: `self._toggle_action_generation`(読み上げ/キューモードの2トグル)と
    # `self._config_action_generation`(話者+5パラメータの選択6種)。両者を1個の
    # トークンで共有すると、一方の action 実行中に他方が bump() した瞬間、
    # 先発の action の完了コールバック(on_result)が is_current() 判定で丸ごと
    # スキップされてしまう(Codex ブランチレビュー指摘 #2)。

    def _run_guarded_action(
        self,
        item: rumps.MenuItem,
        handler: Callable[[rumps.MenuItem], None],
        action_fn: Callable[[], Any],
        on_result: Callable[[Any], None],
        *,
        set_inflight: Callable[[bool], None],
    ) -> None:
        """item を実行中は無効化(set_callback(None))し、完了後に必ず再有効化する。

        `self._toggle_action_generation` の世代 token で多重実行時の古い結果を
        破棄する(on_result は最新の実行のみ呼ばれる)。読み上げ / キューモードの
        2 トグルに使う。素早い連打でも、実行中は item がグレーアウトして
        再クリックできないため、実質的に直列化される。将来 NSSwitch 化しても
        このまま乗せられる設計にしてある。

        `set_inflight` は呼び出し側のトグル専用「実行中」フラグ
        (`self._toggle_action_inflight` / `self._queue_mode_action_inflight`)を
        更新するコールバック。action 実行中はこのフラグを立てておくことで、
        `_apply_status()`(status poll のたびに呼ばれる)が action 完了前に
        `set_callback()` で再有効化してしまうのを防ぐ(Codex ブランチレビュー
        指摘 #3: poll 間隔より action が長引くと、無効化しても poll のたびに
        クリック可能へ戻ってしまい多重実行防止が意味を失う)。

        `action_fn` が万一例外を送出した場合(`_run_async` の保険経路)も
        `on_failure` で `set_inflight(False)` + `set_callback(handler)` を行い、
        フラグ/callback が立ちっぱなしで item が永久にロックされないようにする。
        """
        item.set_callback(None)
        set_inflight(True)
        token = self._toggle_action_generation.bump()

        def done(result: Any) -> None:
            set_inflight(False)
            item.set_callback(handler)
            if self._toggle_action_generation.is_current(token):
                on_result(result)

        def on_failure() -> None:
            set_inflight(False)
            item.set_callback(handler)

        self._run_async(action_fn, done, on_failure=on_failure)

    def _run_generation_guarded_action(
        self, action_fn: Callable[[], Any], on_result: Callable[[Any], None]
    ) -> None:
        """デフォルト設定サブメニュー(話者 + 5 パラメータ)の選択で使う config 操作を
        直列化しつつ、世代 token で多重実行時の古い結果を破棄する。

        `vvread config --set` は同じ設定ファイルへ read-modify-write するため、
        話者/音量/スピード/抑揚/句読点ポーズ/最大チャンク数のいずれか2つの選択を
        連打すると、サブプロセス自体が並行に2つ起動され、最終的にファイルへ
        書き込まれた内容と画面上のチェックマークが食い違いうる(Codex ブランチ
        レビュー指摘 #1)。そのため、既に config 操作が実行中(`self._config_action_inflight`)
        なら新しいクリックは無視する(=直列化)。世代 token(`self._config_action_generation`)
        による古い結果の破棄は、直列化後も設計の一貫性のため維持する。

        `self._toggle_action_generation`(トグル専用)とは独立した世代カウンタを
        使う(Codex ブランチレビュー指摘 #2)。

        `action_fn` が万一例外を送出した場合(`_run_async` の保険経路)も
        `on_failure` で `_config_action_inflight` を解除し、以後のクリックが
        すべて無視され続ける(config サブメニューが永久に固まる)事態を防ぐ。
        """
        if self._config_action_inflight:
            return  # 既に実行中の config 操作があるので、この操作は無視する(直列化)
        self._config_action_inflight = True
        token = self._config_action_generation.bump()

        def done(result: Any) -> None:
            self._config_action_inflight = False
            if self._config_action_generation.is_current(token):
                on_result(result)

        def on_failure() -> None:
            self._config_action_inflight = False

        self._run_async(action_fn, done, on_failure=on_failure)

    # ------------------------------------------------------------------
    # status polling
    # ------------------------------------------------------------------

    def _on_status_poll(self, _timer: rumps.Timer) -> None:
        self._refresh_status(force=False)

    def _refresh_status(self, *, force: bool) -> None:
        token = self._generation.token()

        def done(status: mc.StatusState) -> None:
            if not force and not self._generation.is_current(token):
                # action 実行で世代が進んでいた場合、この poll 結果は古いので破棄する
                # (in-flight poll が action 後の即時 refresh の結果を巻き戻さないため)。
                return
            self._apply_status(status)

        self._run_async(mc.fetch_status, done)

    def _apply_status(self, status: mc.StatusState) -> None:
        if status.state == "error":
            self._failures.record_failure()
        else:
            self._failures.record_success()

        self._last_status = status

        if status.state == "error":
            if not self._ever_succeeded:
                # まだ一度も正常取得できていない場合のみ「状態不明」を表示する。
                # 過去に正常取得済みなら state_item/queue_item は更新せず、
                # 直前の正常表示(last-known-good)をそのまま保持する。
                self.state_item.title = "⚠ vvread は 状態不明"
                self.queue_item.title = "キュー: -"
            # チェックマーク(state)は last-known-good のまま変更せず、
            # クリックだけを封じる(グレーアウト)。
            self.toggle_item.set_callback(None)
            self.queue_mode_item.set_callback(None)
            self.title = mc.ICONS["error"]
            return

        self._ever_succeeded = True
        model = mc.to_display_model(status)

        # tray icon は既存通り連続失敗カウンタ(degraded)を優先しつつ model.icon を使う
        # (トレイアイコンの仕組み自体はこのタスクで変更しない)。
        self.title = mc.ICONS["error"] if self._failures.degraded else model.icon
        self.state_item.title = model.state_line
        self.queue_item.title = model.queue_line

        # action 実行中(Codex ブランチレビュー指摘 #3)は set_callback() による
        # 再有効化をスキップする(クリック可否だけを維持する)。チェックマーク
        # (state)自体は実行中でも最新の poll 結果をそのまま反映してよい。
        if not self._toggle_action_inflight:
            self.toggle_item.set_callback(self._on_toggle)
        # disabled のときのみ OFF(0)。idle/playing/muted はミュートの有無に関わらず
        # 読み上げ自体は ON(1)(読み上げ ON/OFF はミュートとは独立した概念)。
        reading_on = not mc.toggle_action_enables(status.state)
        self.toggle_item.title = f"読み上げ {'ON' if reading_on else 'OFF'}"
        self.toggle_item.state = 0 if mc.toggle_action_enables(status.state) else 1

        if not self._queue_mode_action_inflight:
            self.queue_mode_item.set_callback(self._on_queue_mode_toggle)
        queue_on = status.queue.mode == "on"
        self.queue_mode_item.title = f"キューモード {'ON' if queue_on else 'OFF'}"
        self.queue_mode_item.state = 1 if queue_on else 0

    # ------------------------------------------------------------------
    # エラー表示(メニュー内、action 失敗用)
    # ------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        self.error_item.title = f"⚠ {message}"
        self.error_item.hidden = False

    def _clear_error(self) -> None:
        self.error_item.hidden = True
        self.error_item.title = ""

    def _run_action_and_refresh(self, action_fn: Callable[[], mc.RunResult]) -> None:
        """action をワーカースレッドで実行し、完了後にメインスレッドで反映する。

        action 完了直後(done 内、メインスレッド)に世代を進める(bump)ことで、
        直前に発行されていた古い poll 結果がこの後の refresh を巻き戻さない
        ようにする。一時ミュート/解除/停止/キュークリアはここでは多重実行が
        致命的でない(冪等または連打しても実害が薄い)操作のため、
        `_run_guarded_action` の対象には含めていない。
        """

        def done(result: mc.RunResult) -> None:
            if result.ok:
                self._clear_error()
            else:
                self._show_error(mc.describe_run_error(result))
            self._generation.bump()
            self._refresh_status(force=True)

        self._run_async(action_fn, done)

    # ------------------------------------------------------------------
    # クリックハンドラ(読み上げ / キューモード: desired-state + 多重実行防止)
    # ------------------------------------------------------------------

    def _on_toggle(self, _sender: rumps.MenuItem) -> None:
        desired = mc.toggle_action_enables(self._last_status.state)

        def action_fn() -> mc.RunResult:
            return mc.action_set_enabled(desired)

        def on_result(result: mc.RunResult) -> None:
            if not result.ok:
                self._show_error(mc.describe_run_error(result))
            else:
                self._clear_error()
            self._generation.bump()
            self._refresh_status(force=True)

        def set_inflight(value: bool) -> None:
            self._toggle_action_inflight = value

        self._run_guarded_action(
            self.toggle_item, self._on_toggle, action_fn, on_result, set_inflight=set_inflight
        )

    def _on_queue_mode_toggle(self, _sender: rumps.MenuItem) -> None:
        desired = self._last_status.queue.mode != "on"

        def action_fn() -> mc.QueueModeChangeResult:
            return mc.action_queue_set_mode(desired, queue=self._last_status.queue)

        def on_result(result: mc.QueueModeChangeResult) -> None:
            if not result.ok:
                self._show_error(result.error or "キューモードの変更に失敗しました")
            else:
                self._clear_error()
            self._generation.bump()
            self._refresh_status(force=True)

        def set_inflight(value: bool) -> None:
            self._queue_mode_action_inflight = value

        self._run_guarded_action(
            self.queue_mode_item,
            self._on_queue_mode_toggle,
            action_fn,
            on_result,
            set_inflight=set_inflight,
        )

    # ------------------------------------------------------------------
    # クリックハンドラ(一時ミュート / 停止 / クリア: 既存どおり)
    # ------------------------------------------------------------------

    def _make_mute_handler(self, duration: str) -> Callable[[rumps.MenuItem], None]:
        def handler(_sender: rumps.MenuItem) -> None:
            self._run_action_and_refresh(lambda: mc.action_mute(duration))

        return handler

    def _on_unmute(self, _sender: rumps.MenuItem) -> None:
        self._run_action_and_refresh(mc.action_unmute)

    def _on_stop(self, _sender: rumps.MenuItem) -> None:
        self._run_action_and_refresh(mc.action_stop)

    def _on_queue_clear(self, _sender: rumps.MenuItem) -> None:
        self._run_action_and_refresh(mc.action_queue_clear)

    def _on_quit(self, _sender: rumps.MenuItem) -> None:
        rumps.quit_application()

    # ------------------------------------------------------------------
    # デフォルト設定サブメニュー(話者 + 音量/スピード/抑揚/句読点ポーズ/最大チャンク数)
    # ------------------------------------------------------------------

    def _init_speaker_submenu(self) -> None:
        """話者サブメニューの永続 widget を一度だけ構築する。

        以後この widget 群(status_item・reload_item・各 style_id の MenuItem)は
        二度と削除せず、`_apply_speakers_result()` で title/state/hidden だけを
        その場で更新する。
        """
        self._speaker_status_item = rumps.MenuItem("読み込み中…")
        self.speaker_submenu.add(self._speaker_status_item)
        self.speaker_submenu.add(rumps.separator)
        self.speaker_submenu.add(
            rumps.MenuItem(_SPEAKER_RELOAD_KEY, callback=self._on_speaker_reload)
        )

    def _on_speaker_retry(self, _timer: rumps.Timer) -> None:
        # Engine 未接続などで前回取得(話者一覧 or 設定スナップショットのいずれか)が
        # 失敗しているときだけ低頻度で再試行する。
        if self._speakers_result.error or self._config_snapshot.error:
            self._refresh_default_settings()

    def _on_speaker_reload(self, _sender: rumps.MenuItem) -> None:
        self._refresh_default_settings()

    def _refresh_default_settings(self) -> None:
        """話者一覧 + 6 設定(話者/音量/スピード/抑揚/句読点ポーズ/最大チャンク数)を
        まとめて再取得する。

        Codex 指摘対応: `config --list` の呼び出しを 1 回にまとめる。
        `fetch_speakers()`(話者一覧、エンジン依存で別コマンド)と
        `fetch_config_snapshot()`(config --list、6 設定分)の 2 回の subprocess
        呼び出しだけで、6 設定 + 話者一覧すべてを取得できる。

        config 操作(話者+5パラメータのいずれかの選択)が実行中の間はこのメソッドを
        何もせず終了する(advisor レビュー指摘: 課題4)。起動時・「再読み込み」クリック・
        話者 retry タイマーのいずれの呼び出し経路でも、書込み中の `self._config_snapshot`
        を古いスナップショットで無条件に上書きしてしまうと、書込み完了後に
        `_on_config_choice_selected` が当てるパッチ(実効値 or フォールバックした
        raw_value)より後にこの上書きが走った場合、確定させたはずの値が古い値へ
        巻き戻る可能性がある。config 操作専用の直列化ポリシー
        (`self._config_action_inflight` / `_run_generation_guarded_action`)と
        一貫性を保つため、実行中は素通りする。

        逆方向のレース(advisor レビューで追加指摘): この呼び出し開始時点では
        `_config_action_inflight` が False でも、`fetch_speakers()` /
        `fetch_config_snapshot()`(最大 5 秒 x 2 の subprocess)の実行中に config 選択が
        割り込み、書込み+反映確認まで完了してしまうことがありうる。その場合この
        fetch が返す payload は書込み前の状態を捉えた古いものであり、`done` が無条件に
        `self._config_snapshot` を上書きすると、確定させたばかりの値を巻き戻してしまう。
        `_refresh_status` と同じ「開始前に世代 token を控え、`done` で is_current()
        判定してから反映する」idiom を `self._config_action_generation` に対して適用し、
        fetch 実行中に config 操作が入った(=generation が進んだ)場合は payload を
        丸ごと破棄する。
        """
        if self._config_action_inflight:
            return

        token = self._config_action_generation.token()

        def work() -> Tuple[mc.SpeakersResult, mc.ConfigSnapshot]:
            speakers_result = mc.fetch_speakers()
            snapshot = mc.fetch_config_snapshot()
            return speakers_result, snapshot

        def done(payload: Tuple[mc.SpeakersResult, mc.ConfigSnapshot]) -> None:
            if not self._config_action_generation.is_current(token):
                # fetch 実行中に config 選択(書込み+反映確認)が割り込んだので、
                # この payload は古い可能性がある。確定済みの値を巻き戻さないよう破棄する。
                return
            speakers_result, snapshot = payload
            self._config_snapshot = snapshot
            current_speaker_id = mc.get_int_value(snapshot.raw, mc.SPEAKER_SETTING_KEY)
            self._apply_speakers_result(speakers_result, current_speaker_id)
            self._apply_scalar_submenus(snapshot)

        self._run_async(work, done)

    def _apply_speakers_result(
        self, speakers_result: mc.SpeakersResult, current_speaker_id: Optional[int]
    ) -> None:
        """話者一覧 + 現在値を既存 MenuItem 群へ差分反映する(呼び出しはメインスレッド前提)。"""
        self._speakers_result = speakers_result
        self._current_speaker_id = current_speaker_id

        if speakers_result.error:
            self._speaker_status_item.title = f"取得失敗: {speakers_result.error}"
            self._speaker_status_item.hidden = False
            self._hide_all_speaker_items()
            return

        entries = mc.build_speaker_menu_entries(speakers_result.speakers, current_speaker_id)
        if not entries:
            self._speaker_status_item.title = "話者が見つかりません"
            self._speaker_status_item.hidden = False
            self._hide_all_speaker_items()
            return

        self._speaker_status_item.hidden = True

        seen_ids = set()
        for entry in entries:
            seen_ids.add(entry.style_id)
            item = self._speaker_items.get(entry.style_id)
            if item is None:
                # 初めて観測した style_id のときだけ新規 MenuItem を作る
                # (=NSApp._ns_to_py_and_callback への新規登録も、この時だけ発生する)。
                item = rumps.MenuItem(
                    entry.label, callback=self._make_speaker_handler(entry.style_id)
                )
                self.speaker_submenu.insert_before(_SPEAKER_RELOAD_KEY, item)
                self._speaker_items[entry.style_id] = item
            item.title = entry.label
            item.state = 1 if entry.checked else 0
            item.hidden = False

        for style_id, item in self._speaker_items.items():
            if style_id not in seen_ids:
                item.hidden = True

    def _hide_all_speaker_items(self) -> None:
        for item in self._speaker_items.values():
            item.hidden = True

    def _make_speaker_handler(self, style_id: int) -> Callable[[rumps.MenuItem], None]:
        def handler(_sender: rumps.MenuItem) -> None:
            self._on_speaker_selected(style_id)

        return handler

    def _on_speaker_selected(self, style_id: int) -> None:
        def on_result(result: mc.SetSpeakerResult) -> None:
            if not result.ok:
                self._show_error(result.error or "既定話者の変更に失敗しました")
            elif result.warning:
                self._show_error(result.warning)
            else:
                self._clear_error()

            effective = (
                result.effective_speaker_id
                if result.effective_speaker_id is not None
                else self._current_speaker_id
            )
            # 話者一覧そのものは変わっていないので取り直さず、チェックマークだけ更新する。
            self._apply_speakers_result(self._speakers_result, effective)

        self._run_generation_guarded_action(lambda: mc.set_default_speaker(style_id), on_result)

    def _apply_scalar_submenus(self, snapshot: mc.ConfigSnapshot) -> None:
        for submenu in (
            self.volume_submenu,
            self.speed_submenu,
            self.intonation_submenu,
            self.pause_scale_submenu,
            self.max_chunks_submenu,
        ):
            submenu.apply(snapshot)

    def _on_config_choice_selected(self, setting_key: str, raw_value: str) -> None:
        def on_result(result: mc.SetConfigResult) -> None:
            if not result.ok:
                self._show_error(result.error or "設定の変更に失敗しました")
            elif result.warning:
                self._show_error(result.warning)
            else:
                self._clear_error()

            # config --list をもう一度叩き直さず、set_config_value() が返した
            # 実効値でローカルスナップショットを更新してからチェックマークだけ
            # 再計算する(話者選択と同じ「取り直さず差分反映」方針)。
            if result.ok:
                if result.effective_raw is not None:
                    self._config_snapshot.raw[setting_key] = result.effective_raw
                else:
                    # 書込み自体は成功したが、直後の config --list 再読が失敗し
                    # 実効値を確認できなかったケース(ok=True, warning 付き,
                    # effective_raw=None)。反映確認はできなかったが書込み自体は
                    # 成功しているので、ひとまず要求した raw_value を信じて
                    # ローカルスナップショットへ反映する(advisor レビュー指摘: 課題3)。
                    self._config_snapshot.raw[setting_key] = raw_value
            self._apply_scalar_submenus(self._config_snapshot)

        self._run_generation_guarded_action(
            lambda: mc.set_config_value(setting_key, raw_value), on_result
        )

    # ------------------------------------------------------------------
    # 後始末
    # ------------------------------------------------------------------

    def _cleanup(self) -> None:
        """Timer 停止 + flock 解放。多重呼び出しに対して冪等。

        rumps.events.before_quit フックとして登録される(applicationWillTerminate_
        経由で、quit_application() がどこから呼ばれても必ず実行される)。
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for t in (self.status_timer, self.speaker_retry_timer):
            try:
                t.stop()
            except Exception:  # noqa: BLE001 - 終了処理は握りつぶして続行
                pass
        self._lock.release()


def _install_signal_handlers() -> None:
    def _handler(signum: int, frame: object) -> None:  # noqa: ARG001
        rumps.quit_application()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    lock = mc.SingleInstanceLock()
    try:
        lock.acquire()
    except mc.LockError as e:
        sys.stderr.write(f"{e}\n")
        sys.stderr.write("既存の vvread menubar を終了してから再度実行してください。\n")
        return 1

    try:
        app = VvreadMenubarApp(lock)
        _install_signal_handlers()
        app.run()
    except Exception:
        # 通常はメニュー callback 内で例外は握りつぶされる(rumps 自体の挙動)ため
        # ここに到達するのは起動時エラー等の想定外ケースのみ。保険として flock を
        # 解放してから re-raise する(_cleanup が既に呼ばれていれば冪等)。
        lock.release()
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
