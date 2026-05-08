"""kana_dict.py の整合性テスト(T-006)

辞書の手編集で起きやすい drift を早期検出する目的のスモークテスト集。
各 dict / 文字列の不変条件を「コードコメントの規約」として固定する。

カバー範囲:
- 長音記号の揺らぎ(`ー` (U+30FC) 以外の hyphen / dash 系混入)
- 値のカタカナ純粋性(英数字や記号の混入)
- キーのフォーマット(WORD_KANA は `[a-z0-9]+`、EXTENSION_KANA は `.<lowercase>`)
- 網羅性(ALPHABET_KANA 26 文字、DIGIT_KANA 10 文字)
- COUNTER_CHARS の整合(重複なし、年月日なし、数字・ASCII なし)
- ソースレベルの重複キー(`{"k": v, "k": v}` の後勝ち事故)検出
"""
import re
from collections import Counter
from pathlib import Path

import pytest

from kana_dict import (
    ALPHABET_KANA,
    COUNTER_CHARS,
    DIGIT_KANA,
    EXTENSION_KANA,
    SYMBOL_KANA,
    WORD_KANA,
)


# 長音記号 `ー` (U+30FC) と紛れ込みやすい hyphen / dash 系。これらが値に入ると
# VOICEVOX が「ハイフン」と読み上げて誤読の原因になる。
LOOKALIKE_CHOUON = {
    "-": "U+002D HYPHEN-MINUS",
    "‐": "U+2010 HYPHEN",
    "‑": "U+2011 NON-BREAKING HYPHEN",
    "–": "U+2013 EN DASH",
    "—": "U+2014 EM DASH",
    "−": "U+2212 MINUS SIGN",
    "ｰ": "U+FF70 HALFWIDTH KATAKANA-HIRAGANA PROLONGED SOUND MARK",
}

# カタカナブロック範囲(0x30A0-0x30FF)。長音 `ー` (U+30FC)、踊り字 `ヾ` 等も含む。
# SYMBOL_KANA の "_PAUSE = ' '" は別ロジック(下の TestSymbolKana)で扱う。
KATAKANA_RE = re.compile(r"^[゠-ヿ]+$")


def _assert_no_lookalike(scope: str, key: str, value: str) -> None:
    for ch in value:
        if ch in LOOKALIKE_CHOUON:
            pytest.fail(
                f"{scope}[{key!r}] = {value!r} に長音もどき "
                f"{ch!r} ({LOOKALIKE_CHOUON[ch]}) が混入。`ー` (U+30FC) を使うこと。"
            )


# ---------- ALPHABET_KANA ----------


class TestAlphabetKana:
    def test_covers_all_26_letters(self):
        expected = set("abcdefghijklmnopqrstuvwxyz")
        actual = set(ALPHABET_KANA.keys())
        assert actual == expected, f"a-z の網羅性が崩れている: 不足={expected - actual}, 余分={actual - expected}"

    def test_keys_are_lowercase_ascii_single_char(self):
        for key in ALPHABET_KANA:
            assert len(key) == 1 and key.isascii() and key.islower(), \
                f"ALPHABET_KANA の key {key!r} は 1 文字小文字 ASCII であるべき"

    def test_values_are_katakana_only(self):
        for key, value in ALPHABET_KANA.items():
            assert KATAKANA_RE.match(value), \
                f"ALPHABET_KANA[{key!r}] = {value!r} がカタカナ以外を含む"

    def test_values_have_no_lookalike_chouon(self):
        for key, value in ALPHABET_KANA.items():
            _assert_no_lookalike("ALPHABET_KANA", key, value)


# ---------- DIGIT_KANA ----------


class TestDigitKana:
    def test_covers_all_10_digits(self):
        assert set(DIGIT_KANA.keys()) == set("0123456789")

    def test_values_are_katakana_only(self):
        for key, value in DIGIT_KANA.items():
            assert KATAKANA_RE.match(value), \
                f"DIGIT_KANA[{key!r}] = {value!r} がカタカナ以外を含む"


# ---------- SYMBOL_KANA ----------


class TestSymbolKana:
    def test_all_values_are_pause(self):
        # 現行実装は全値 `_PAUSE = " "`(半角空白)で揃えている。これが崩れたら
        # 「区切り記号は読まずに短いポーズ」という設計意図が失われるので検出する。
        values = set(SYMBOL_KANA.values())
        assert values == {" "}, f"SYMBOL_KANA の値が _PAUSE 以外に分かれている: {values!r}"


# ---------- EXTENSION_KANA ----------


class TestExtensionKana:
    def test_keys_start_with_dot(self):
        for key in EXTENSION_KANA:
            assert key.startswith("."), \
                f"EXTENSION_KANA の key {key!r} が '.' で始まっていない"

    def test_keys_are_lowercase(self):
        for key in EXTENSION_KANA:
            assert key == key.lower(), \
                f"EXTENSION_KANA の key {key!r} が小文字でない(split_extension は lower 比較)"

    def test_keys_have_no_whitespace(self):
        for key in EXTENSION_KANA:
            assert not any(c.isspace() for c in key), \
                f"EXTENSION_KANA の key {key!r} に空白文字が混入"

    def test_values_start_with_dot_kana(self):
        # 規約: 全値「ドット〜」で始まる(read_inline_code でドットを明示的に読ませるため)
        for key, value in EXTENSION_KANA.items():
            assert value.startswith("ドット"), \
                f"EXTENSION_KANA[{key!r}] = {value!r} が「ドット」で始まっていない"

    def test_values_are_katakana_only(self):
        for key, value in EXTENSION_KANA.items():
            assert KATAKANA_RE.match(value), \
                f"EXTENSION_KANA[{key!r}] = {value!r} がカタカナ以外を含む"

    def test_values_have_no_lookalike_chouon(self):
        for key, value in EXTENSION_KANA.items():
            _assert_no_lookalike("EXTENSION_KANA", key, value)


