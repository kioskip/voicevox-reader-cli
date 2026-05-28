"""cache_patterns.py - wav キャッシュ用の正規化テーブル

定型文化することで「言い回しの揺らぎ」を吸収して同じキャッシュエントリに
ヒットさせる。各パターンは「フル行マッチ」(行頭から行末まで `^...$`)
にして、文中の hex のような文字列に誤反応しないようにしている。

新しいパターンを追加する時は:
1. 必ず `^...$` で行頭・行末を固定する(誤マッチ防止)
2. 動的部分(コミットID、数字)は `[0-9a-f]{6,40}` `[0-9]+` などで一般化
3. 出力に `{e}` を含めると、speaker 別の語尾(ENDING_BY_SPEAKER)が差し込まれる
4. tests/test_cache_key.py に正例 + 負例(誤マッチしないこと)を追加
"""

import re
from typing import List, Optional, Tuple


# (compiled regex, replacement template)
NORMALIZE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # === コミット系(揺らぎを吸収して 1 出力に統合)===
    # 吸収する揺らぎ:
    #   - hash 表記: 「[0-9a-f]{6,40}」(地の文)/「ハッシュ」(`<hash>` のインライン
    #                コードが sanitize.read_inline_code で「ハッシュ」と読み替え
    #                られた後の形)
    #   - 表記: 「commit」/「コミット」
    #   - 状態: 「」/「完了」/「済み」(オプション)
    #   - スペース: 「で commit」「でコミット」「commit完了」「commit 完了」など
    # 例にマッチ:
    #   - 1b78c0c で commit。
    #   - abf6939 で commit 完了。
    #   - cea927d で commit 済み。
    #   - ハッシュ で コミット。(バッククォート付きの hash がカナ化された形)
    #   - ハッシュ で コミット 完了。
    #
    # NOTE: 過去にブランチ名前置(「<branch> に <hash> で commit」)も拾う形で
    # 対応していたが、sanitize.py の transform_dict_words で `cache`→「キャッシュ」
    # 等に変換された後では regex がマッチしないため、実環境で機能していなかった。
    # 単独 hash 形式に絞ることでテストと実態の乖離を解消(2026-05-01)
    (re.compile(
        r"^(?:[0-9a-f]{6,40}|ハッシュ)\sで\s?" # <hash> or 「ハッシュ」 で
        r"(?:commit|コミット)"                # commit / コミット
        r"(?:\s?(?:完了|済み))?"               # 状態(任意、間スペースも任意)
        r"。?$"
     ),
     "git で commit した{e}。"),

    # === マージ系(3 揺らぎ → 1 出力)===
    (re.compile(r"^main にマージ(?:します|しました)。?$"),
     "main にマージした{e}。"),
    (re.compile(r"^マージ完了。?$"),
     "main にマージした{e}。"),

    # === 動作確認・テスト系 ===
    (re.compile(r"^(?:syntax|動作確認)\sOK。?$"),
     "動作確認 OK {e}。"),
    # 「件」直前の空白は sanitize.transform_counter_space で削除されるため任意
    # マッチ(空白あり/なしの両方を吸収)。「pytest」と「PASS」前後の空白は残る
    (re.compile(r"^pytest\s[0-9]+\s?件\sPASS。?$"),
     "pytest 全件 PASS {e}。"),

    # === 短い相槌(OK と 了解 を統合)===
    (re.compile(r"^(?:OK|了解)。?$"),
     "了解{e}。"),
    (re.compile(r"^完了しました。?$"),
     "できた{e}。"),

    # === 実装・修正宣言(2 揺らぎ → 1 出力)===
    (re.compile(r"^(?:実装|修正)に入ります。?$"),
     "進める{e}。"),

    # === 確認質問 ===
    (re.compile(r"^これで進めて良いですか。?$"),
     "これで進めて良い{e}?"),

    # === 省略系 ===
    (re.compile(r"^\(以下省略\)$"),
     "(以下省略)"),
]


# speaker ID → 語尾。{e} プレースホルダーに差し込まれる。
# 未登録の speaker は DEFAULT_ENDING(空文字)が使われる
ENDING_BY_SPEAKER = {
    2:  "ですわ",      # 四国めたん(ノーマル)
    3:  "のだ",        # ずんだもん(ノーマル)
    8:  "ね",          # 春日部つむぎ(ノーマル)
    10: "ですよ",       # 雨晴はう(ノーマル)
    14: "ですね",       # 冥鳴ひまり(ノーマル)
    74: "ですね",       # 琴詠ニア(ノーマル)
}

DEFAULT_ENDING = ""


def get_ending(speaker_id: int) -> str:
    return ENDING_BY_SPEAKER.get(speaker_id, DEFAULT_ENDING)


def normalize(text: str, speaker_id: int) -> Optional[str]:
    """text が定型文パターンにマッチするなら、{e} を speaker 別の語尾で
    差し替えた正規化文字列を返す。マッチしなければ None を返す
    (=キャッシュ対象外であることを呼び出し側に伝える)。
    """
    ending = get_ending(speaker_id)
    for pattern, template in NORMALIZE_PATTERNS:
        if pattern.match(text):
            return template.replace("{e}", ending)
    return None
