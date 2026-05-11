#!/usr/bin/env python3
"""lib_http.py - 簡易 HTTP GET ヘルパー (R-101)

doctor.py / speakers.py / setup.py に重複していた _http_get を一本化。
"""

import urllib.error
import urllib.request
from typing import Optional, Tuple


def http_get(url: str, timeout: float = 3.0) -> Tuple[Optional[str], Optional[str]]:
    """簡易 HTTP GET。成功時は (text, None)、失敗時は (None, error_msg)"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.URLError as e:
        return None, f"URL error: {e}"
    except (TimeoutError, OSError) as e:
        return None, f"connection error: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"unexpected: {e}"
