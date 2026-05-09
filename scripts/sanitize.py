#!/usr/bin/env python3
"""
sanitize.py - Claude の応答テキストを読み上げ用に整形する

標準入力からテキストを受け取り、PIPELINE に並んだ各変換を順に適用して
標準出力に書き出す。新しい整形ルールを足すときは PIPELINE に関数を 1 つ
追加すれば済む構成。インラインコードの読みを拡張したいときは
kana_dict.py の EXTENSION_KANA / WORD_KANA に登録する。
"""

import os
import re
import sys
from typing import Callable, List, Optional, Tuple

# 同ディレクトリの kana_dict.py を import 可能にする
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kana_dict import (  # noqa: E402
    ALPHABET_KANA,
    COUNTER_CHARS,
    DIGIT_KANA,
    EXTENSION_KANA,
    SYMBOL_KANA,
    WORD_KANA,
)
from constants import (  # noqa: E402
    CHUNK_CHARS_DEFAULT,
    CHUNK_HARD_MAX_DEFAULT,
    FIRST_CHUNK_CHARS_DEFAULT,
    INLINE_CODE_LIMIT_DEFAULT,
    MAX_CHARS_DEFAULT,
    MAX_CHARS_LIMIT,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# 整形後テキストの最大文字数。0 は「上限なし」として MAX_CHARS_LIMIT に読み替え
_max_chars_raw = _env_int("VOICEVOX_MAX_CHARS", MAX_CHARS_DEFAULT)
MAX_CHARS = MAX_CHARS_LIMIT if _max_chars_raw == 0 else _max_chars_raw

# インラインコード入力長の上限。これを超えると詳細を読まず「コマンド」または
# 「ファイル」(末尾が登録済み拡張子の場合)で代用する。辞書ヒットしないトークンが
# per-char で展開されると暴走するための短縮読み上げ。
INLINE_CODE_LENGTH_LIMIT = _env_int("VOICEVOX_INLINE_CODE_LIMIT", INLINE_CODE_LIMIT_DEFAULT)

# 1 チャンクの目安文字数。これを超える場合は境界(改行→句点)で分割する
CHUNK_CHARS = _env_int("VOICEVOX_CHUNK_CHARS", CHUNK_CHARS_DEFAULT)

# 1 チャンクの強制上限。改行・句点が見つからない場合はここで強制分割する
CHUNK_HARD_MAX = _env_int("VOICEVOX_CHUNK_HARD_MAX", CHUNK_HARD_MAX_DEFAULT)

# 最初のチャンクの目安文字数。初手の合成時間を短くして声出しまでのレイテンシを縮める
FIRST_CHUNK_CHARS = _env_int("VOICEVOX_FIRST_CHUNK_CHARS", FIRST_CHUNK_CHARS_DEFAULT)


# ---------- e2k(英単語 → カタカナ)フォールバック ----------
# 重い RNN モデルを抱えるため、最初に必要になったときだけ遅延ロードする。
# e2k が未インストールでも動くように、ImportError は吸収して無効化する。

_e2k_state: dict = {"loaded": False, "models": None}


def _get_e2k():
    if not _e2k_state["loaded"]:
        _e2k_state["loaded"] = True
        try:
            from e2k import C2K, NGram  # type: ignore
            _e2k_state["models"] = (C2K(), NGram())
        except Exception:
            _e2k_state["models"] = None
    return _e2k_state["models"]


def lookup_english(word: str) -> Optional[str]:
    """英単語を WORD_KANA → e2k の順で引き、カタカナ読みを返す。
    引けなければ None(呼び出し側で逐字フォールバックする)
    """
    if not word:
        return None
    lower = word.lower()
    if lower in WORD_KANA:
        return WORD_KANA[lower]
    if not lower.isalpha() or len(lower) < 2:
        return None
    models = _get_e2k()
    if models is None:
        return None
    c2k, ngram = models
    if not ngram(lower):
        # ngram が「綴り読みすべき」と判定 → 呼び出し側の per-char に任せる
        return None
    try:
        return c2k(lower)
    except Exception:
        return None


# ---------- 個別の変換ヘルパー ----------

def to_kana_per_char(text: str) -> str:
    """1 文字ずつカタカナ読みに変換(辞書ヒットしないときの最終手段)"""
    parts = []
    for ch in text:
        lower = ch.lower()
        if lower in ALPHABET_KANA:
            parts.append(ALPHABET_KANA[lower])
        elif ch in DIGIT_KANA:
            parts.append(DIGIT_KANA[ch])
        elif ch in SYMBOL_KANA:
            parts.append(SYMBOL_KANA[ch])
        else:
            parts.append(ch)
    return "".join(parts)


def has_ascii_alnum(text: str) -> bool:
    return any(c.isascii() and c.isalnum() for c in text)


def split_extension(content: str) -> Optional[Tuple[str, str]]:
    """末尾が登録済み拡張子なら (stem, 拡張子カナ) を返す。なければ None"""
    lower = content.lower()
    # 長いマッチを優先(.tar.gz のような複合拡張子は今は対応していない)
    for ext in sorted(EXTENSION_KANA, key=len, reverse=True):
        if lower.endswith(ext):
            stem = content[: -len(ext)]
            return stem, EXTENSION_KANA[ext]
    return None


def read_token(tok: str) -> str:
    """単語または記号 1 つ分を読み上げ表現に変換"""
    if not tok:
        return ""
    if tok in SYMBOL_KANA:
        return SYMBOL_KANA[tok]
    kana = lookup_english(tok)
    if kana is not None:
        return kana
    return to_kana_per_char(tok)


def read_as_kana(content: str) -> str:
    """単語辞書/e2k → 拡張子サフィックス → 区切り分割 → 逐字 の順で読みを決める"""
    kana = lookup_english(content)
    if kana is not None:
        return kana

    suffix = split_extension(content)
    if suffix is not None:
        stem, ext_kana = suffix
        return (read_as_kana(stem) if stem else "") + ext_kana

    tokens = re.split(r"([./_\-:= ])", content)
    if len(tokens) > 1:
        return "".join(read_token(t) for t in tokens)

    return to_kana_per_char(content)


# インラインコード読み上げ判定の構成要素。
# PIPELINE と同じく「条件 → 変換」の組をリストに並べて、上から順に最初にマッチした
# ルールを採用する。新ルール追加時は INLINE_CODE_RULES に 1 行足すだけで済むよう、
# 各 predicate を named function として外出ししている。
_HEX_HASH_RE = re.compile(r"[0-9a-fA-F]{6,40}")


def _is_url(content: str) -> bool:
    return re.match(r"^https?://", content) is not None


def _is_japanese_only(content: str) -> bool:
    return not has_ascii_alnum(content)


def _is_commit_hash(content: str) -> bool:
    # 6〜40 字の hex(コミットハッシュ等)。「数字と英字 a-f が両方混在する」ことを
    # 条件に絞る("aaaaaaa" や "11111" のような単一文字反復は対象外)。
    # cache_patterns 側で「ハッシュ で コミット。」のパターンにヒットさせるため、
    # 逐字カナ化(キュウゴディービーニエー...)を避ける目的。
    return (_HEX_HASH_RE.fullmatch(content) is not None
            and re.search(r"\d", content) is not None
            and re.search(r"[a-fA-F]", content) is not None)


def _is_long_filepath(content: str) -> bool:
    # 長文 + 末尾が EXTENSION_KANA 登録済み拡張子(.md / .sh / .json 等)。
    # 「ファイル」と「コマンド」を区別することで、聞き手が「長いファイルパスが
    # 省略された」のか「コマンドが省略された」のかを把握できるようにする。
    return len(content) > INLINE_CODE_LENGTH_LIMIT and split_extension(content) is not None


def _is_long_command(content: str) -> bool:
    # 長文 + 拡張子無し → 「コマンド」フォールバック。順序上 _is_long_filepath の
    # 後に置く必要がある(両方 True ならファイル扱いを優先)。
    return len(content) > INLINE_CODE_LENGTH_LIMIT


# (predicate, transform) のペア。順序が意味を持つ:
# URL → 日本語のみ → hex ハッシュ → 長文ファイルパス → 長文コマンド → (フォールバック)
INLINE_CODE_RULES: List[Tuple[Callable[[str], bool], Callable[[str], str]]] = [
    (_is_url,           lambda c: "URL省略"),
    (_is_japanese_only, lambda c: c),
    (_is_commit_hash,   lambda c: "ハッシュ"),
    (_is_long_filepath, lambda c: "ファイル"),
    (_is_long_command,  lambda c: "コマンド"),
]


def read_inline_code(content: str) -> str:
    """インラインコード `foo` の中身を読み上げ用に変換。

    INLINE_CODE_RULES の最初にマッチしたルールを採用し、どれにもマッチしなければ
    read_as_kana(辞書/拡張子/区切り/逐字)にフォールバックする。
    """
    for matches, transform in INLINE_CODE_RULES:
        if matches(content):
            return transform(content)
    return read_as_kana(content)


# ---------- パイプラインの各ステップ ----------

def remove_code_blocks(text: str) -> str:
    return re.sub(r"```[\s\S]*?```", "コードブロック省略。", text)


def transform_inline_code(text: str) -> str:
    return re.sub(r"`([^`]*)`", lambda m: read_inline_code(m.group(1)), text)


def remove_urls(text: str) -> str:
    return re.sub(r"https?://\S+", "URL省略", text)


# T-009: 地の文に出る絶対/ホームパスを末尾セグメントだけに圧縮する。
# インラインコード版(`...`)は `_is_long_filepath` が「ファイル」総称化するが、
# 地の文は通常「ファイルは X にあります」のように文脈で「これはパス」が伝わるため
# 末尾だけ残せば十分(後段の transform_filenames が tail.ext を自然にカナ化する)。
#
# パターン仕様:
#   - 先頭は `/` または `~/`(絶対パスとホームパス)
#   - 第 1 セグメントは英字または `.` 始まり(`/var/...` / `/Users/...` /
#     `~/.config/...`)。`/2026/...` のような数字始まりは除外して、
#     `transform_dates` が拾い損ねた数字 3 連を巻き込まない
#   - 全体で 3 セグメント以上(`/a/b/c` 以上)。`/var/log` のような 2 セグメントは
#     現状 VOICEVOX が「ヴァア、ログ」程度に収めるので無理に変換しない
#   - セグメント文字は `[A-Za-z0-9._-]+`。日本語句読点(。、)を巻き込まない
#   - 直前が単語文字または `/` の場合は無視(連結や URL 残骸の誤マッチ防止)
#
# 順序: remove_urls の後、transform_filenames より前。後者を先に走らせると
# パス末尾の `sanitize.py` だけ部分カナ化され、`/Users/.../サニタイズドットパイ`
# のような中途半端な状態になる(T-009 観測 snapshot 参照)。
BARE_PATH_PATTERN = re.compile(
    r"(?<![\w/])(~?/[A-Za-z.][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+){2,})"
)


def transform_bare_paths(text: str) -> str:
    return BARE_PATH_PATTERN.sub(lambda m: m.group(1).rsplit("/", 1)[-1], text)


# T-009: 地の文の hex ハッシュ(6〜40 字、数字 + a-f 混在)を「ハッシュ」化する。
# インラインコード版は `_is_commit_hash` が同条件で「ハッシュ」化しているので、
# その判定をそのまま地の文にも流用する(DRY)。
#
# `\b` 境界で囲み、`xxx0bfebe5xxx` のような連結語を弾く。alpha-only(`abcdef`)や
# digit-only(`1234567`)は `_is_commit_hash` で False になり、変換されない。
#
# 順序: transform_bare_paths の後に置く。パス内に hex セグメントが含まれる場合は
# パス変換で末尾だけ残るため、残った末尾が hex なら本変換でさらに「ハッシュ」化される。
BARE_HASH_PATTERN = re.compile(r"\b[0-9a-fA-F]{6,40}\b")


def transform_bare_hashes(text: str) -> str:
    return BARE_HASH_PATTERN.sub(
        lambda m: "ハッシュ" if _is_commit_hash(m.group(0)) else m.group(0),
        text,
    )


# 地の文の「英字 + 拡張子」(CLAUDE.md, package.json 等)を read_as_kana に通す。
# URL は remove_urls で先に消えているので、ここに入ってくるのは地の文のファイル名。
FILENAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\.[a-zA-Z]+")


def transform_filenames(text: str) -> str:
    return FILENAME_PATTERN.sub(lambda m: read_as_kana(m.group(0)), text)


# 地の文の英単語のうち WORD_KANA に登録されているものを置換する。
# (transform_filenames が拡張子付きを既に消費しているので、ここは裸の語が対象)
#
# T-001: k8s / s3 のような「英字始まり + 英数字混在」のキーも置換対象に含める。
# isalpha() で除外していると WORD_KANA に "k8s": "ケーエイトエス" を登録しても
# 地の文の "k8s クラスタ" には効かず逐字読み(ケー、ハチ、エス)になっていた。
#
# 条件:
# - ASCII / 英字始まり / 英数字のみ
# - 「3 文字以上」または「数字を含む」(B-011 Phase 4 で `s3` のような 2 文字
#   英数字混在キーを対象に含めるための緩和。`cd` `ls` 等の 2 文字 alpha-only は
#   `is` / `it` / `on` 等の頻出英字との誤マッチを避けるため引き続き除外)
#
# `\b` 境界は \w (英数字+_) の境目で取れるので、"s3" の前後が \W であれば
# だけマッチし、"s3bucket" "ws3" のような連結ではマッチしない(safe)。
def _build_word_kana_pattern() -> Optional[re.Pattern]:
    keys = [
        k for k in WORD_KANA.keys()
        if k.isascii() and k[0].isalpha() and k.isalnum()
        and (len(k) >= 3 or any(c.isdigit() for c in k))
    ]
    if not keys:
        return None
    keys.sort(key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")\b", re.IGNORECASE)


_WORD_KANA_PATTERN = _build_word_kana_pattern()


def transform_dict_words(text: str) -> str:
    if _WORD_KANA_PATTERN is None:
        return text
    return _WORD_KANA_PATTERN.sub(lambda m: WORD_KANA.get(m.group(0).lower(), m.group(0)), text)


def remove_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def strip_markdown_links(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return text


def strip_table_separators(text: str) -> str:
    text = re.sub(r"^\s*\|?[\s\-:|]+\|?\s*$", "", text, flags=re.MULTILINE)
    return text.replace("|", " ")


def strip_heading_marks(text: str) -> str:
    return re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)


def strip_list_marks(text: str) -> str:
    return re.sub(r"^\s*[-*+>]\s+", "", text, flags=re.MULTILINE)


def strip_emphasis_marks(text: str) -> str:
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    return text


def collapse_whitespace(text: str) -> str:
    # 改行は chunk 分割のヒントとして残す。連続改行は 1 つに、それ以外の連続空白は 1 つに
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    return text.strip()


# 「数字 区切り 数字 区切り 数字」の 3 つ組を年月日として漢数字化する。
# - 区切り文字は - か / で揃っている必要がある(\2 で同一を要求)
# - 年は 4 桁(YYYY)または 2 桁(YY)を受ける
# - 月日は 0 埋め任意(1〜12 / 1〜31)
# - 月日だけの 2 つ組(1/2 や 16/9 など比率と紛らわしい表記)は意図的に除外
# - VOICEVOX は 2 桁以上の数字を桁ごと(2026→ニゼロニロク、12→イチニ)に読んで
#   しまうので、年・月・日とも漢数字に置換して自然に読ませる
#
# T-012: 区切り両側に半角/全角空白を許容する。「2026 / 05 / 02」のように
# 空白が挟まる表記でも漢数字化されるよう、`[ 　]*` を挟む。`\2` は同一
# 区切り文字を参照するだけなので空白挿入で参照は壊れない。
DATE_PATTERN = re.compile(
    r"(?<!\d)(\d{4}|\d{2})[ 　]*([-/])[ 　]*(0?[1-9]|1[0-2])[ 　]*\2[ 　]*(0?[1-9]|[12]\d|3[01])(?!\d)"
)

# 2 桁年を 4 桁に補完するときの境界。45 以上は 19xx、未満は 20xx に解釈する
TWO_DIGIT_YEAR_PIVOT = 45

_KANJI_DIGITS = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _int_to_kanji(n: int) -> str:
    """0〜9999 の整数を漢数字に変換する(2026 → 二千二十六)"""
    if n == 0:
        return "零"
    parts = []
    for unit_val, unit_kanji in [(1000, "千"), (100, "百"), (10, "十")]:
        d = n // unit_val
        if d == 1:
            parts.append(unit_kanji)
        elif d > 1:
            parts.append(_KANJI_DIGITS[d] + unit_kanji)
        n %= unit_val
    if n > 0:
        parts.append(_KANJI_DIGITS[n])
    return "".join(parts)


def _normalize_year(token: str) -> int:
    """年トークンを 4 桁の西暦に正規化する。2 桁は TWO_DIGIT_YEAR_PIVOT で 19xx/20xx に振り分け"""
    n = int(token)
    if len(token) >= 4:
        return n
    return 1900 + n if n >= TWO_DIGIT_YEAR_PIVOT else 2000 + n


def transform_dates(text: str) -> str:
    def repl(m: "re.Match[str]") -> str:
        year = _int_to_kanji(_normalize_year(m.group(1)))
        month = _int_to_kanji(int(m.group(3)))
        day = _int_to_kanji(int(m.group(4)))
        return f"{year}年{month}月{day}日"
    return DATE_PATTERN.sub(repl, text)


# 単発の「数字 + 月 / 日」を漢数字化する。
# VOICEVOX は「5月」を「ゴツキ」と誤読するため、「五月」に直してから渡す。
# transform_dates が YYYY-MM-DD を先に消費しているので、ここに来るのは
# 「5月請求」「5月15日」のような独立した月日表記のみ。
#
# B-011 Phase 3: 半角/全角空白を許容する。組版習慣で「5 月」のように
# 数字と漢字の境界に空白が入ると、空白なしの場合と同じ漢数字化を期待される。
# `[ 　]*` (0 個以上)で書かれた通りの両方に対応。
MONTH_DAY_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[ 　]*([月日])")


def transform_month_day(text: str) -> str:
    return MONTH_DAY_PATTERN.sub(
        lambda m: f"{_int_to_kanji(int(m.group(1)))}{m.group(2)}",
        text,
    )


# 単発の「数字 + 年」を漢数字化する。
# VOICEVOX は「2026年」を「ニゼロニロクネン」と桁ごとに誤読するため、
# 「二千二十六年」に直してから渡す。
# 4 桁(2026)と 2 桁(26)の両方を受ける。2 桁は 20xx 補完せず、書かれた
# 通りに「二十六年」と読ませる(transform_dates の TWO_DIGIT_YEAR_PIVOT は
# YYYY-MM-DD でしか使わない)。
#
# B-011 Phase 3: 半角/全角空白を許容する。「2026 年」のような空白入り表記でも
# 漢数字化されるよう `[ 　]*` (0 個以上)を挟む。
YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4}|\d{2})[ 　]*年")


