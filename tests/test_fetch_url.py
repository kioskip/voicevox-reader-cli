"""tests/test_fetch_url.py - fetch_url.py のユニットテスト (B-003)"""

import socket
import urllib.error
import urllib.request
from io import BytesIO
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

import fetch_url as furl


# ---------------------------------------------------------------------------
# モックレスポンスヘルパー
# ---------------------------------------------------------------------------


class _MockHeaders:
    def __init__(self, content_type: str, charset: Optional[str] = None) -> None:
        self._content_type = content_type
        self._charset = charset

    def get(self, key: str, default: str = "") -> str:
        if key == "Content-Type":
            return self._content_type
        return default

    def get_content_charset(self) -> Optional[str]:
        return self._charset


class _MockResponse:
    def __init__(
        self,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        url: str = "https://example.com",
        charset: Optional[str] = "utf-8",
    ) -> None:
        self._body = body
        self._url = url
        self.headers = _MockHeaders(content_type, charset)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self) -> str:
        return self._url

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            return self._body
        return self._body[:n]


def _mock_urlopen(body: bytes, **kwargs) -> _MockResponse:
    return _MockResponse(body, **kwargs)


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


class TestFetchUrlBasic:
    def test_simple_html_extracts_text(self):
        html = b"<html><body><p>Hello World</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Hello World" in text

    def test_script_tag_is_excluded(self):
        html = b"<html><body><p>Good</p><script>alert('bad')</script></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Good" in text
        assert "alert" not in text
        assert "bad" not in text

    def test_style_tag_is_excluded(self):
        html = b"<html><head><style>body{color:red}</style></head><body><p>Visible</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Visible" in text
        assert "color" not in text

    def test_nav_tag_is_excluded(self):
        html = b"<html><body><nav><a>Menu</a></nav><main><p>Content</p></main></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Content" in text
        assert "Menu" not in text

    def test_nested_tag_inside_nav_excluded(self):
        """nav内のネストしたタグもスキップされること(depth管理)"""
        html = (
            b"<html><body><nav><div><ul><li><a>Deep nested</a></li></ul></div></nav>"
            b"<p>Main content</p></body></html>"
        )
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Main content" in text
        assert "Deep nested" not in text

    def test_header_footer_excluded(self):
        html = (
            b"<html><body><header>Site header</header>"
            b"<p>Article</p><footer>Copyright</footer></body></html>"
        )
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Article" in text
        assert "Site header" not in text
        assert "Copyright" not in text

    def test_html_entity_expanded(self):
        """&amp; 等の HTML entity が展開されること"""
        html = b"<html><body><p>AT&amp;T &lt;example&gt;</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "AT&T" in text
        assert "<example>" in text
        assert "&amp;" not in text


# ---------------------------------------------------------------------------
# charset 処理
# ---------------------------------------------------------------------------


class TestCharsetDetection:
    def test_shift_jis_decoded_correctly(self):
        text_jp = "テスト"
        body = text_jp.encode("shift_jis")
        resp = _MockResponse(
            body,
            content_type="text/html; charset=Shift_JIS",
            charset="Shift_JIS",
        )
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com")
        assert "テスト" in text

    def test_shift_jis_via_meta_charset(self):
        """HTTPヘッダにcharsetがなくても<meta charset>から検出できること(青空文庫等)"""
        body_str = '<html><head><meta charset="Shift_JIS"></head><body><p>青空文庫</p></body></html>'
        body = body_str.encode("shift_jis")
        # HTTPヘッダには charset なし
        resp = _MockResponse(body, content_type="text/html", charset=None)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://www.aozora.gr.jp/")
        assert "青空文庫" in text

    def test_shift_jis_via_meta_http_equiv(self):
        """<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS"> から検出"""
        body_str = (
            '<html><head>'
            '<meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">'
            '</head><body><p>テキスト</p></body></html>'
        )
        body = body_str.encode("shift_jis")
        resp = _MockResponse(body, content_type="text/html", charset=None)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com")
        assert "テキスト" in text

    def test_utf8_fallback_when_charset_absent(self):
        body = "<p>ASCII OK</p>".encode("utf-8")
        resp = _MockResponse(body, content_type="text/html", charset=None)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com")
        assert "ASCII OK" in text


# ---------------------------------------------------------------------------
# text/plain
# ---------------------------------------------------------------------------


