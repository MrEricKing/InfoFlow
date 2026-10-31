from __future__ import annotations

CAPTURE_BROWSER_LOCALE = "zh-CN"
CAPTURE_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"


def build_accept_language_headers() -> dict[str, str]:
    return {"Accept-Language": CAPTURE_ACCEPT_LANGUAGE}