def transform_year(text: str) -> str:
    return YEAR_PATTERN.sub(
        lambda m: f"{_int_to_kanji(int(m.group(1)))}年",
        text,
    )


# 「<数字>文目」は VOICEVOX が「文目」を「アヤメ」と誤読する(空白除去でも解消
# しない、Phase 1 の COUNTER_CHARS では救えない数少ない組合せ)。漢数字 + カタカナの
# 「ブンメ」表記に置き換えて正しく読ませる。空白あり(`1 文目`)/全角空白/連続空白
# にも対応する。
BUNME_PATTERN = re.compile(r"(?<!\d)(\d+)[ 　]*文目")


def transform_bunme(text: str) -> str:
    return BUNME_PATTERN.sub(
        lambda m: f"{_int_to_kanji(int(m.group(1)))}ブンメ",
        text,
    )


# 「数字 + 半角/全角空白 + 助数詞文字」の間の空白を除去する。
# Claude の応答は「1 件」のように日本語と半角数字の境界に半角空白を置く組版習慣
# になっており、これがあると VOICEVOX は数字と助数詞を独立語に分解して音便化に
# 失敗する(1 件 → イチ/ケン、本来は イッケン)。空白を消せば VOICEVOX 自身が
# 正しく音便化するので、kana_dict.COUNTER_CHARS に列挙した助数詞についてのみ
# 空白を削る。年/月/日 は transform_year / transform_month_day で既に漢数字化
# されているため、ここでは触らない。
COUNTER_SPACE_PATTERN = re.compile(
    rf"(?<!\d)(\d+)[ 　]+([{COUNTER_CHARS}])"
)