# ---------- WORD_KANA ----------


class TestWordKana:
    def test_keys_are_lowercase_ascii_alphanum(self):
        # transform_dict_words の `\b<key>\b` 正規表現は word boundary に依存する。
        # `_` `-` を含むと boundary の挙動がブレるので a-z 0-9 のみに限る。
        for key in WORD_KANA:
            assert re.fullmatch(r"[a-z0-9]+", key), \
                f"WORD_KANA の key {key!r} は a-z 0-9 のみで構成すべき(現状: {[hex(ord(c)) for c in key]})"

    def test_values_are_katakana_only(self):
        for key, value in WORD_KANA.items():
            assert KATAKANA_RE.match(value), \
                f"WORD_KANA[{key!r}] = {value!r} がカタカナ以外を含む"

    def test_values_have_no_lookalike_chouon(self):
        for key, value in WORD_KANA.items():
            _assert_no_lookalike("WORD_KANA", key, value)

    def test_values_are_non_empty(self):
        for key, value in WORD_KANA.items():
            assert value, f"WORD_KANA[{key!r}] の値が空"


# ---------- COUNTER_CHARS ----------


class TestCounterChars:
    def test_no_duplicates(self):
        # 文字列なので Counter で重複文字を検出
        counts = Counter(COUNTER_CHARS)
        dupes = {ch: n for ch, n in counts.items() if n > 1}
        assert not dupes, f"COUNTER_CHARS に重複文字: {dupes}"

    def test_non_empty(self):
        assert len(COUNTER_CHARS) > 0

    def test_excludes_year_month_day(self):
        # transform_year / transform_month_day で先に消費されるため、ここに含めると
        # 二重発火のリスクがある。明示的に除外を保証する。
        for ch in "年月日":
            assert ch not in COUNTER_CHARS, \
                f"COUNTER_CHARS に {ch!r} が含まれている(transform_year/month_day と二重発火の可能性)"

    def test_no_digits(self):
        for ch in COUNTER_CHARS:
            assert not ch.isdigit(), \
                f"COUNTER_CHARS に数字 {ch!r} が含まれている(正規表現 `(\\d+)[ 　]+([COUNTER_CHARS])` の意味が崩れる)"

    def test_no_ascii_chars(self):
        # 助数詞は CJK 漢字 + カタカナ「ヶ」のみ。ASCII は混入し得ない
        for ch in COUNTER_CHARS:
            assert not ch.isascii(), \
                f"COUNTER_CHARS に ASCII 文字 {ch!r} が含まれている"

    def test_no_whitespace(self):
        for ch in COUNTER_CHARS:
            assert not ch.isspace(), \
                f"COUNTER_CHARS に空白 {ch!r} が含まれている"


# ---------- ソースレベルの重複キー検出 ----------


class TestSourceLevelDuplicateKeys:
    """Python の dict literal `{"k": 1, "k": 2}` は構文 OK で後勝ち。dict オブジェクト
    上の重複は失われるので、原ソースをテキスト走査して "key": の出現数を数える。

    手編集で同じ key を 2 回登録すると先のエントリが黙って消えるバグの予防線。
    """

    @pytest.fixture(scope="class")
    def src_text(self) -> str:
        path = Path(__file__).resolve().parent.parent / "scripts" / "kana_dict.py"
        return path.read_text(encoding="utf-8")

    def _extract_section(self, src: str, name: str) -> str:
        # `NAME = { ... }` の中身だけを抜き出す。三重引用符内に同名トークンが
        # 来ない前提(現状の kana_dict.py は満たしている)。
        m = re.search(rf"^{name}\s*=\s*\{{(.*?)^\}}", src, re.MULTILINE | re.DOTALL)
        assert m, f"ソースから {name} セクションが見つからない"
        return m.group(1)

    def _keys_in_section(self, section: str) -> list[str]:
        # 各行の先頭近くにある `"key":` を拾う。値の中に出てくる文字列は対象外
        # (行頭から最初の `"..."` だけを見る)
        keys = []
        for line in section.splitlines():
            m = re.match(r'\s*"([^"]+)"\s*:', line)
            if m:
                keys.append(m.group(1))
        return keys

    @pytest.mark.parametrize("dict_name", ["ALPHABET_KANA", "DIGIT_KANA", "EXTENSION_KANA", "WORD_KANA", "SYMBOL_KANA"])
    def test_no_duplicate_keys_in_source(self, src_text, dict_name):
        section = self._extract_section(src_text, dict_name)
        keys = self._keys_in_section(section)
        counts = Counter(keys)
        dupes = {k: n for k, n in counts.items() if n > 1}
        assert not dupes, \
            f"{dict_name} に重複キーが宣言されている(後勝ちで黙って上書き): {dupes}"
