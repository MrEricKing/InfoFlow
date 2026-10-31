from __future__ import annotations

import builtins
import asyncio
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from app.core.safety import CAPTURE_RESPONSE_MAX_BYTES, URLSafetyError
from app.services.capture_tools import crawl4ai_wrapper
from app.services.capture_tools.models import CaptureToolUnavailableError


@pytest.fixture(autouse=True)
def _bypass_network_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        crawl4ai_wrapper,
        "_preflight_capture_url",
        lambda url: crawl4ai_wrapper.validate_public_http_url(
            url,
            purpose="Crawl4AI capture",
            resolve_dns=False,
        ),
    )


def test_capture_markdown_returns_raw_fit_and_downloaded_images(monkeypatch) -> None:
    fake_crawl4ai = install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/1",
            markdown=SimpleNamespace(
                raw_markdown="# Raw\n\n![Hero](/hero.png)\n",
                fit_markdown="# Fit\n\n![Hero](/hero.png)\n",
            ),
            media={"images": [{"src": "/hero.png", "alt": "Hero"}]},
        ),
    )
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", FakeHttpxClient)

    result = crawl4ai_wrapper.capture_markdown(" https://1.1.1.1/articles/1 ")

    assert result.raw_markdown == "# Raw\n\n![Hero](https://1.1.1.1/hero.png)"
    assert result.fit_markdown == "# Fit\n\n![Hero](https://1.1.1.1/hero.png)"
    assert len(result.image_assets) == 1
    assert result.image_assets[0].source_url == "https://1.1.1.1/hero.png"
    assert result.image_assets[0].mime_type == "image/png"
    assert result.image_assets[0].file_ext == "png"
    assert result.image_assets[0].content_bytes == b"PNG!"
    assert result.diagnostics == []
    assert fake_crawl4ai.last_run_config["run"]["page_timeout"] == crawl4ai_wrapper.CRAWL4AI_PAGE_TIMEOUT_MS


def test_safe_image_get_avoids_brotli_for_real_httpx_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_safe_http_get(client, url, *, headers, max_bytes, purpose, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["max_bytes"] = max_bytes
        captured["purpose"] = purpose
        return FakeResponse()

    monkeypatch.setattr(crawl4ai_wrapper, "safe_http_get", fake_safe_http_get)

    with crawl4ai_wrapper.httpx.Client() as client:
        response = crawl4ai_wrapper._safe_image_get(client, "https://1.1.1.1/avatar.image")

    assert response.content == b"PNG!"
    assert captured == {
        "url": "https://1.1.1.1/avatar.image",
        "headers": crawl4ai_wrapper.CRAWL4AI_IMAGE_DOWNLOAD_HEADERS,
        "max_bytes": crawl4ai_wrapper.CRAWL4AI_MAX_IMAGE_BYTES,
        "purpose": "Crawl4AI image download",
    }


def test_capture_markdown_configures_dynamic_content_loading(monkeypatch) -> None:
    fake_crawl4ai = install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/1",
            markdown=SimpleNamespace(
                raw_markdown="# Raw\n\nLoaded comment\n",
                fit_markdown="# Fit\n\nLoaded comment\n",
            ),
            media={"images": []},
        ),
    )
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", FakeHttpxClient)

    crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/1")

    run_config = fake_crawl4ai.last_run_config["run"]
    browser_config = fake_crawl4ai.last_browser_config["browser"]
    assert run_config["locale"] == crawl4ai_wrapper.CAPTURE_BROWSER_LOCALE
    assert run_config["wait_for_images"] is True
    assert run_config["delay_before_return_html"] == crawl4ai_wrapper.CRAWL4AI_DELAY_BEFORE_RETURN_HTML_SECONDS
    assert run_config["scan_full_page"] is True
    assert run_config["scroll_delay"] == crawl4ai_wrapper.CRAWL4AI_SCROLL_DELAY_SECONDS
    assert run_config["max_scroll_steps"] == crawl4ai_wrapper.CRAWL4AI_MAX_SCROLL_STEPS
    assert run_config["process_iframes"] is True
    assert run_config["flatten_shadow_dom"] is True
    assert "__infoflowCrawl4AIPreCapture" in run_config["js_code"]
    assert browser_config["headers"] == crawl4ai_wrapper.CRAWL4AI_BROWSER_HEADERS
    assert f"--lang={crawl4ai_wrapper.CAPTURE_BROWSER_LOCALE}" in browser_config["extra_args"]