# B-011 Phase 3: COUNTER_CHARS に未登録の助数詞・単位(期 / 泊 / 袋 / 営業日 /
# 四半期 / パーセント / メートル ...)も「数字+空白+漢字 or カタカナ連続」の
# 形で空白除去対象にする。COUNTER_CHARS パターン(つ などの hiragana 助数詞
# を残すため)とは別パターンとして重ね適用する。
#
# Unicode 範囲:
#   一-鿿 = CJK 統合漢字 (基本ブロック)
#   ゠-ヿ = カタカナ (長音符号 ー も含む)
# hiragana は範囲外なので「5 と 6」「5 から 10」のような助詞・接続詞は誤マッチしない。
# ASCII 単位(5 cm / 5 km)も範囲外なので別スコープ(優先度低)。
COUNTER_SPACE_GENERAL_PATTERN = re.compile(
    r"(?<!\d)(\d+)[ 　]+([一-鿿゠-ヿ]+)"
)

# T-011: ASCII 単位(`5 cm` / `5 GB` 等)の空白除去。COUNTER_SPACE_GENERAL_PATTERN は
# CJK 漢字 + カタカナのみが対象なので、ASCII の単位は別パターンで吸収する。
#
# ホワイトリスト方式を採る理由: `\d+ [a-zA-Z]+` のような一般化は
# 「version 1 stable」「5 minutes」「step 3 done」のような数字+空白+英単語の
# 通常表現を巻き込んで誤マッチする。1 文字単位(m/s/g/t)も meter / minute /
# second / secondary / gram など複数語と衝突するため除外。
#
# 列挙する単位は「実機で空白を入れた状態の VOICEVOX が誤読 or 分解読みする」
# ことが想定されるもの:
#   長さ: cm / mm / km   重量: kg / mg
#   容量: GB / MB / KB / TB   時間: ms
#   周波数: Hz / kHz / MHz
#
# `(?![A-Za-z0-9])` は連結語ガード(`5 cmd` の cm + d、`5 GBP` の GB + P 等を
# 弾いて誤マッチを防ぐ)。
COUNTER_SPACE_ASCII_PATTERN = re.compile(
    r"(?<!\d)(\d+)[ 　]+(cm|mm|km|kg|mg|GB|MB|KB|TB|ms|Hz|kHz|MHz)(?![A-Za-z0-9])"
)


