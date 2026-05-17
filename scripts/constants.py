"""scripts/constants.py - テキスト処理の定数一元管理

settings.py の SCHEMA デフォルト値と sanitize.py のデフォルト値を
ここに集約し、マジックナンバーの重複を排除する。
"""

# テキスト全体文字数
MAX_CHARS_DEFAULT: int = 500
# maxChars = 0 は「上限なし」として解釈し、この値をキャップとして使う
MAX_CHARS_LIMIT: int = 9999

# chunking パラメータ
CHUNK_CHARS_DEFAULT: int = 200
CHUNK_HARD_MAX_DEFAULT: int = 400
INLINE_CODE_LIMIT_DEFAULT: int = 25
FIRST_CHUNK_CHARS_DEFAULT: int = 30

# チャンク数上限（0 = 無制限）
MAX_CHUNKS_DEFAULT: int = 0

# 省略通知（maxChars / maxChunks の両方で共有）
TRUNCATION_SUFFIX: str = "(以下省略)"