def test_capture_markdown_ignores_ad_probe_images(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/1",
            markdown=SimpleNamespace(
                raw_markdown=(
                    "# Raw\n\n"
                    "![Ad](https://pagead2.googlesyndication.com/pagead/sodar?id=sodar2)\n\n"
                    "![Hero](/hero.png)\n\n"
                    "Article body\n"
                ),
                fit_markdown="# Fit\n\nArticle body\n",
            ),
            media={
                "images": [
                    {
                        "src": "https://pagead2.googlesyndication.com/pagead/sodar?id=sodar2",
                        "alt": None,
                    },
                    {"src": "/hero.png", "alt": "Hero"},
                ]
            },
        ),
    )
    RecordingHttpxClient.requested_urls = []
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", RecordingHttpxClient)

    result = crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/1")

    assert result.diagnostics == []
    assert "googlesyndication" not in result.raw_markdown
    assert result.raw_markdown == "# Raw\n\n\n\n![Hero](https://1.1.1.1/hero.png)\n\nArticle body"
    assert [asset.source_url for asset in result.image_assets] == ["https://1.1.1.1/hero.png"]
    assert RecordingHttpxClient.requested_urls == ["https://1.1.1.1/hero.png"]


def test_capture_markdown_keeps_markdown_when_image_download_fails(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/1",
            markdown=SimpleNamespace(
                raw_markdown="# Raw\n\n![Hero](https://1.1.1.1/missing.png)\n",
                fit_markdown="# Fit\n",
            ),
            media={"images": []},
        ),
    )
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", FailingHttpxClient)

    result = crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/1")

    assert result.raw_markdown.startswith("# Raw")
    assert result.fit_markdown.startswith("# Fit")
    assert result.image_assets == []
    assert result.diagnostics[0].level == "warning"
    assert result.diagnostics[0].code == "crawl4ai_image_download_failed"


def test_capture_markdown_skips_html_image_refs_without_failing_markdown(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/1",
            markdown=SimpleNamespace(
                raw_markdown="# Raw\n\n![Hero](https://1.1.1.1/articles/1)\n",
                fit_markdown="# Fit\n",
            ),
            media={"images": []},
        ),
    )
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", HtmlHttpxClient)

    result = crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/1")

    assert result.raw_markdown.startswith("# Raw")
    assert result.fit_markdown.startswith("# Fit")
    assert result.image_assets == []
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].level == "warning"
    assert result.diagnostics[0].code == "crawl4ai_image_unsupported_type"
    assert "text/html" in result.diagnostics[0].message


def test_capture_markdown_raises_for_failed_crawl(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=False,
            error_message="blocked",
            status_code=403,
        ),
    )

    with pytest.raises(crawl4ai_wrapper.Crawl4AICaptureError, match="blocked"):
        crawl4ai_wrapper.capture_markdown("https://1.1.1.1/blocked")


def test_capture_markdown_rejects_oversized_browser_result(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/huge",
            markdown=SimpleNamespace(
                raw_markdown="x" * (CAPTURE_RESPONSE_MAX_BYTES + 1),
                fit_markdown="# Fit\n",
            ),
            media={"images": []},
        ),
    )

    with pytest.raises(crawl4ai_wrapper.Crawl4AICaptureError, match="raw_markdown exceeds"):
        crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/huge")


def test_capture_markdown_rejects_oversized_browser_html(monkeypatch) -> None:
    install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="https://1.1.1.1/articles/huge-html",
            markdown=SimpleNamespace(
                raw_markdown="# Raw\n",
                fit_markdown="# Fit\n",
            ),
            media={"images": []},
            html="x" * (CAPTURE_RESPONSE_MAX_BYTES + 1),
        ),
    )

    with pytest.raises(crawl4ai_wrapper.Crawl4AICaptureError, match="raw_html exceeds"):
        crawl4ai_wrapper.capture_markdown("https://1.1.1.1/articles/huge-html")


def test_capture_markdown_rejects_blank_url() -> None:
    with pytest.raises(ValueError, match="URL must not be empty"):
        crawl4ai_wrapper.capture_markdown("   ")


def test_capture_markdown_rejects_private_url_before_crawl_dependency_check() -> None:
    with pytest.raises(ValueError, match="non-public"):
        crawl4ai_wrapper.capture_markdown("http://127.0.0.1/internal")