def transform_counter_space(text: str) -> str:
    # COUNTER_CHARS パターンを先に当てて hiragana 助数詞(つ等)を吸収。
    # その後 GENERAL パターンで漢字・カタカナの未登録助数詞を吸収。
    # 最後に ASCII 単位(cm / GB 等)を吸収。いずれも「空白を消す」だけの
    # 冪等変換なので順序は安全。
    text = COUNTER_SPACE_PATTERN.sub(r"\1\2", text)
    text = COUNTER_SPACE_GENERAL_PATTERN.sub(r"\1\2", text)
    text = COUNTER_SPACE_ASCII_PATTERN.sub(r"\1\2", text)
    return text


# B-011 Phase 4: 「第 + 半角/全角空白 + 数字」の前空白を除去する。
# 「第 1 四半期」「第 2 土曜日」のように VOICEVOX が「ダイ、イチ、…」と
# 句切ってしまうケースの救済。Phase 3 の transform_counter_space では
# 「数字 + 空白 + 漢字/カナ」の側だけ吸収していたため、「第 + 数字」の前空白は
# 残っていた。本ルールで「第1 四半期」まで詰め、後段の transform_counter_space
# で「第1四半期」まで圧縮する設計(統合テストで PIPELINE 順序を固定)。
PREFIX_DIGIT_PATTERN = re.compile(r"(第)[ 　]+(\d)")


