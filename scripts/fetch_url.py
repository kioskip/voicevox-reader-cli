#!/usr/bin/env python3
"""fetch_url.py - URLからWebページ本文を取得してテキスト化 (B-003)

Usage:
    fetch_url.py <url>

成功時は本文テキストを stdout に出力。失敗時は stderr にエラーを書いて exit 1。
URL に fragment (#id) が含まれる場合、該当要素から本文収集を開始する。

SSRF注記:
    初回実装はローカルCLIでユーザーが明示指定する用途のため localhost/LAN内URLを禁止しない。
    将来 Claude Code / MCP 等の外部入力から自動実行する場合は SSRF 対策（プライベートIPブロック等）が必要。
"""

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import List, Optional

FETCH_TIMEOUT_SEC: float = 10.0
MAX_RESPONSE_BYTES: int = 2 * 1024 * 1024  # 2 MiB
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain"})
_SKIP_TAGS = frozenset(
    {"script", "style", "nav", "header", "footer", "aside", "form", "noscript"}
)
_BLOCK_TAGS = frozenset({"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr"})


def _detect_meta_charset(raw: bytes) -> Optional[str]:
    """<meta charset=...> / <meta http-equiv Content-Type charset=...> を先頭4KBで探す。

    HTTPヘッダにcharsetがない場合のフォールバック（Shift-JIS等の古いサイト向け）。
    latin-1は全バイト値に対応するため、エンコード問わず先頭をASCII互換として読める。
    """
    head = raw[:4096].decode("latin-1", errors="replace")
    m = re.search(
        r'<meta\b[^>]*\bcharset=["\']?\s*([^"\';\s>]+)',
        head,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    # <meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">
    m = re.search(
        r'<meta\b[^>]*\bcontent=["\'][^"\']*;\s*charset=([^"\';\s>]+)',
        head,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip() or None
    return None


class _TextExtractor(HTMLParser):
    """HTMLからテキストを抽出する。<script>/<style>/ナビ等はスキップ。
    <ruby>は<rt>読みを優先して展開。<rb>/<rp>はスキップ。<title>は別枠で収集。

    fragment を指定すると id または name 属性が一致する要素から本文収集を開始する。
    """

    def __init__(self, fragment: Optional[str] = None) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth: int = 0
        self._parts: List[str] = []
        self._title_parts: List[str] = []
        self._in_title: bool = False
        self._in_ruby: bool = False
        self._in_rt: bool = False
        self._ruby_base: List[str] = []  # <rt>がない場合のフォールバック(漢字等)
        self._rt_parts: List[str] = []   # <rt>内の読み
        self._fragment: Optional[str] = fragment
        # fragment未指定なら最初からTrue、指定時は見つかるまでFalse
        self.fragment_found: bool = not bool(fragment)

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()

        # fragment 探索: 他の処理より先に id/name をチェック
        if not self.fragment_found:
            attrs_dict = dict(attrs)
            if (attrs_dict.get("id") == self._fragment or
                    attrs_dict.get("name") == self._fragment):
                self.fragment_found = True

        if t == "ruby":
            self._in_ruby = True
            self._ruby_base = []
            self._rt_parts = []
        elif t == "rt":
            self._in_rt = True
        elif t == "title":
            self._in_title = True
        elif t in _SKIP_TAGS:
            self._skip_depth += 1
        elif (t in _BLOCK_TAGS and self._skip_depth == 0
              and not self._in_title and not self._in_ruby
              and self.fragment_found):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "ruby":
            # <rt>があればそれを読みとして使用、なければbaseテキスト(漢字)を使用
            if self.fragment_found:
                reading = "".join(self._rt_parts) or "".join(self._ruby_base)
                if reading:
                    self._parts.append(reading)
            self._in_ruby = False
            self._in_rt = False
            self._ruby_base = []
            self._rt_parts = []
        elif t == "rt":
            self._in_rt = False
        elif t == "title":
            self._in_title = False
        elif t in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if not stripped:
            return
        if self._in_title:
            self._title_parts.append(stripped)
            return
        if not self.fragment_found:
            return
        if self._in_rt:
            self._rt_parts.append(stripped)
        elif self._in_ruby:
            self._ruby_base.append(stripped)  # <rb>/<rp> 等のフォールバック用
        elif self._skip_depth == 0:
            self._parts.append(stripped)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        # 日本語テキストでは非ASCII文字間のスペースは不要
        # (ruby読みと後続文字の連結: "おやゆず り" → "おやゆずり")
        return re.sub(r"(?<=[^\x00-\x7F]) +(?=[^\x00-\x7F])", "", raw)

    @property
    def title(self) -> str:
        return " ".join(self._title_parts).strip()


def _validate_url(url: str) -> None:
    """URLのschemeとuserinfoを検証。不正なら RuntimeError を raise。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise RuntimeError(f"unsupported URL scheme: {parsed.scheme!r} (http/https only)")
    if "@" in parsed.netloc:
        raise RuntimeError("URL with userinfo (user:password@host) is not allowed")


def _validate_redirect_url(url: str) -> None:
    """redirect後のURLのschemeを検証。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise RuntimeError(f"redirect to unsupported scheme: {parsed.scheme!r}")


def fetch_url(url: str) -> str:
    """URLからWebページを取得し本文テキストを返す。失敗時は RuntimeError を raise。"""
    _validate_url(url)

    # fragment を取り出す(urllib はサーバーに送らないため手動で取得)
    fragment = urllib.parse.urlsplit(url).fragment or None

    req = urllib.request.Request(url, headers={"User-Agent": "vvread/1.0 (URL reader)"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:  # noqa: S310
            # redirect後URLのscheme検証
            redirect_url = resp.geturl()
            _validate_redirect_url(redirect_url)

            # Content-Typeをbody読み込み前に検証
            content_type_header = resp.headers.get("Content-Type", "")
            mime = content_type_header.split(";")[0].strip().lower()
            if mime and mime not in _ALLOWED_CONTENT_TYPES:
                raise RuntimeError(f"unsupported Content-Type: {mime!r} (text/html or text/plain only)")

            # charsetはbody読み込み後に meta フォールバックする可能性があるため先に None で取得
            header_charset = resp.headers.get_content_charset()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as e:
        raise RuntimeError(f"URL error: {e}") from e
    except (TimeoutError, OSError) as e:
        raise RuntimeError(f"connection error: {e}") from e

    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError(
            f"response body exceeds {MAX_RESPONSE_BYTES // 1024 // 1024} MiB limit"
        )

    # charset 優先順: HTTPヘッダ > <meta charset> > utf-8
    charset = header_charset or _detect_meta_charset(raw) or "utf-8"
    html_text = raw.decode(charset, errors="replace")

    if "text/plain" in mime:
        text = re.sub(r" +", " ", html_text)
    else:
        parser = _TextExtractor(fragment=fragment)
        parser.feed(html_text)

        # fragment が見つからなかった場合はページ全体を使用
        if fragment and not parser.fragment_found:
            print(f"vvread url: fragment #{fragment} not found, reading full page",
                  file=sys.stderr)
            parser = _TextExtractor()
            parser.feed(html_text)

        body = parser.get_text()
        title = parser.title
        text = f"{title}\n\n{body}" if title else body

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise RuntimeError("no readable text found")

    return text


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: fetch_url.py <url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    try:
        content = fetch_url(url)
    except RuntimeError as e:
        print(f"vvread url: {e}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.write(content)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