def test_crawl4ai_fallback_fetch_rejects_hostname_resolving_to_private_ip(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "public.example.com"
        return [(None, None, None, None, ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    async def run_fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok", request=request))
        ) as client:
            await crawl4ai_wrapper._async_safe_http_get(
                client,
                "https://public.example.com/article",
                max_bytes=1024,
                purpose="Crawl4AI fallback fetch",
            )

    with pytest.raises(URLSafetyError, match="non-public"):
        asyncio.run(run_fetch())


def test_crawl4ai_fallback_fetch_rejects_redirect_hostname_resolving_to_private_ip(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        address = "93.184.216.34" if host == "public.example.com" else "127.0.0.1"
        return [(None, None, None, None, (address, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://public.example.com/article":
            return httpx.Response(
                302,
                headers={"Location": "https://rebound.example.com/private"},
                request=request,
            )
        return httpx.Response(200, text="private", request=request)

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    async def run_fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await crawl4ai_wrapper._async_safe_http_get(
                client,
                "https://public.example.com/article",
                max_bytes=1024,
                purpose="Crawl4AI fallback fetch",
            )

    with pytest.raises(URLSafetyError, match="non-public"):
        asyncio.run(run_fetch())


def test_capture_markdown_reports_unavailable_dependency(monkeypatch) -> None:
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "crawl4ai":
            raise ImportError("missing crawl4ai")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(CaptureToolUnavailableError):
        crawl4ai_wrapper.capture_markdown("https://1.1.1.1/missing-dependency")


def test_capture_uploaded_html_markdown_uses_raw_html_without_image_download(monkeypatch) -> None:
    fake_crawl4ai = install_fake_crawl4ai_modules(
        monkeypatch,
        result=SimpleNamespace(
            success=True,
            url="raw:",
            markdown=SimpleNamespace(
                raw_markdown="# Uploaded\n\n![Inline](data:image/png;base64,abc)\n",
                fit_markdown="# Uploaded\n\nBody\n",
            ),
            media={"images": [{"src": "https://1.1.1.1/remote.png", "alt": "Remote"}]},
            html="<html><title>Uploaded Title</title><body>Body</body></html>",
        ),
    )
    RecordingHttpxClient.requested_urls = []
    monkeypatch.setattr(crawl4ai_wrapper.httpx, "Client", RecordingHttpxClient)

    result = crawl4ai_wrapper.capture_uploaded_html_markdown(
        "<html><title>Uploaded Title</title><body>Body</body></html>",
        source_name="upload.html",
    )

    assert result.raw_markdown.startswith("# Uploaded")
    assert result.fit_markdown == "# Uploaded\n\nBody"
    assert result.image_assets == []
    assert result.diagnostics == []
    assert result.page_title == "Uploaded Title"
    assert RecordingHttpxClient.requested_urls == []
    assert fake_crawl4ai.last_run_config["run"]["wait_for_images"] is False
    assert fake_crawl4ai.last_run_config["run"]["scan_full_page"] is False


def install_fake_crawl4ai_modules(monkeypatch, *, result) -> ModuleType:
    crawl4ai = ModuleType("crawl4ai")
    crawl4ai.last_browser_config = None
    crawl4ai.last_run_config = None
    crawl4ai.AsyncWebCrawler = lambda config=None: FakeAsyncWebCrawler(result)

    def fake_browser_config(**kwargs):
        crawl4ai.last_browser_config = {"browser": kwargs}
        return crawl4ai.last_browser_config

    def fake_run_config(**kwargs):
        crawl4ai.last_run_config = {"run": kwargs}
        return crawl4ai.last_run_config

    crawl4ai.BrowserConfig = fake_browser_config
    crawl4ai.CrawlerRunConfig = fake_run_config
    crawl4ai.CacheMode = SimpleNamespace(BYPASS="bypass")

    content_filter = ModuleType("crawl4ai.content_filter_strategy")
    content_filter.PruningContentFilter = lambda **kwargs: {"filter": kwargs}

    markdown_generation = ModuleType("crawl4ai.markdown_generation_strategy")
    markdown_generation.DefaultMarkdownGenerator = lambda **kwargs: {"generator": kwargs}

    monkeypatch.setitem(__import__("sys").modules, "crawl4ai", crawl4ai)
    monkeypatch.setitem(__import__("sys").modules, "crawl4ai.content_filter_strategy", content_filter)
    monkeypatch.setitem(__import__("sys").modules, "crawl4ai.markdown_generation_strategy", markdown_generation)
    return crawl4ai


class FakeAsyncWebCrawler:
    def __init__(self, result) -> None:
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def arun(self, *, url: str, config):
        return self._result


class FakeHttpxClient:
    def __init__(self, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def get(self, url: str, **kwargs):
        return FakeResponse()


class FailingHttpxClient(FakeHttpxClient):
    def get(self, url: str, **kwargs):
        raise crawl4ai_wrapper.httpx.HTTPError("not found")


class RecordingHttpxClient(FakeHttpxClient):
    requested_urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.requested_urls.append(url)
        return FakeResponse()


class HtmlHttpxClient(FakeHttpxClient):
    def get(self, url: str, **kwargs):
        return HtmlResponse()


class FakeResponse:
    headers = {"Content-Type": "image/png"}
    content = b"PNG!"

    def raise_for_status(self) -> None:
        return None


class HtmlResponse(FakeResponse):
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b"<!doctype html><html></html>"