def transform_prefix_digit(text: str) -> str:
    return PREFIX_DIGIT_PATTERN.sub(r"\1\2", text)


# B-011 Phase 4: 「あの方」(本来「あのかた」)を VOICEVOX が「アノホオ」と
# 誤読する問題の救済。文脈判定不可なので限定置換する。
# 後ろが [法 向 面 々] のいずれかなら別の語(方法 / 方向 / 方面 / 方々)なので
# 触らない。それ以外(助詞 / 句読点 / 末尾)では「あのかた」と読ませる。
ANO_KATA_PATTERN = re.compile(r"あの方(?![法向面々])")


def transform_ano_kata(text: str) -> str:
    return ANO_KATA_PATTERN.sub("あのかた", text)


# B-011 Phase 4: 「お米」(本来「おこめ」)を VOICEVOX が「オベエ」と誤読
# する問題の救済。複合語(「お米屋」等)も hiragana 化されるが、VOICEVOX は
# 「おこめや」を素直に読めるので問題なし(advisor 確認済み)。
OKOME_PATTERN = re.compile(r"お米")


def transform_okome(text: str) -> str:
    return OKOME_PATTERN.sub("おこめ", text)


# B-011 Phase 4 / T-010: 「最中」を文脈で「もなか / さいちゅう」と読み分けられない
# (VOICEVOX 側は両方「サイチュウ」と読む)問題の救済。
# 安全側に倒し、「最中を + 食/かじ/噛/割/くず」のステム交替でマッチ。
# 「最中を逃した」「最中を見過ごした」のような慣用は **触らない**。
#
# T-010 で「最中を割る」「最中をくずして食べる」が実機で `サイチュウオワル` /
# `サイチュウオクズシテタベル` と誤読することを probe で確認、対象動詞を拡張した
# (Phase 4 の `[食か噛]` 文字クラスは安全側に倒した結果なので、観測ベースで追加)。
#
# 文字クラス [食か噛割く] にしないのは、`く` 1 文字を入れると「最中をくれる」
# 「最中をください」「最中をくる」など意図しない動詞を巻き込むため。ステム単位
# (`くず`)で交替して `くずす / くずして` のみに限定する。
MONAKA_PATTERN = re.compile(r"最中(?=を(?:食|かじ|噛|割|くず))")