class TestTextPlain:
    def test_plain_text_extracted_without_html_parsing(self):
        body = b"Hello plain world"
        resp = _MockResponse(body, content_type="text/plain; charset=utf-8", charset="utf-8")
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com")
        assert "Hello plain world" in text

    def test_plain_text_entities_not_unescaped(self):
        """text/plain では &amp; はそのまま残す"""
        body = b"A &amp; B"
        resp = _MockResponse(body, content_type="text/plain", charset=None)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com")
        assert "&amp;" in text


# ---------------------------------------------------------------------------
# Content-Type 制限
# ---------------------------------------------------------------------------


class TestContentTypeRestriction:
    def test_pdf_raises_runtime_error(self):
        resp = _MockResponse(b"%PDF-1.4", content_type="application/pdf")
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="unsupported Content-Type"):
                furl.fetch_url("https://example.com/doc.pdf")

    def test_image_raises_runtime_error(self):
        resp = _MockResponse(b"\xff\xd8\xff", content_type="image/jpeg")
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="unsupported Content-Type"):
                furl.fetch_url("https://example.com/photo.jpg")


# ---------------------------------------------------------------------------
# レスポンスサイズ制限
# ---------------------------------------------------------------------------


class TestResponseSizeLimit:
    def test_oversized_response_raises(self):
        # MAX_RESPONSE_BYTES + 1 バイト を返す
        too_large = b"x" * (furl.MAX_RESPONSE_BYTES + 1)
        resp = _MockResponse(too_large)
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="exceeds"):
                furl.fetch_url("https://example.com")

    def test_exactly_max_bytes_ok(self):
        # read(MAX + 1) が MAX バイト返した場合 → OK
        at_limit = b"<p>" + b"a" * (furl.MAX_RESPONSE_BYTES - 10) + b"</p>"

        class _SizedResponse(_MockResponse):
            def read(self, n=-1):
                data = self._body
                if n >= 0:
                    data = data[:n]
                return data

        resp = _SizedResponse(at_limit[: furl.MAX_RESPONSE_BYTES])
        with patch("urllib.request.urlopen", return_value=resp):
            # RuntimeError が発生しないことを確認（本文が空でなければOK）
            result = furl.fetch_url("https://example.com")
        assert result  # 何らかのテキストが返る


# ---------------------------------------------------------------------------
# ネットワークエラー
# ---------------------------------------------------------------------------


