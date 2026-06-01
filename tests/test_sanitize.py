"""sanitize.py のテスト

辞書(WORD_KANA / EXTENSION_KANA)を触ったり、整形パイプラインの順序を
変えたりした時の事故を検出する目的のスモークテスト集。

辞書の登録に依存するケースは「現状の登録に対して期待する動作」を
そのまま固定するので、辞書を意図的に変えた場合は同時にここも更新する。
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import constants
import sanitize

REPO = Path(__file__).resolve().parent.parent
SANITIZE_SCRIPT = REPO / "scripts" / "sanitize.py"


# ---------- helper ----------

def _run_main_with_input(text: str, argv: list[str]) -> str:
    """sanitize.main() を stdin 経由で叩き、stdout を文字列で返す"""
    saved_stdin, saved_stdout, saved_argv = sys.stdin, sys.stdout, sys.argv
    try:
        sys.stdin = io.StringIO(text)
        sys.stdout = io.StringIO()
        sys.argv = argv
        sanitize.main()
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout, sys.argv = saved_stdin, saved_stdout, saved_argv


# ---------- sanitize() 全体 ----------


class TestSanitizePassthrough:
    """整形しても変化しない or 期待通りに整形されるベーシックケース"""

    def test_short_japanese_passes_through(self):
        assert sanitize.sanitize("こんにちは") == "こんにちは"

    def test_collapses_multiple_spaces(self):
        assert sanitize.sanitize("a    b") == "a b"

    def test_collapses_multiple_newlines_to_one(self):
        # 改行は chunk 分割のヒントとして 1 つだけ残す
        assert sanitize.sanitize("a\n\n\nb") == "a\nb"

    def test_strips_html_tags(self):
        # タグは空文字置換(空白挿入はしない)
        assert sanitize.sanitize("hello<br>world") == "helloworld"


class TestExpandRuby:
    """expand_ruby() — ルビ展開の直接テストと PIPELINE 統合確認"""

    @pytest.mark.parametrize(
        "src,expected",
        [
            # 基本: <rt> 読みに置換
            ("<ruby>漢字<rt>かんじ</rt></ruby>", "かんじ"),
            ("<ruby>東京都<rt>とうきょうと</rt></ruby>", "とうきょうと"),
            # 属性付き <ruby>
            ('<ruby class="highlight">言葉<rt>ことば</rt></ruby>', "ことば"),
            # 属性付き <rt>
            ('<ruby>漢字<rt lang="ja">かんじ</rt></ruby>', "かんじ"),
            # <rb> を含む形式
            ("<ruby><rb>難読</rb><rt>なんどく</rt></ruby>", "なんどく"),
            # <rp> を含む形式（フォールバック括弧）
            ("<ruby>言葉<rp>(</rp><rt>ことば</rt><rp>)</rp></ruby>", "ことば"),
            # <rt> 内に <span> 等のタグ
            ("<ruby>漢字<rt><span>かんじ</span></rt></ruby>", "かんじ"),
            # 複数 <rt>: 音節ごとに分割されたケース
            ("<ruby>亜<rt>あ</rt>米<rt>め</rt>利<rt>り</rt>加<rt>か</rt></ruby>", "あめりか"),
            # 大文字タグ
            ("<RUBY>漢字<RT>かんじ</RT></RUBY>", "かんじ"),
            # HTML entity
            ("<ruby>A&amp;B<rt>えー&amp;びー</rt></ruby>", "えー&びー"),
            # 前後テキストあり
            ("本文<ruby>漢字<rt>かんじ</rt></ruby>終了", "本文かんじ終了"),
            # 複数 ruby が同じテキストに
            (
                "<ruby>東京<rt>とうきょう</rt></ruby>と<ruby>大阪<rt>おおさか</rt></ruby>",
                "とうきょうとおおさか",
            ),
            # <rt> なし → inner テキストのみ残す
            ("<ruby>漢字</ruby>", "漢字"),
            # ruby なし → 変化なし
            ("普通のテキスト", "普通のテキスト"),
        ],
    )
    def test_expand_ruby(self, src, expected):
        assert sanitize.expand_ruby(src) == expected

    def test_pipeline_expands_ruby(self):
        out = sanitize.sanitize("<ruby>漢字<rt>かんじ</rt></ruby>")
        assert out == "かんじ"
        assert "<ruby>" not in out

    def test_pipeline_no_rt_strips_tags(self):
        out = sanitize.sanitize("<ruby>漢字</ruby>")
        assert "<ruby>" not in out
        assert "漢字" in out

    def test_unclosed_ruby_not_recovered(self):
        # 閉じタグ欠落は expand_ruby では復旧しない（remove_html_tags が処理）
        out = sanitize.sanitize("<ruby>漢字<rt>かんじ</rt>")
        assert "<ruby>" not in out
        assert "<rt>" not in out


class TestSanitizeCodeAndUrl:
    def test_fenced_code_block_replaced(self):
        text = "前\n```\nfoo()\nbar()\n```\n後"
        assert "コードブロック省略" in sanitize.sanitize(text)
        assert "foo()" not in sanitize.sanitize(text)

    def test_url_in_plain_text_replaced(self):
        assert "URL省略" in sanitize.sanitize("詳細は https://example.com/foo を参照")

    def test_inline_code_url_replaced(self):
        assert "URL省略" in sanitize.sanitize("`https://example.com`")

    def test_inline_code_japanese_only_kept(self):
        assert "テスト" in sanitize.sanitize("`テスト`")

    def test_inline_code_too_long_becomes_command(self):
        long = "a" * (sanitize.INLINE_CODE_LIMIT + 5)
        assert "コマンド" in sanitize.sanitize(f"`{long}`")

    def test_inline_code_hex_hash_replaced_with_hash_label(self):
        # コミットハッシュ風の hex は逐字カナ化せず「ハッシュ」と固定で読む
        # cache_patterns 側でこの形を捕捉して cache 化するため
        assert "ハッシュ" in sanitize.sanitize("`95db2a5` で commit。")
        assert "ハッシュ" in sanitize.sanitize("`fec9b58` で commit 完了。")
        assert "キュウゴ" not in sanitize.sanitize("`95db2a5`")  # 逐字カナ化されない

    def test_inline_code_short_hex_still_kanaized(self):
        # 5 文字以下の hex は短すぎてハッシュではない → 通常のカナ化に戻る
        out = sanitize.sanitize("`abc12`")
        assert "ハッシュ" not in out

    def test_inline_code_long_path_with_extension_becomes_file_label(self):
        # 25 文字を超えるインラインコードが、末尾に登録済み拡張子を持つ場合は
        # 「コマンド」ではなく「ファイル」と短く読む(パス内容自体は読み上げない)
        long_path = "doc/worklog/2026-05-01-t005-t003-voice-tests.md"
        assert len(long_path) > sanitize.INLINE_CODE_LIMIT
        out = sanitize.sanitize(f"`{long_path}` を更新")
        assert "ファイル" in out
        assert "コマンド" not in out
        # ファイル名そのものは読み上げられない(短縮読み)
        assert "ワークログ" not in out
        assert "ドットエムディー" not in out

    def test_inline_code_short_path_with_extension_keeps_segments(self):
        # 25 文字以内なら従来通り read_as_kana が動き、各セグメントが読まれる
        out = sanitize.sanitize("`scripts/voice.sh` を編集")
        assert "コマンド" not in out
        assert "ファイル" not in out
        assert "ドットエスエイチ" in out  # .sh が拡張子として読まれる

    def test_inline_code_long_command_without_extension_remains_command(self):
        # 25 文字超で拡張子無しなら従来通り「コマンド」
        cmd = "pytest tests/test_sanitize.py -v -k some_long_filter"
        assert len(cmd) > sanitize.INLINE_CODE_LIMIT
        out = sanitize.sanitize(f"`{cmd}` 実行")
        assert "コマンド" in out
        assert "ファイル" not in out

    def test_inline_code_unknown_extension_falls_back_to_command(self):
        # EXTENSION_KANA に未登録の拡張子 (.foo) はファイル扱いされず「コマンド」になる
        bogus = "doc/worklog/2026-05-01-t005-t003-voice-tests.foo"
        assert len(bogus) > sanitize.INLINE_CODE_LIMIT
        out = sanitize.sanitize(f"`{bogus}` を更新")
        assert "コマンド" in out
        assert "ファイル" not in out


# T-009: 地の文(バッククォート無し)の hash / path 読み飛ばし
class TestSanitizeBareHashAndPath:
    """地の文に直接 hex ハッシュ・絶対パスが現れた時の短縮動作を検証する。

    インラインコード版は TestSanitizeCodeAndUrl でカバー済み(「ハッシュ」「ファイル」
    総称化)。地の文は文脈を持つので tail のみ残す方針(T-009 設計判断)。
    """

    # ---- transform_bare_hashes ----

    def test_bare_hex_hash_replaced(self):
        out = sanitize.sanitize("コミット 0bfebe5 を確認してください。")
        assert "ハッシュ" in out
        assert "0bfebe5" not in out
        # 逐字読みされていないことの床線
        assert "ゼロブフェベゴ" not in out

    def test_bare_full_sha_replaced(self):
        sha = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9012"
        out = sanitize.sanitize(f"SHA は {sha} です。")
        assert "ハッシュ" in out
        assert sha not in out

    def test_bare_alpha_only_hex_not_replaced(self):
        # alpha-only(数字無し)は `_is_commit_hash` の「数字 + a-f 混在」判定で除外
        out = sanitize.sanitize("abcdef のハッシュ値は無視してください。")
        assert "abcdef" in out  # そのまま残る
        assert out.count("ハッシュ") == 1  # 元文の「ハッシュ値」分のみ

    def test_bare_digit_only_not_replaced(self):
        # digit-only(a-f 無し)も同条件で除外
        out = sanitize.sanitize("番号 1234567 を控える。")
        assert "1234567" in out
        assert "ハッシュ" not in out

    def test_bare_short_hex_not_replaced(self):
        # 5 文字以下の hex は短すぎてハッシュ判定外(BARE_HASH_PATTERN が拾わない)
        out = sanitize.sanitize("コード a1b2c を入力。")
        assert "ハッシュ" not in out

    def test_bare_hash_in_word_not_matched(self):
        # 連結語(`xxx0bfebe5xxx`)は `\b` 境界で弾かれる
        out = sanitize.sanitize("xxx0bfebe5xxx は一塊の文字列。")
        assert "ハッシュ" not in out
        assert "0bfebe5" in out

    # ---- transform_bare_paths ----

    def test_bare_long_absolute_path_keeps_only_tail(self):
        # /Users/.../sanitize.py → tail のみ残し、後段 transform_filenames がカナ化
        out = sanitize.sanitize(
            "ファイルは /Users/alice/projects/myapp/scripts/sanitize.py にあります。"
        )
        assert "/Users" not in out
        assert "projects" not in out
        assert "myapp" not in out
        # 末尾の sanitize.py がカナ化されていること
        assert "サニタイズドットパイ" in out

    def test_bare_three_segment_absolute_path(self):
        # 最小ケース: 3 セグメント /var/log/system.log
        out = sanitize.sanitize("ログは /var/log/system.log に出力されます。")
        assert "/var" not in out
        assert "/log" not in out  # path 中の log
        # 末尾の system.log がカナ化されていること
        assert "システムドットログ" in out

    def test_bare_home_path_supported(self):
        # ~/projects/foo/bar/baz/quux.txt → tail のみ
        out = sanitize.sanitize("~/projects/foo/bar/baz/quux.txt を編集します。")
        assert "projects" not in out
        assert "/foo" not in out
        assert "/bar" not in out
        # tail は transform_filenames でカナ化される(.txt → ドットテキスト)
        assert "ドットテキスト" in out

    def test_bare_dotfile_home_path_supported(self):
        # ~/.config/nvim/init.lua のように第 1 セグメントが `.` 始まり(dotfile)でも
        # tail のみ残す。`[A-Za-z.]` 緩和で対応(T-009 完了後の補強)
        out = sanitize.sanitize("設定は ~/.config/nvim/init.lua にあります。")
        assert ".config" not in out
        assert "/nvim" not in out
        # tail の init.lua がカナ化されて「イニットドットルア」になる
        assert "イニットドットルア" in out

    def test_bare_two_segment_path_not_matched(self):
        # 2 セグメント /var/log は短いので変換しない(深さ閾値 ≥ 3)。
        # bare_paths は発火しないため、`/var/` のスラッシュ構造が保持される
        # (内部の `log` は WORD_KANA に登録があり transform_dict_words でカナ化)
        out = sanitize.sanitize("ディレクトリは /var/log です。")
        assert "/var" in out  # path 構造保持(slash 削除されていない)

    def test_bare_path_with_numeric_first_segment_not_matched(self):
        # 第 1 セグメントが英字始まりでない場合は除外(transform_dates 漏れの保険)
        # `/2026/05/03` が来てもパス扱いしない
        out = sanitize.sanitize("通知 /2026/13/99 を表示。")
        # "13" / "99" は invalid なので transform_dates も発火せず、パス変換も発火しない
        assert "/2026" in out

    def test_bare_path_does_not_grab_japanese_punctuation(self):
        # 末尾が日本語句読点に隣接 → 句読点は match に巻き込まれない
        out = sanitize.sanitize("/var/log/system.log。次の行。")
        assert "。" in out  # 句読点保持

    def test_bare_path_in_middle_of_word_not_matched(self):
        # `aaa/bbb/ccc/ddd` のような連結 path-like(先頭が単語文字)は除外
        out = sanitize.sanitize("aaa/bbb/ccc/ddd は通常の文字列。")
        assert "aaa/bbb/ccc/ddd" in out

    def test_bare_path_then_hash_in_pipeline(self):
        # path 末尾が hex hash の場合: path 変換で tail 残し → hash 変換で 「ハッシュ」化
        out = sanitize.sanitize("成果物は /var/cache/builds/0bfebe5abc12 にある。")
        assert "ハッシュ" in out
        assert "/var" not in out

    def test_date_with_slash_not_matched_as_path(self):
        # `2026/05/03` は transform_dates が先に消費 → パス変換に流れない床線
        out = sanitize.sanitize("予定日は 2026/05/03 です。")
        assert "二千二十六年" in out
        assert "/05/03" not in out


class TestSanitizePlainTextEnglish:
    """地の文の英字パターン(transform_filenames + transform_dict_words)"""

    def test_filename_with_extension_is_kanaized(self):
        # CLAUDE.md は拡張子付きパターン → read_as_kana → クロード + ドット + エムディー
        out = sanitize.sanitize("CLAUDE.md を確認")
        assert "クロード" in out
        assert "CLAUDE" not in out
        assert ".md" not in out

    def test_inline_filename_matches_plain_filename(self):
        # バッククォート有無で出力が変わらないこと(後方互換)
        plain = sanitize.sanitize("CLAUDE.md")
        backticked = sanitize.sanitize("`CLAUDE.md`")
        assert plain == backticked

    def test_dict_word_in_plain_text_replaced(self):
        # Claude(辞書登録あり)が地の文でカナ化される
        out = sanitize.sanitize("Claude が応答した")
        assert "クロード" in out
        assert "Claude" not in out

    def test_dict_word_case_insensitive(self):
        for src in ["Claude", "CLAUDE", "claude"]:
            assert "クロード" in sanitize.sanitize(f"{src} 起動")

    def test_filename_consumed_before_dict_word(self):
        # CLAUDE.md → ファイル名として丸ごと消費される。後段の dict 置換で
        # CLAUDE が単独に残らない(=「クロード.md」のような中途半端にならない)
        out = sanitize.sanitize("Claude は CLAUDE.md を読む")
        assert out.count("クロード") >= 2  # 裸の Claude + CLAUDE.md 内の CLAUDE


class TestSanitizeMarkdown:
    def test_heading_marks_stripped(self):
        assert sanitize.sanitize("## 見出し") == "見出し"

    def test_list_marks_stripped(self):
        out = sanitize.sanitize("- 項目1\n- 項目2")
        assert "-" not in out

    def test_emphasis_marks_stripped(self):
        assert sanitize.sanitize("**強調**") == "強調"


# G-1: strip_markdown_links の単体テスト(直接呼び出し)
class TestStripMarkdownLinks:
    """`[text](url)` / `![alt](url)` の置換挙動を直接検証する。

    PIPELINE の他ステップ(URL 削除等)を介さずに、リンク剥がし規則だけを
    純粋にテストする。
    """

    def test_link_replaced_with_text(self):
        assert sanitize.strip_markdown_links("詳細は[ドキュメント](https://example.com)を参照") \
            == "詳細はドキュメントを参照"

    def test_image_replaced_with_alt(self):
        assert sanitize.strip_markdown_links("![ロゴ](https://img.example.com/logo.png)") == "ロゴ"

    def test_image_pattern_consumed_before_link(self):
        # `![alt](url)` のうち先頭 `!` が無いと link としてマッチして alt 部分が残るが、
        # `!` 付きを先に処理しているため画像は alt のみ残る
        out = sanitize.strip_markdown_links("![画像](url1)と[リンク](url2)")
        assert out == "画像とリンク"

    def test_empty_text_link(self):
        assert sanitize.strip_markdown_links("[](https://example.com)") == ""

    def test_empty_alt_image(self):
        assert sanitize.strip_markdown_links("![](https://img.example.com/x.png)") == ""

    def test_multiple_links_in_one_line(self):
        out = sanitize.strip_markdown_links("[A](u1)と[B](u2)と[C](u3)")
        assert out == "AとBとC"

    def test_text_without_link_unchanged(self):
        assert sanitize.strip_markdown_links("普通のテキスト") == "普通のテキスト"

    def test_unmatched_brackets_unchanged(self):
        # `[text]` だけ(URL カッコ無し)はリンクではないので触らない
        assert sanitize.strip_markdown_links("[残す]") == "[残す]"

    def test_full_pipeline_drops_link_url(self):
        # sanitize() 全体を通すと link URL も丸ごと消えてラベルだけが残る
        out = sanitize.sanitize("[ドキュメント](https://example.com)を見る")
        assert "ドキュメント" in out
        assert "https" not in out
        assert "example.com" not in out


# G-2: strip_table_separators の単体テスト(直接呼び出し)
class TestStripTableSeparators:
    """Markdown テーブルの区切り行(`|---|---|`)削除と `|` の空白置換を直接検証する。"""

    def test_separator_row_deleted(self):
        # `|---|---|` 行は削除される
        text = "| 列1 | 列2 |\n|-----|-----|\n| A | B |"
        out = sanitize.strip_table_separators(text)
        assert "-----" not in out
        # ヘッダ・データ行は残るが `|` は空白に置換
        assert "列1" in out
        assert "A" in out
        assert "|" not in out

    def test_alignment_separator_deleted(self):
        # `| :--- | ---: | :---: |`(alignment 指定)も削除される
        text = "| L | R | C |\n| :--- | ---: | :---: |\n| a | b | c |"
        out = sanitize.strip_table_separators(text)
        assert ":---" not in out
        assert "---:" not in out

    def test_separator_without_outer_pipes_deleted(self):
        # `---|---|---` のような外側 `|` 無しの区切り行も削除される
        text = "A | B | C\n---|---|---\nx | y | z"
        out = sanitize.strip_table_separators(text)
        assert "---" not in out

    def test_pipe_replaced_with_space(self):
        # 区切り行が無くても `|` は空白に置換される
        assert sanitize.strip_table_separators("a|b|c") == "a b c"

    def test_text_without_pipe_unchanged(self):
        assert sanitize.strip_table_separators("普通の段落") == "普通の段落"

    def test_full_pipeline_table_becomes_readable(self):
        # sanitize() 全体を通すと区切り行が消えて、セルがスペース区切りで読み上げ可能に
        text = "| 名前 | 値 |\n|------|----|\n| α | 1 |"
        out = sanitize.sanitize(text)
        assert "------" not in out
        assert "|" not in out


# G-3: transform_dates の境界(2 桁年 PIVOT、`/` 区切り、月日のみ除外)
class TestTransformDatesBoundary:
    """`transform_dates` / `_normalize_year` の境界条件を狙うテスト。"""

    def test_two_digit_year_below_pivot_maps_to_2000s(self):
        # PIVOT = 45。44 は 2044 に解釈
        out = sanitize.sanitize("44-01-02")
        assert "二千四十四年" in out

    def test_two_digit_year_at_pivot_maps_to_1900s(self):
        # 45 ぴったりは 1945(>= PIVOT は 19xx)
        out = sanitize.sanitize("45-01-02")
        assert "千九百四十五年" in out

    def test_two_digit_year_above_pivot_maps_to_1900s(self):
        # 46 は 1946
        out = sanitize.sanitize("46-12-31")
        assert "千九百四十六年" in out

    def test_slash_separator_recognized(self):
        # `/` 区切りも `-` と同じく受ける
        out = sanitize.sanitize("2026/05/02")
        assert "二千二十六年" in out
        assert "五月" in out
        assert "二日" in out

    def test_mixed_separator_rejected(self):
        # `\2` で同一区切りを要求しているため、`-` と `/` の混在は日付として認識しない
        out = sanitize.sanitize("2026-05/02")
        # 日付として漢数字化されない
        assert "二千二十六年" not in out

    def test_month_day_only_pair_not_converted(self):
        # `1/2` のような 2 つ組は比率や分数と紛らわしいので transform_dates の対象外
        # (transform_month_day で `/2` は対象外、`1月`/`2日` の文字が無いので無風)
        out = sanitize.sanitize("比率は 1/2 です")
        # 数字は残る
        assert "1" in out

    def test_normalize_year_at_pivot(self):
        assert sanitize._normalize_year("45") == 1945

    def test_normalize_year_below_pivot(self):
        assert sanitize._normalize_year("44") == 2044

    def test_normalize_year_4digit_passthrough(self):
        # 4 桁はそのまま int 化(PIVOT 補完なし)
        assert sanitize._normalize_year("2026") == 2026
        assert sanitize._normalize_year("1999") == 1999

    def test_zero_padded_month_day_recognized(self):
        # 0 埋めの `05`、無し `5` の両方を受ける
        out1 = sanitize.sanitize("2026-05-02")
        out2 = sanitize.sanitize("2026-5-2")
        assert "五月" in out1 and "五月" in out2
        assert "二日" in out1 and "二日" in out2

    def test_invalid_month_rejected(self):
        # 月 13 は 1〜12 範囲外で日付として認識しない
        out = sanitize.sanitize("2026-13-01")
        assert "二千二十六年" not in out
        assert "十三月" not in out

    # T-012: 空白入りスラッシュ日付
    @pytest.mark.parametrize(
        "src",
        [
            "2026 / 05 / 02",         # 半角空白
            "2026 /05/ 02",           # 部分的に空白
            "2026　/　05　/　02",     # 全角空白
            "2026/ 05 /02",           # 不揃い
            "2026 - 05 - 02",         # ハイフンも対象
        ],
    )
    def test_spaced_separator_date_to_kanji(self, src):
        out = sanitize.sanitize(src)
        assert "二千二十六年" in out
        assert "五月" in out
        assert "二日" in out
        assert "2026" not in out

    def test_spaced_separator_keeps_mixed_rejection(self):
        # T-012 拡張後も `-` と `/` の混在は日付として認識しない(\2 で同一区切り強制)
        out = sanitize.sanitize("2026 - 05 / 02")
        assert "二千二十六年" not in out


class TestSanitizeDates:
    def test_yyyy_mm_dd_to_kanji(self):
        out = sanitize.sanitize("2026-04-30 に作業")
        assert "二千二十六年" in out
        assert "四月" in out
        assert "三十日" in out
        assert "2026" not in out

    def test_year_only_to_kanji(self):
        out = sanitize.sanitize("2026年")
        assert "二千二十六年" in out

    def test_month_day_to_kanji(self):
        # 「5月」が「ゴツキ」と読まれる問題への対応
        assert "五月" in sanitize.sanitize("5月")


class TestSanitizeCounterSpace:
    """transform_counter_space: 数字+空白+助数詞 の空白を削る(B-010)

    VOICEVOX は「1 件」のように間に半角空白があると音便化に失敗するため、
    sanitize 段階で空白を削って「1件」→ イッケン と読ませる。
    """

    @pytest.mark.parametrize(
        "src,expected_substr",
        [
            ("1 件", "1件"),
            ("1 つ", "1つ"),
            ("10 個", "10個"),
            ("3 人", "3人"),
        ],
    )
    def test_space_between_digit_and_counter_removed(self, src, expected_substr):
        assert expected_substr in sanitize.transform_counter_space(src)

    def test_bunme_space_removed_by_general_pattern(self):
        # B-011 Phase 3: GENERAL パターン(数字+空白+漢字)が「1 文目」の空白も
        # 吸収する。最終的に「文目」→「ブンメ」のカナ変換は PIPELINE 順序で先に
        # 実行される transform_bunme が担うため、フルパイプラインでは「一ブンメ」
        # に到達する(test_bunme_full_pipeline 参照)。
        # ここは transform_counter_space 単体としての挙動を固定する。
        assert sanitize.transform_counter_space("1 文目") == "1文目"

    # B-011 Phase 3: COUNTER_CHARS 未登録の助数詞・単位を一般化(漢字 / カタカナ連続)
    @pytest.mark.parametrize(
        "src,expected",
        [
            # 単一漢字の助数詞(COUNTER_CHARS 外)
            ("3 期連続", "3期連続"),
            ("1 泊", "1泊"),
            ("1 袋", "1袋"),
            # 複数漢字の単位語
            ("1 営業日", "1営業日"),
            ("1 四半期", "1四半期"),
            ("第 2 土曜日", "第 2土曜日"),  # 「2 土曜日」の空白だけ消える(第の前は別問題)
            # カタカナ単位
            ("12 パーセント", "12パーセント"),
            ("5 メートル", "5メートル"),
            ("3 キロ", "3キロ"),
            # 全角空白でもマッチ
            ("3　期連続", "3期連続"),
        ],
    )
    def test_general_pattern_removes_space_for_kanji_or_katakana(self, src, expected):
        assert sanitize.transform_counter_space(src) == expected

    # B-011 Phase 3: 過剰マッチ防止(advisor 指摘)
    # ※ T-011 で `5 cm` / `5 km` は ASCII パターン側で吸収するように移動した
    #   (TestSanitizeCounterSpaceAscii 参照)
    @pytest.mark.parametrize(
        "src",
        [
            "5 と 6",          # と は hiragana なので非マッチ
            "5 から 10",       # から も hiragana
            "100 100",         # 後続が数字のみ(漢字でもカタカナでもない)
            "数字 5",          # 後ろに対象語がない(末尾)
        ],
    )
    def test_general_pattern_does_not_match_non_japanese_followers(self, src):
        # 入力が改変されないことを固定。誤マッチで「100 100」が「100100」に
        # ならないこと等を保証する
        assert sanitize.transform_counter_space(src) == src


class TestSanitizeCounterSpaceAscii:
    """T-011: ASCII 単位(cm / km / GB 等)の空白除去

    COUNTER_SPACE_GENERAL_PATTERN(CJK + カタカナのみ)で吸収できない
    ASCII 単位を別パターンで吸収する。ホワイトリスト + negative lookahead
    で「5 minutes」のような英単語を誤マッチしないことを保証する。
    """

    @pytest.mark.parametrize(
        "src,expected",
        [
            # 長さ
            ("5 cm", "5cm"),
            ("10 mm", "10mm"),
            ("3 km", "3km"),
            # 重量
            ("5 kg", "5kg"),
            ("10 mg", "10mg"),
            # 容量
            ("5 GB", "5GB"),
            ("10 MB", "10MB"),
            ("100 KB", "100KB"),
            ("1 TB", "1TB"),
            # 時間
            ("100 ms", "100ms"),
            # 周波数
            ("60 Hz", "60Hz"),
            ("4 kHz", "4kHz"),
            ("2 MHz", "2MHz"),
            # 全角空白
            ("5　cm", "5cm"),
            # 文中
            ("距離は5 km です", "距離は5km です"),
        ],
    )
    def test_ascii_unit_space_removed(self, src, expected):
        assert sanitize.transform_counter_space(src) == expected

    @pytest.mark.parametrize(
        "src",
        [
            "5 minutes",       # m + inutes(連結語)
            "5 stages",        # s 単独不採用なので m と紛らわしい入口がそもそも無いが念のため
            "5 cmd",           # cm + d(連結語)
            "5 GBP",           # GB + P(連結語)
            "5 KBytes",        # KB + ytes(連結語)
            "5 msg",           # ms + g(連結語)
            "version 1 stable",  # 数字+英単語の通常表現
            "5 m",             # 1 文字単位はホワイトリスト外
            "5 s",             # 同上
            "5 g",             # 同上
        ],
    )
    def test_ascii_unit_does_not_match_concatenations_or_unlisted(self, src):
        # negative lookahead `(?![A-Za-z0-9])` とホワイトリスト未登録で誤マッチを防ぐ
        assert sanitize.transform_counter_space(src) == src

    def test_full_pipeline_ascii_unit_normalized(self):
        # PIPELINE 全体を通しても期待通り空白除去される
        out = sanitize.sanitize("ファイルサイズは5 GB です。")
        assert "5GB" in out
        assert "5 GB" not in out


class TestSanitizeBunme:
    """transform_bunme: <数字>文目 → <漢数字>ブンメ(B-010 Phase 2)

    VOICEVOX は「文目」を「アヤメ」と誤読するので、Phase 1 の空白除去では救えない。
    数字を漢数字化、「文目」を「ブンメ」(カタカナ)に置き換えて正しく読ませる。
    """

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("1文目", "一ブンメ"),
            ("2文目", "二ブンメ"),
            ("10文目", "十ブンメ"),
            ("23文目", "二十三ブンメ"),
            ("100文目", "百ブンメ"),
            # 空白あり/全角/連続にも対応
            ("1 文目", "一ブンメ"),
            ("1　文目", "一ブンメ"),
            ("1   文目", "一ブンメ"),
        ],
    )
    def test_bunme_pattern_replaced(self, src, expected):
        assert sanitize.transform_bunme(src) == expected

    def test_in_context(self):
        assert sanitize.transform_bunme("最初の1文目を確認") == "最初の一ブンメを確認"
        assert sanitize.transform_bunme("1 文目から3 文目まで") == "一ブンメから三ブンメまで"

    def test_does_not_match_without_leading_digit(self):
        # 「文目」単体や、数字が直前に無いケースは触らない
        assert sanitize.transform_bunme("最後の文目") == "最後の文目"
        assert sanitize.transform_bunme("文目を確認") == "文目を確認"

    def test_full_pipeline_bunme_normalized(self):
        # sanitize() 全体を通して「1 文目」が「一ブンメ」に置換される
        out = sanitize.sanitize("1 文目を確認")
        assert "一ブンメ" in out
        assert "文目" not in out
        assert "1 文目" not in out

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("修正は1 件です", "修正は1件です"),
            ("10 個の修正", "10個の修正"),
            ("残り3 つ", "残り3つ"),
            ("1 ヶ月後", "1ヶ月後"),  # 複合(ヶ月)も先頭の「ヶ」で発火して空白除去
            ("1 番目", "1番目"),       # 番目: 番で発火、目は残るが VOICEVOX が正しく読む
            ("1 回目", "1回目"),
            ("1 時間", "1時間"),       # 時間: 時で発火
            ("1 週間", "1週間"),       # 週間: 週で発火
        ],
    )
    def test_in_context(self, src, expected):
        assert sanitize.transform_counter_space(src) == expected

    @pytest.mark.parametrize(
        "src",
        [
            "2 + 3 = 5",            # 数字+空白+演算記号は対象外
            "version 1 stable",      # 数字+空白+英単語は対象外
            "10 ° の角度",            # 数字+空白+(° は COUNTER_CHARS に無い)
        ],
    )
    def test_non_counter_followers_untouched(self, src):
        # COUNTER_CHARS / GENERAL / ASCII のいずれにも該当しない文字が続く場合は空白を残す
        # ※ `5 GB` は T-011 で ASCII パターン側に移動した
        assert sanitize.transform_counter_space(src) == src

    def test_already_no_space_unchanged(self):
        # 空白なしの「1件」はそのまま
        assert sanitize.transform_counter_space("1件の修正") == "1件の修正"

    def test_full_width_space_also_removed(self):
        # 全角空白(U+3000)もマッチして除去される(Claude が稀に挿入する想定)
        assert sanitize.transform_counter_space("1　件") == "1件"

    def test_multiple_spaces_collapsed(self):
        # 連続空白もまとめて除去
        assert sanitize.transform_counter_space("1   件") == "1件"

    def test_does_not_collide_with_year_month_day(self):
        # transform_year / transform_month_day と PIPELINE 順で衝突しないこと:
        # 空白なしの「2026年5月2日」は year/month_day で漢数字化される。
        # その後 transform_counter_space に届いても、数字が消えているので発火せず
        # 二重処理にならない(= COUNTER_CHARS に「年/月/日」を入れない方針の検証)
        out = sanitize.sanitize("2026年5月2日")
        assert "二千二十六年" in out
        assert "2026" not in out

    def test_year_with_space_kanji_normalized(self):
        # B-011 Phase 3 で `[ 　]*年` に拡張、空白入りでも漢数字化される
        out = sanitize.sanitize("2026 年に作業")
        assert "二千二十六年" in out
        assert "2026" not in out
        assert "2026 年" not in out

    def test_full_pipeline_counter_normalized(self):
        # sanitize() 全体を通すと「1 件」が「1件」になって出力される
        out = sanitize.sanitize("テストは1 件 PASS。")
        # 「1 件」の空白が消えている(「件」の前に空白が無い)
        assert "1件" in out
        assert "1 件" not in out

    def test_pipeline_year_month_day_with_spaces(self):
        # B-011 Phase 3 統合テスト(advisor 案):
        # transform_dates → transform_year → transform_month_day → ... の
        # PIPELINE 順序が、空白入り年月日 + ハイフン日付の混在ケースで全て揃って
        # 漢数字化されることを固定する。将来 PIPELINE 順序を弄った時の回帰検出器。
        raw = "2026 年 5 月 2 日に、5 月の予算と 2026-05-02 の予定を見直す。"
        out = sanitize.sanitize(raw)
        # YEAR_PATTERN 拡張で「2026 年」→「二千二十六年」
        assert "二千二十六年" in out
        # MONTH_DAY_PATTERN 拡張で「5 月」→「五月」、「2 日」→「二日」
        assert "五月" in out
        assert "二日" in out
        # transform_dates が「2026-05-02」を漢数字化していること
        # (空白入り変換の追加で副作用が無いことの確認)
        assert "二千二十六年五月二日" in out
        # 元の数字+空白パターンが残っていないこと
        assert "2026" not in out
        assert "5 月" not in out
        assert "2 日" not in out


# ---------------------------------------------------------------------------
# B-011 Phase 4: prefix-digit / 同形異音
# ---------------------------------------------------------------------------


class TestSanitizePrefixDigit:
    """transform_prefix_digit: 「第 + 空白 + 数字」の前空白を除去。
    後段の transform_counter_space と組み合わせて「第N単位」を一気読みさせる。"""

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("第 1 四半期", "第1 四半期"),
            ("第 2 土曜日", "第2 土曜日"),
            ("第　1　四半期", "第1　四半期"),  # 全角空白も除去対象だが、後ろの空白は別
        ],
    )
    def test_prefix_digit_space_removed(self, src, expected):
        assert sanitize.transform_prefix_digit(src) == expected

    @pytest.mark.parametrize(
        "src",
        [
            "第1四半期",          # 既に空白なしなら変化なし
            "第一四半期",         # 漢数字なら変化なし(\d 不一致)
            "前第 章",            # 「第」直後が数字以外なら触らない
            "第 章",              # 同上
        ],
    )
    def test_prefix_digit_does_not_match_other_forms(self, src):
        assert sanitize.transform_prefix_digit(src) == src

    def test_pipeline_prefix_digit_collapses_to_compact(self):
        # advisor 案の PIPELINE 統合テスト:
        # 「第 1 四半期」が「第1四半期」まで圧縮される(prefix_digit + counter_space general 連携)
        out = sanitize.sanitize("第 1 四半期の業績は")
        assert "第1四半期" in out
        assert "第 1" not in out
        assert "1 四半期" not in out


class TestSanitizeAnoKata:
    """transform_ano_kata: 「あの方」を「あのかた」に置換(VOICEVOX が「ホオ」と
    誤読するため)。ただし「方法 / 方向 / 方面 / 方々」は別語として保護する。"""

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("あの方の", "あのかたの"),
            ("あの方は", "あのかたは"),
            ("あの方が", "あのかたが"),
            ("あの方。", "あのかた。"),
            ("あの方を呼ぶ", "あのかたを呼ぶ"),
        ],
    )
    def test_ano_kata_replaced(self, src, expected):
        assert sanitize.transform_ano_kata(src) == expected

    @pytest.mark.parametrize(
        "src",
        [
            "あの方法は良い",   # 方法 → 触らない
            "あの方向に進む",   # 方向
            "あの方面では",     # 方面
            "あの方々が集まる",  # 方々
        ],
    )
    def test_ano_kata_protected_compounds(self, src):
        assert sanitize.transform_ano_kata(src) == src


class TestSanitizeOkome:
    """transform_okome: 「お米」を「おこめ」に置換(VOICEVOX が「オベエ」と誤読)。
    複合語(お米屋等)も hiragana 化される(VOICEVOX 側で自然に読める)。"""

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("お米を買う", "おこめを買う"),
            ("お米", "おこめ"),
            ("お米屋", "おこめ屋"),  # 複合語も hiragana 化、advisor 確認済み
        ],
    )
    def test_okome_replaced(self, src, expected):
        assert sanitize.transform_okome(src) == expected


class TestSanitizeMonaka:
    """transform_monaka: 「最中」を文脈で「もなか / さいちゅう」と読み分けられない
    問題の救済。「最中を + 食/かじ/噛/割/くず」のステム交替で「もなか」化。"""

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("最中をかじる", "もなかをかじる"),
            ("最中を食べる", "もなかを食べる"),
            ("最中を噛む", "もなかを噛む"),
            # T-010: 観測ベースで追加した動詞ステム
            ("最中を割る音が響く", "もなかを割る音が響く"),
            ("最中をくずして食べる", "もなかをくずして食べる"),
            ("最中をくずす", "もなかをくずす"),
        ],
    )
    def test_monaka_replaced_for_eating_verbs(self, src, expected):
        assert sanitize.transform_monaka(src) == expected

    @pytest.mark.parametrize(
        "src",
        [
            "勉強の最中に",       # 「最中に」(時間)は触らない
            "最中だった",         # 「最中だ」も触らない
            "最中を逃した",       # 「を逃」は対象外動詞
            "最中を見過ごした",   # 「を見」は対象外動詞
            "最中の出来事",       # 「最中の」は時間表現で触らない
            # T-010: ステム交替の負例(「く」1 文字を巻き込まないこと)
            "最中をくれる",       # `く` 1 文字では `くず` と不一致
            "最中をください",     # `くだ`
            "最中をくる",         # `く` 単独
        ],
    )
    def test_monaka_does_not_match_non_eating_contexts(self, src):
        assert sanitize.transform_monaka(src) == src

    def test_pipeline_canonical_corpus_example(self):
        # B-011 hard_readings.txt の canonical example に対する統合検証:
        # 1 つ目の「最中に」(時間)は さいちゅう、2 つ目の「最中をかじり」は もなか
        raw = "最中に最中をかじりながら、中華の中身を確認する。"
        out = sanitize.sanitize(raw)
        # 1 つ目の最中は残る(時間: さいちゅう)
        assert "最中に" in out
        # 2 つ目の最中は もなか に化けている
        assert "もなかをかじり" in out


class TestSanitizeS3:
    """B-011 Phase 4: WORD_KANA に s3 を追加して「エススリー」と読ませる。
    T-001 で `[a-z][a-z0-9]*` を緩和済みなので、追加だけで地の文置換が効く。"""

    def test_s3_replaced_in_plain_text(self):
        out = sanitize.sanitize("S3 に転送")
        assert "エススリー" in out
        assert "S3" not in out

    def test_s3_case_insensitive(self):
        for src in ["S3", "s3", "S3"]:
            assert "エススリー" in sanitize.transform_dict_words(f"{src} を更新")

    def test_s3_word_boundary_protects_concatenation(self):
        # 連結された英数字は \b 境界が成立せず非マッチ(T-001 と同じ守り)
        assert sanitize.transform_dict_words("s3bucket") == "s3bucket"
        assert sanitize.transform_dict_words("ws3") == "ws3"
        assert sanitize.transform_dict_words("s33") == "s33"


class TestSanitizeTruncate:
    # truncate() のデフォルト引数 max_chars=MAX_CHARS は関数定義時に評価
    # されるため、monkeypatch で MAX_CHARS を変えても sanitize() 経由では
    # 反映されない。ここは truncate() を直接呼んで挙動を固定する
    def test_truncate_appends_suffix(self):
        out = sanitize.truncate("あ" * 30, max_chars=10)
        assert out == "あ" * 10 + "\n" + constants.TRUNCATION_SUFFIX

    def test_truncate_skips_when_under_limit(self):
        assert sanitize.truncate("短い", max_chars=10) == "短い"

    def test_truncate_uses_truncation_suffix_constant(self):
        # truncate() と split_into_chunks が同じ定数を使うことを確認
        out = sanitize.truncate("あ" * 30, max_chars=5)
        assert out.endswith(constants.TRUNCATION_SUFFIX)


# ---------- split_into_chunks() ----------


class TestSplitIntoChunks:
    def test_short_text_single_chunk(self):
        chunks = sanitize.split_into_chunks("短いテストです", target=200, hard_max=400, first_target=30)
        assert chunks == ["短いテストです"]

    def test_first_chunk_breaks_at_first_boundary_after_first_target(self):
        # first_target=10 を超えてから最初の改行で切れる
        text = "最初の段落です。これは続きです。\n二段目に入ります。続けます。"
        chunks = sanitize.split_into_chunks(text, target=200, hard_max=400, first_target=10)
        assert len(chunks) >= 2
        # 1 つ目は first_target 以上 かつ 元の改行位置で切れている
        assert chunks[0] == "最初の段落です。これは続きです。"

    def test_force_split_uses_hard_max_even_for_first_chunk(self):
        # first_target を超えても境界が見つからない場合、強制分割の幅は
        # hard_max。first chunk であっても hard_max まで伸びる
        text = "あ" * 200
        chunks = sanitize.split_into_chunks(text, target=50, hard_max=100, first_target=10)
        assert all(len(c) <= 100 for c in chunks)
        assert len(chunks[0]) == 100  # first chunk も強制分割は hard_max まで

    def test_japanese_period_fallback_when_no_newline(self):
        # 改行は無いが句点はある。first chunk は first_target=10 を超えてから
        # 最初の句点まで(=「一文目。あ...あ二文目。」)で切れる
        text = "一文目。" + "あ" * 100 + "二文目。" + "い" * 100
        chunks = sanitize.split_into_chunks(text, target=50, hard_max=300, first_target=10)
        # 少なくとも 1 個目は句点で終わる
        assert chunks[0].endswith("。")

    def test_hard_max_force_split_when_no_boundary(self):
        # 境界が一切無い場合、全 chunk が hard_max で強制分割される
        text = "あ" * 300
        chunks = sanitize.split_into_chunks(text, target=50, hard_max=100, first_target=10)
        assert all(len(c) <= 100 for c in chunks)
        # 合計文字数が保存される
        assert sum(len(c) for c in chunks) == 300

    def test_internal_newlines_replaced_with_space(self):
        # 1 つの chunk に複数行が入ったら、改行は空白に変換される
        text = "段落1\n段落2\n段落3"  # 短いので 1 chunk になる
        chunks = sanitize.split_into_chunks(text, target=200, hard_max=400, first_target=200)
        assert chunks == ["段落1 段落2 段落3"]


class TestSplitIntoChunksCacheAware:
    """is_cacheable callable を渡した時の文単位分解 + 合体ロジック"""

    def test_cacheable_sentence_emitted_as_independent_chunk(self):
        """途中のキャッシュ対象が独立 chunk として切り出される"""
        text = "前置きの文です。OK。後続の文です。"
        chunks = sanitize.split_into_chunks(
            text, target=200, hard_max=400, first_target=200,
            is_cacheable=lambda s: s == "OK。",
        )
        assert chunks == ["前置きの文です。", "OK。", "後続の文です。"]

    def test_consecutive_non_cacheable_are_combined(self):
        """非対象の文は target を超えるまで合体される"""
        text = "短文1。短文2。短文3。"
        chunks = sanitize.split_into_chunks(
            text, target=200, hard_max=400, first_target=200,
            is_cacheable=lambda s: False,
        )
        # 全部合体されて 1 chunk
        assert chunks == ["短文1。短文2。短文3。"]

    def test_combine_until_target_then_flush(self):
        """target を超えたら合体バッファを emit して新規バッファ"""
        text = "あ" * 50 + "。" + "い" * 50 + "。" + "う" * 50 + "。"
        chunks = sanitize.split_into_chunks(
            text, target=80, hard_max=200, first_target=80,
            is_cacheable=lambda s: False,
        )
        # 50 + "。" = 51 字、+ 50 + "。" = 102 字 (> 80) → 1 文目で flush ではなく 2 文目を追加すると超える
        # 実際は最初の文(51 字)が buffer に入り、2 文目を足すと 102 字で超える → 1 文目を emit、2 文目は buffer
        # 結果: ["あ...。", "い...。", "う...。"] のように分割
        assert len(chunks) == 3

    def test_long_sentence_force_split_at_hard_max(self):
        """target を超える単一文は hard_max で強制分割される"""
        text = "あ" * 250 + "。"
        chunks = sanitize.split_into_chunks(
            text, target=80, hard_max=100, first_target=80,
            is_cacheable=lambda s: False,
        )
        # 250 + 1 字を 100 字単位で割る → 100, 100, 51
        assert all(len(c) <= 100 for c in chunks)

    def test_consecutive_cacheable_each_independent(self):
        """連続するキャッシュ対象はそれぞれ独立 chunk"""
        text = "OK。完了しました。了解。"
        chunks = sanitize.split_into_chunks(
            text, target=200, hard_max=400, first_target=200,
            is_cacheable=lambda s: s in {"OK。", "完了しました。", "了解。"},
        )
        assert chunks == ["OK。", "完了しました。", "了解。"]

    def test_cacheable_at_start_flushes_empty_buffer(self):
        """先頭のキャッシュ対象は空バッファを flush して独立 emit(空 chunk が漏れない)"""
        text = "OK。続きの文です。"
        chunks = sanitize.split_into_chunks(
            text, target=200, hard_max=400, first_target=200,
            is_cacheable=lambda s: s == "OK。",
        )
        assert chunks == ["OK。", "続きの文です。"]


class TestSplitIntoChunksMaxChunks:
    """max_chunks パラメータのテスト"""

    def _make_text(self, n: int) -> str:
        return "\n".join(f"文{i}です。" for i in range(n))

    def test_max_chunks_zero_is_unlimited(self):
        text = self._make_text(10)
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=0)
        assert len(chunks) >= 5
        assert not any(c.endswith(constants.TRUNCATION_SUFFIX) for c in chunks)

    def test_max_chunks_truncates_to_limit(self):
        text = self._make_text(10)
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=3)
        # max_chunks=3 の本文チャンク + 独立「以下省略」チャンク = 4
        assert len(chunks) == 4

    def test_max_chunks_last_chunk_has_suffix(self):
        text = self._make_text(10)
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=3)
        assert chunks[-1].endswith(constants.TRUNCATION_SUFFIX)

    def test_max_chunks_not_reached_no_suffix(self):
        # max_chunks=10 だが実チャンク数は 3: suffix なし
        text = "一文目。\n二文目。\n三文目。"
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=10)
        assert len(chunks) >= 2  # 分割されていること
        assert len(chunks) <= 10  # max_chunks 以下
        assert not any(c.endswith(constants.TRUNCATION_SUFFIX) for c in chunks)

    def test_max_chunks_one(self):
        text = self._make_text(5)
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=1)
        # 本文1チャンク + 独立「以下省略」チャンク = 2
        assert len(chunks) == 2
        assert chunks[-1] == constants.TRUNCATION_SUFFIX

    def test_max_chunks_with_cache_aware(self):
        text = "前置きです。OK。後続です。続きです。さらに続きます。"
        chunks = sanitize.split_into_chunks(
            text, target=200, hard_max=400, first_target=200,
            max_chunks=2,
            is_cacheable=lambda s: s == "OK。",
        )
        # 本文2チャンク + 独立「以下省略」チャンク = 3
        assert len(chunks) == 3
        assert chunks[-1].endswith(constants.TRUNCATION_SUFFIX)

    def test_max_chunks_suffix_not_doubled(self):
        # maxChars で既に TRUNCATION_SUFFIX が付いたテキストでも二重付与されない
        suffix = constants.TRUNCATION_SUFFIX
        already_truncated = "一文目。" + suffix
        chunks = sanitize.split_into_chunks(
            already_truncated, target=200, hard_max=400, first_target=200, max_chunks=1
        )
        assert len(chunks) == 1
        assert not chunks[0].endswith(suffix + suffix)
        assert chunks[0].endswith(suffix)

    def test_max_chunks_suffix_uses_truncation_suffix_constant(self):
        # split_into_chunks の打ち切りが constants.TRUNCATION_SUFFIX を使うことを確認
        text = self._make_text(10)
        chunks = sanitize.split_into_chunks(text, target=5, hard_max=30, first_target=5, max_chunks=2)
        assert chunks[-1].endswith(constants.TRUNCATION_SUFFIX)


# ---------- 補助関数 ----------


class TestIntToKanji:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (0, "零"),
            (1, "一"),
            (10, "十"),
            (12, "十二"),
            (100, "百"),
            (2026, "二千二十六"),
            (1999, "千九百九十九"),
        ],
    )
    def test_int_to_kanji(self, n, expected):
        assert sanitize._int_to_kanji(n) == expected


class TestTransformFilenames:
    def test_basic_filename(self):
        # transform_filenames は read_as_kana を呼ぶので結果はカナ
        out = sanitize.transform_filenames("foo.py")
        assert "fp" not in out  # 何らかの変換が走っていることだけ確認
        assert out != "foo.py"

    def test_no_match_keeps_text(self):
        # 拡張子が無いと素通り
        assert sanitize.transform_filenames("Claude code") == "Claude code"


class TestTransformDictWords:
    def test_known_word_replaced(self):
        out = sanitize.transform_dict_words("Claude is here")
        assert "クロード" in out

    def test_unknown_word_kept(self):
        # 辞書に無い英単語は素通り
        assert sanitize.transform_dict_words("xyzzy") == "xyzzy"

    def test_short_word_not_matched(self):
        # 2 文字以下の登録は word boundary パターン構築から除外される
        # (誤マッチを避けるため。is / it のような頻出英字を壊さない)
        assert sanitize.transform_dict_words("it is on") == "it is on"

    # T-001: 数字含み辞書キー(k8s 等)を地の文置換対象に含める
    def test_dict_word_with_digit_replaced(self):
        # "k8s" は WORD_KANA に登録あり。地の文でカナ化される
        out = sanitize.transform_dict_words("k8s クラスタを構築")
        assert "ケーエイトエス" in out
        assert "k8s" not in out

    def test_dict_word_with_digit_case_insensitive(self):
        for src in ["k8s", "K8S", "K8s"]:
            out = sanitize.transform_dict_words(f"{src} を確認")
            assert "ケーエイトエス" in out, f"failed: {src}"

    def test_dict_word_with_digit_word_boundary_protects_concatenation(self):
        # "k8s" と隣接する英数字が連結している場合は単語境界が成立せずマッチしない。
        # 誤って "abck8s" や "k8sxx" を「(?)ケーエイトエス」と読み替えないこと
        assert sanitize.transform_dict_words("abck8s") == "abck8s"
        assert sanitize.transform_dict_words("k8sxx") == "k8sxx"
        assert sanitize.transform_dict_words("k8s9") == "k8s9"

    def test_dict_word_with_digit_within_pipeline(self):
        # sanitize() フル PIPELINE 通しでも k8s が地の文でカナ化される
        # (transform_filenames が拡張子無しを素通しすることの確認も兼ねる)
        out = sanitize.sanitize("k8s で動作確認")
        assert "ケーエイトエス" in out
        assert "k8s" not in out


# ---------- main() の出力モード ----------


class TestMain:
    """sanitize.py の main() は整形のみを担う薄い CLI(S-008 後)。
    chunk 分割と cache-aware split は scripts/chunk_split.py(別 CLI)が担う。
    """

    def test_outputs_sanitized_text(self):
        out = _run_main_with_input("**強調**", ["sanitize.py"])
        assert out == "強調"

    def test_preserves_newlines_as_chunk_hints(self):
        # 改行は chunk 分割のヒントとして保持される(後段の chunk_split.py が使う)。
        # 旧 main() は改行を空白に潰していたが、S-008 で責務分離したため改行保持。
        out = _run_main_with_input("段落1\n\n段落2", ["sanitize.py"])
        assert "\n" in out
        assert out == "段落1\n段落2"

    def test_no_chunked_flag_required(self):
        # --chunked / --speaker は廃止(chunk_split.py に移管)。
        # 引数なしで stdin → stdout のシンプルな整形のみ。
        out = _run_main_with_input("テストです。", ["sanitize.py"])
        assert out == "テストです。"


# ---------- MAX_CHARS 上限なし(R-037 + constants) ----------


class TestMaxChars:
    def test_constants_values(self):
        assert constants.MAX_CHARS_DEFAULT == 500
        assert constants.MAX_CHARS_LIMIT == 9999

    def test_default_max_chars(self):
        """デフォルト MAX_CHARS は MAX_CHARS_DEFAULT(500)"""
        # 環境変数未設定時の動作: sanitize モジュールは既にインポート済みなので
        # truncate() のデフォルト引数を通じて確認する
        short = "あ" * 10
        assert sanitize.truncate(short) == short

    def test_truncate_at_limit(self):
        text = "あ" * 600
        result = sanitize.truncate(text, 500)
        assert result == "あ" * 500 + "\n" + "(以下省略)"

    def test_max_chars_zero_resolves_to_limit(self):
        """VOICEVOX_MAX_CHARS=0 → subprocess 内で MAX_CHARS が MAX_CHARS_LIMIT になる"""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("VOICEVOX_")}
        env["VOICEVOX_MAX_CHARS"] = "0"
        r = subprocess.run(
            [sys.executable, "-c", "import sanitize; print(sanitize.MAX_CHARS)"],
            capture_output=True, text=True,
            cwd=str(REPO / "scripts"),
            env=env,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == str(constants.MAX_CHARS_LIMIT)

    def test_max_chars_negative_resolves_to_limit_with_warning(self):
        """VOICEVOX_MAX_CHARS=-1 → MAX_CHARS_LIMIT にフォールバックし stderr に警告"""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("VOICEVOX_")}
        env["VOICEVOX_MAX_CHARS"] = "-1"
        r = subprocess.run(
            [sys.executable, "-c", "import sanitize; print(sanitize.MAX_CHARS)"],
            capture_output=True, text=True,
            cwd=str(REPO / "scripts"),
            env=env,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == str(constants.MAX_CHARS_LIMIT)
        assert "無効な値" in r.stderr