def transform_monaka(text: str) -> str:
    return MONAKA_PATTERN.sub("もなか", text)


def truncate(text: str, max_chars: int = MAX_CHARS) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + "(以下省略)"
    return text


# ---------- パイプライン ----------

# 順序が意味を持つ:
# - コードブロック(三連)を先に潰してからインラインコード(単一)を処理する
# - インラインコードを先に消費してから URL 置換することで、バッククォート内の
#   URL は read_inline_code 側のロジックで扱える(逐字カナ化を回避)
PIPELINE: List[Callable[[str], str]] = [
    remove_code_blocks,
    # B-011 Phase 4: 「第 + 空白 + 数字」の前空白を先に詰めることで、後段の
    # transform_year / transform_month_day / transform_counter_space がそれぞれ
    # 「第N年」「第N月」「第N四半期」を漢数字・空白除去できるようにする。
    transform_prefix_digit,
    # 日付変換はインラインコード処理より前に置く。
    # `2026-04-30` のようにバッククォート内に日付が来ても、漢数字に変換してから
    # インラインコード処理に渡れば has_ascii_alnum が False になり、そのまま
    # 「二千二十六年四月三十日」として読まれる
    transform_dates,
    transform_year,
    transform_month_day,
    # 「<数字>文目」は VOICEVOX が誤読(アヤメ)するので、漢数字+カタカナ化で
    # 迂回する。数字を漢数字にしてしまうので transform_counter_space より前に置く。
    transform_bunme,
    # 上 4 つで 年/月/日/文目 を漢数字化したあと、残りの「数字 + 空白 + 助数詞」の
    # 空白を除去する。日付系・文目を先に消費させているので、ここで「2026 年」のような
    # 入力にマッチして二重処理になることはない。
    transform_counter_space,
    # B-011 Phase 4: 同形異音の固有読みを限定置換。VOICEVOX が文脈判定で
    # 取り違える特定パターン(あの方=ホオ / お米=オベエ / もなかをかじる=サイチュウ)
    # を hiragana 表記に置き換えて正しく読ませる。範囲を狭めるための negative
    # lookahead や次語制限を入れている(各関数のコメント参照)。
    transform_ano_kata,
    transform_okome,
    transform_monaka,
    transform_inline_code,
    remove_urls,
    # T-009: 地の文の hash / path を読み飛ばす。
    # path → hash の順で並べる: パス内に hex セグメントが含まれた場合、先に
    # パスを末尾だけにしてから残った末尾を hash 判定にかけられる。
    # transform_filenames より前に置くことで、path 末尾だけ部分マッチして
    # 「/Users/.../サニタイズドットパイ」のような中途半端な出力を避ける。
    transform_bare_paths,
    transform_bare_hashes,
    # 地の文の英字パターンを辞書/拡張子経由でカナ化。順序は重要:
    # ファイル名 (CLAUDE.md) を先に丸ごと消費してから、残った裸の単語 (Claude) を辞書置換
    transform_filenames,
    transform_dict_words,
    remove_html_tags,
    strip_markdown_links,
    strip_table_separators,
    strip_heading_marks,
    strip_list_marks,
    strip_emphasis_marks,
    collapse_whitespace,
]