class TestNetworkErrors:
    def test_url_error_raises_runtime_error(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with pytest.raises(RuntimeError, match="URL error"):
                furl.fetch_url("https://example.com")

    def test_timeout_raises_runtime_error(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with pytest.raises(RuntimeError, match="connection error"):
                furl.fetch_url("https://example.com")


# ---------------------------------------------------------------------------
# URL検証
# ---------------------------------------------------------------------------


class TestUrlValidation:
    def test_http_scheme_accepted(self):
        body = b"<p>OK</p>"
        resp = _MockResponse(body, url="http://example.com")
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("http://example.com")
        assert "OK" in text

    def test_file_scheme_rejected(self):
        """初期URLのschemeをPython層でチェックすること"""
        with pytest.raises(RuntimeError, match="unsupported URL scheme"):
            furl.fetch_url("file:///etc/passwd")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(RuntimeError, match="unsupported URL scheme"):
            furl.fetch_url("ftp://example.com/file")

    def test_userinfo_rejected(self):
        with pytest.raises(RuntimeError, match="userinfo"):
            furl.fetch_url("https://user:password@example.com/")

    def test_redirect_to_javascript_rejected(self):
        """redirect後のURLがhttps以外ならエラー"""
        resp = _MockResponse(b"<p>text</p>", url="javascript:alert(1)")
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="unsupported URL scheme"):
                furl.fetch_url("https://example.com")


# ---------------------------------------------------------------------------
# fragment ジャンプ
# ---------------------------------------------------------------------------


class TestFragmentJump:
    def test_fragment_by_id_starts_from_anchor(self):
        """id属性が一致する要素から本文収集を開始すること"""
        html = (
            b"<html><body>"
            b"<p>Before</p>"
            b'<h2 id="target">Here</h2>'
            b"<p>After</p>"
            b"</body></html>"
        )
        resp = _MockResponse(html)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com/page#target")
        assert "Here" in text
        assert "After" in text
        assert "Before" not in text

    def test_fragment_by_name_attribute(self):
        """name属性(青空文庫スタイル)でも開始位置を特定できること"""
        html = (
            "<html><body>"
            "<p>前の段落</p>"
            '<a name="midashi40">見出し</a>'
            "<p>本文テキスト</p>"
            "</body></html>"
        ).encode("utf-8")
        resp = _MockResponse(html)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com/page#midashi40")
        assert "見出し" in text
        assert "本文テキスト" in text
        assert "前の段落" not in text

    def test_fragment_not_found_falls_back_to_full_page(self):
        """fragmentが見つからない場合はページ全体を返すこと"""
        html = b"<html><body><p>Full content</p></body></html>"
        resp = _MockResponse(html)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com/page#nonexistent")
        assert "Full content" in text

    def test_no_fragment_returns_full_page(self):
        """fragmentなしはページ全体を返すこと(既存挙動の維持)"""
        html = b"<html><body><p>All content</p></body></html>"
        resp = _MockResponse(html)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com/page")
        assert "All content" in text

    def test_title_always_included_regardless_of_fragment(self):
        """fragmentが指定されていてもtitleは常に先頭に付与されること"""
        html = (
            b"<html><head><title>Page Title</title></head><body>"
            b"<p>Before</p>"
            b'<section id="section2"><p>Section content</p></section>'
            b"</body></html>"
        )
        resp = _MockResponse(html)
        with patch("urllib.request.urlopen", return_value=resp):
            text = furl.fetch_url("https://example.com/page#section2")
        assert "Page Title" in text
        assert "Section content" in text
        assert "Before" not in text


# ---------------------------------------------------------------------------
# ruby 展開
# ---------------------------------------------------------------------------


class TestRubyHandling:
    def test_rt_reading_used_base_skipped(self):
        """<ruby>漢字<rt>よみ</rt></ruby>の読みが使われ漢字は除外されること"""
        html = b"<html><body><ruby>\xe8\xa6\xaa\xe8\xae\x93<rt>\xe3\x81\x8a\xe3\x82\x84\xe3\x82\x86\xe3\x81\x9a</rt></ruby>\xe3\x82\x8a</body></html>"
        # "親譲" の UTF-8 + rt "おやゆず" + "り"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "おやゆず" in text
        assert "り" in text
        # 漢字の素テキスト"親譲"がそのまま残っていないこと(RTが使われている)
        assert text.count("おやゆず") == 1

    def test_rt_and_following_char_no_space(self):
        """ruby読みと直後の文字の間にスペースがないこと"""
        html = "<html><body><ruby>字<rt>じ</rt></ruby>を</body></html>".encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "じを" in text  # スペースなし

    def test_aozora_style_ruby_rb_rp_rt(self):
        """青空文庫形式 <ruby><rb>漢字</rb><rp>（</rp><rt>よみ</rt><rp>）</rp></ruby>"""
        html = (
            "<html><body>"
            "<ruby><rb>親譲</rb><rp>（</rp><rt>おやゆず</rt><rp>）</rp></ruby>り"
            "</body></html>"
        ).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "おやゆずり" in text
        assert "（" not in text
        assert "）" not in text
        assert "親譲" not in text

    def test_ruby_without_rt_uses_base_text(self):
        """<rt>がない<ruby>ではbaseテキストを使うこと"""
        html = "<html><body><ruby>漢字</ruby>テスト</body></html>".encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "漢字" in text

    def test_cjk_no_space_between_chars(self):
        """日本語文字間のスペースが除去されること"""
        html = "<html><body><p>あ</p><p>い</p></body></html>".encode("utf-8")
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        # ブロックタグによる改行は残るが、スペースは除去される
        assert "あ い" not in text

    def test_ascii_space_preserved(self):
        """英語テキストの単語間スペースは維持されること"""
        html = b"<html><body><p>Hello World</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "Hello World" in text


# ---------------------------------------------------------------------------
# SSRF チェック（strict_ssrf=True）
# ---------------------------------------------------------------------------


def _make_getaddrinfo_mock(ip: str):
    """指定 IP を返す socket.getaddrinfo モックを作る。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]


class TestSsrfCheck:
    """strict_ssrf=True 時の SSRF ブロック確認。socket.getaddrinfo をモックして使用。"""

    def _ssrf_raises(self, url: str, ip: str) -> None:
        with patch("fetch_url.socket.getaddrinfo", return_value=_make_getaddrinfo_mock(ip)):
            with pytest.raises(RuntimeError, match="SSRF blocked"):
                furl.fetch_url(url, strict_ssrf=True)

    def test_loopback_ipv4_blocked(self):
        self._ssrf_raises("http://localhost/", "127.0.0.1")

    def test_loopback_explicit_ip_blocked(self):
        self._ssrf_raises("http://127.0.0.1/", "127.0.0.1")

    def test_private_class_c_blocked(self):
        self._ssrf_raises("http://192.168.1.1/", "192.168.1.1")

    def test_link_local_metadata_blocked(self):
        self._ssrf_raises("http://169.254.169.254/", "169.254.169.254")

    def test_loopback_ipv6_blocked(self):
        with patch(
            "fetch_url.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))],
        ):
            with pytest.raises(RuntimeError, match="SSRF blocked"):
                furl.fetch_url("http://[::1]/", strict_ssrf=True)

    def test_dns_resolution_failure_blocked(self):
        """解決不能なホストは拒否する。"""
        with patch(
            "fetch_url.socket.getaddrinfo",
            side_effect=socket.gaierror("Name or service not known"),
        ):
            with pytest.raises(RuntimeError, match="hostname resolution failed"):
                furl.fetch_url("http://no-such-host.invalid/", strict_ssrf=True)

    def test_redirect_to_private_ip_blocked(self):
        """strict_ssrf=True では redirect 先の内部 IP もブロックされること。"""
        # _fetch_strict_ssrf が使われ、_RedirectException 後に _check_ssrf が走る
        def fake_getaddrinfo(host, port, *args, **kwargs):
            # 初回（初期 URL の external.com）はグローバル IP を返す
            if host == "external.com":
                return _make_getaddrinfo_mock("203.0.113.1")
            # redirect 先の internal.example は内部 IP を返す
            return _make_getaddrinfo_mock("10.0.0.1")

        class FakeOpener:
            call_count = 0

            def open(self, req, timeout=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise furl._RedirectException("http://internal.example/", 302)
                return _MockResponse(b"<p>secret</p>", url="http://internal.example/")

        with patch("fetch_url.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            with patch("fetch_url.urllib.request.build_opener", return_value=FakeOpener()):
                with pytest.raises(RuntimeError, match="SSRF blocked"):
                    furl.fetch_url("http://external.com/", strict_ssrf=True)

    def test_strict_ssrf_false_does_not_call_getaddrinfo(self):
        """デフォルト(strict_ssrf=False)では getaddrinfo が呼ばれないこと。"""
        html = b"<p>OK</p>"
        with patch("fetch_url.socket.getaddrinfo") as mock_gai:
            with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
                text = furl.fetch_url("http://localhost:3000/page")
        assert "OK" in text
        mock_gai.assert_not_called()


# ---------------------------------------------------------------------------
# title 抽出
# ---------------------------------------------------------------------------


class TestTitleExtraction:
    def test_title_prepended_to_body(self):
        html = b"<html><head><title>My Page</title></head><body><p>Content</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert text.startswith("My Page")
        assert "Content" in text

    def test_title_before_body_text(self):
        """titleがbody本文より前に来ること"""
        html = b"<html><head><title>Title</title></head><body><p>Body</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert text.index("Title") < text.index("Body")

    def test_no_title_still_works(self):
        """<title>なしでも本文が抽出できること"""
        html = b"<html><body><p>No title</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert "No title" in text

    def test_title_not_duplicated_in_body(self):
        """titleテキストがbody部分に重複して現れないこと"""
        html = b"<html><head><title>Special Title</title></head><body><p>Body text</p></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            text = furl.fetch_url("https://example.com")
        assert text.count("Special Title") == 1


# ---------------------------------------------------------------------------
# 空コンテンツ
# ---------------------------------------------------------------------------


class TestEmptyContent:
    def test_empty_html_body_raises(self):
        """本文が空ならエラー"""
        html = b"<html><body></body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            with pytest.raises(RuntimeError, match="no readable text found"):
                furl.fetch_url("https://example.com")

    def test_whitespace_only_raises(self):
        html = b"<html><body>   \n\n   </body></html>"
        with patch("urllib.request.urlopen", return_value=_MockResponse(html)):
            with pytest.raises(RuntimeError, match="no readable text found"):
                furl.fetch_url("https://example.com")