def sanitize(text: str) -> str:
    for step in PIPELINE:
        text = step(text)
    return truncate(text)


def split_into_chunks(
    text: str,
    target: int = CHUNK_CHARS,
    hard_max: int = CHUNK_HARD_MAX,
    first_target: int = FIRST_CHUNK_CHARS,
    is_cacheable=None,
) -> List[str]:
    """テキストを発話単位のチャンクに分割する。

    境界の優先度: 改行 → 句点(。)→ 強制 hard_max 文字
    各チャンクは内部の改行を空白に変換した完成形を返す。
    最初のチャンクは初手レイテンシを縮めるため first_target で短めに切る。

    is_cacheable: callable(sentence) -> bool。指定すると「文単位分解 +
    キャッシュ対象は独立 chunk、非対象は target を超えるまで合体バッファに
    溜める」というキャッシュ最適化モードで動作する。指定しなければ従来動作。
    """
    if is_cacheable is None:
        return _split_traditional(text, target, hard_max, first_target)
    return _split_with_cache_aware(text, target, hard_max, first_target, is_cacheable)


def _split_traditional(text, target, hard_max, first_target) -> List[str]:
    chunks: List[str] = []
    remaining = text
    while remaining:
        # 最初のチャンクだけ短めの目安(first_target)を使う。それ以外のロジックは
        # 通常チャンクと同じで「current_target を超えてから最初の境界で切る、
        # 境界が無ければ hard_max で強制分割」
        current_target = first_target if not chunks else target

        if len(remaining) <= current_target:
            chunks.append(remaining)
            break

        nl_idx = remaining.find("\n", current_target, hard_max + 1)
        if nl_idx >= 0:
            chunks.append(remaining[:nl_idx])
            remaining = remaining[nl_idx + 1 :]
            continue

        jp_idx = remaining.find("。", current_target, hard_max + 1)
        if jp_idx >= 0:
            chunks.append(remaining[: jp_idx + 1])
            remaining = remaining[jp_idx + 1 :]
            continue

        # 境界なし: hard_max で強制分割
        chunks.append(remaining[:hard_max])
        remaining = remaining[hard_max:]

    # 各チャンクを発話に渡す形に正規化(改行は空白化、前後の空白を落とす)
    return [c.replace("\n", " ").strip() for c in chunks if c.strip()]


# 文単位分解の境界。改行 or 句点で文を切り出す
_SENTENCE_SPLIT = re.compile(r"(?<=[。\n])")


def _split_into_sentences(text: str) -> List[str]:
    """整形済みテキストを文単位に分解。空文字は除外し、各文は末尾の境界
    文字(。 or \n)を含む(後段で改行は空白に変換される)。
    """
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _split_with_cache_aware(text, target, hard_max, first_target, is_cacheable) -> List[str]:
    """キャッシュ対象を独立 chunk として切り出し、非対象は target を超える
    まで合体バッファに溜める。

    挙動の例(target=200):
      sentences = [A, B(キャッシュ対象), C, D, E(キャッシュ対象), F]
      → [A]            (B 到達で flush)
      → [B]            (キャッシュ対象を独立 emit)
      → [C+D]          (target 超過で flush、または E 到達で flush)
      → [E]            (キャッシュ対象を独立 emit)
      → [F]            (末尾の残り)
    """
    chunks: List[str] = []
    buffer = ""
    sentences = _split_into_sentences(text)

    def flush_buffer():
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer)
        buffer = ""

    def current_target() -> int:
        return first_target if not chunks else target

    for sent in sentences:
        if is_cacheable(sent.strip().rstrip("\n")):
            # キャッシュ対象: 直前のバッファを flush して独立 chunk として emit
            flush_buffer()
            chunks.append(sent)
            continue

        # 非対象: 合体バッファに追加
        new_buffer = buffer + sent if buffer else sent
        if len(new_buffer) > current_target():
            # 既存バッファを flush してから今の文を新バッファに
            if buffer:
                flush_buffer()
                buffer = sent
            else:
                # 単一の文が target を超える: hard_max で強制分割
                while len(sent) > hard_max:
                    chunks.append(sent[:hard_max])
                    sent = sent[hard_max:]
                buffer = sent
        else:
            buffer = new_buffer

    flush_buffer()
    return [c.replace("\n", " ").strip() for c in chunks if c.strip()]


def main():
    """整形のみを担う薄い CLI。改行はチャンク境界のヒントとして保持して
    stdout に出す。chunk 分割と cache-aware 判定は scripts/chunk_split.py
    が担うため、本モジュールは cache_patterns に一切依存しない(S-008 / DIP)。
    """
    raw = sys.stdin.read()
    sys.stdout.write(sanitize(raw))


if __name__ == "__main__":
    main()
