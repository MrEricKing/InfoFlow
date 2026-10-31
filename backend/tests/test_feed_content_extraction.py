from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.db.models import FeedItem
from app.services import feed_content_extraction
from app.services.feed_content_extraction import (
    CONTENT_EXTRACTION_STATUS_FAILED,
    CONTENT_EXTRACTION_STATUS_NOT_NEEDED,
    CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION,
    CONTENT_EXTRACTION_STATUS_SKIPPED,
    CONTENT_TEXT_SOURCE_FEED_CONTENT,
    CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    FeedContentExtractionError,
    decide_feed_item_content_extraction,
    extract_feed_item_content,
    normalize_extracted_text_for_storage,
    read_content_extraction_diagnostics,
    validate_extracted_text_quality,
)


def test_content_extraction_http_client_requests_identity_encoding() -> None:
    with feed_content_extraction.create_http_client() as client:
        assert client.headers["Accept-Encoding"] == "identity"
        assert "Mozilla/5.0" in client.headers["User-Agent"]
        assert client.headers["Accept-Language"].startswith("zh-CN")


def test_short_jump_prompt_is_marked_for_extraction() -> None:
    item = build_feed_item(
        summary_text="Read more https://example.com/post/1",
        content_text="Read more https://example.com/post/1",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is True
    assert decision.status == CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION
    assert decision.signals["jump_prompt"] is True


def test_long_feed_content_is_not_extracted_automatically() -> None:
    long_text = "This is already a complete feed body. " * 20
    item = build_feed_item(
        summary_text="Short summary",
        content_text=long_text,
        content_text_source=CONTENT_TEXT_SOURCE_FEED_CONTENT,
    )

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is False
    assert decision.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED


def test_long_feed_content_with_jump_prompt_is_not_extracted_automatically() -> None:
    long_text = (
        "该渲染由 Shiro API 生成，最佳体验请前往原站查看：https://example.com/post/1 "
        + "这是一篇已经随 RSS 提供的完整正文，包含足够多的段落内容和阅读细节。"
    ) * 12
    item = build_feed_item(
        summary_text=long_text,
        content_text=long_text,
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is False
    assert decision.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert decision.signals["jump_prompt"] is True
    assert decision.signals["jump_prompt_with_short_text"] is False
    assert decision.signals["short_visible_text"] is False


def test_decision_prefers_preserved_feed_content_over_current_short_text() -> None:
    long_feed_content = "This full RSS body should be treated as the source baseline. " * 20
    item = build_feed_item(
        summary_text="Read more https://example.com/post/1",
        content_text="Read more https://example.com/post/1",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.feed_content_text = long_feed_content

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is False
    assert decision.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert decision.visible_without_urls_char_count > 200


def test_automatic_extraction_skips_known_media_pages() -> None:
    item = build_feed_item(
        summary_text='<iframe src="https://www.youtube-nocookie.com/embed/demo"></iframe>',
        content_text='<iframe src="https://www.youtube-nocookie.com/embed/demo"></iframe>',
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.canonical_url = "https://www.youtube.com/watch?v=demo"
    item.original_url = "https://www.youtube.com/watch?v=demo"

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is False
    assert decision.status == CONTENT_EXTRACTION_STATUS_SKIPPED
    assert decision.reason == "unsupported_media_page"


def test_short_form_feed_summary_is_treated_as_complete_body() -> None:
    item = build_feed_item(
        title="周五最快乐的就是想今晚出去吃什么好吃的",
        summary_text="周五最快乐的就是想今晚出去吃什么好吃的",
        content_text="周五最快乐的就是想今晚出去吃什么好吃的",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.canonical_url = "https://t.me/polebugisabug/374"
    item.original_url = "https://t.me/polebugisabug/374"

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is False
    assert decision.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert decision.reason == "short_form_feed_content"
    assert decision.signals["short_visible_text"] is True
    assert decision.signals["short_form_feed_content_host"] is True


def test_short_form_url_only_feed_summary_still_needs_extraction() -> None:
    item = build_feed_item(
        title="Tweet permalink",
        summary_text="https://x.com/CodeNightOwl/status/1904194677718609935",
        content_text="https://x.com/CodeNightOwl/status/1904194677718609935",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.canonical_url = "https://x.com/CodeNightOwl/status/1904194677718609935"
    item.original_url = "https://x.com/CodeNightOwl/status/1904194677718609935"

    decision = decide_feed_item_content_extraction(item)

    assert decision.should_extract is True
    assert decision.status == CONTENT_EXTRACTION_STATUS_NEEDS_EXTRACTION
    assert decision.reason == "short_or_jump_content"
    assert decision.signals["url_only"] is True
    assert decision.signals["short_form_feed_content_host"] is True


def test_short_form_feed_retry_clears_previous_failure_without_fetching() -> None:
    item = build_feed_item(
        title="nillion 强大，movement傻x。 质押还要解，但是现在要开香槟。",
        summary_text="nillion 强大，movement傻x。 质押还要解，但是现在要开香槟。",
        content_text="nillion 强大，movement傻x。 质押还要解，但是现在要开香槟。",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.canonical_url = "https://x.com/CodeNightOwl/status/1904194677718609935"
    item.original_url = "https://x.com/CodeNightOwl/status/1904194677718609935"
    item.content_extraction_status = CONTENT_EXTRACTION_STATUS_FAILED
    item.content_extraction_error = "fallback_failed: quality_failed: extracted text is too short"

    def unexpected_client_factory() -> httpx.Client:
        raise AssertionError("short-form feed content should not fetch the source page")

    result = extract_feed_item_content(
        item,
        record_debug=True,
        client_factory=unexpected_client_factory,
    )

    assert result.attempted is False
    assert result.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert item.content_extraction_status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert item.content_extraction_error is None
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["decision"]["reason"] == "short_form_feed_content"


def test_quality_gate_rejects_short_extracted_text() -> None:
    passed, reason = validate_extracted_text_quality(
        extracted_text="Too short",
        original_text="Read more",
        title="Example Story",
    )

    assert passed is False
    assert "too short" in reason


def test_quality_gate_accepts_short_title_related_page_body() -> None:
    passed, reason = validate_extracted_text_quality(
        extracted_text=(
            "Research KnifeCut: Refining Thin Part Segmentation with Cutting Lines "
            "21/12/2022 Zheng Lin"
        ),
        original_text="Read more",
        title="KnifeCut: Refining Thin Part Segmentation with Cutting Lines",
    )

    assert passed is True
    assert reason == "passed"


def test_quality_gate_accepts_very_short_cjk_photo_post_body() -> None:
    passed, reason = validate_extracted_text_quality(
        extracted_text="← 返回 秋日上海外滩随手拍 October 17, 2022 | 1 min read 苏州河畔 黄浦江远眺 宝格丽酒店远眺 分享",
        original_text="Read more",
        title="秋日上海外滩随手拍",
    )

    assert passed is True
    assert reason == "passed"


def test_quality_gate_still_rejects_short_unrelated_comment_form() -> None:
    passed, reason = validate_extracted_text_quality(
        extracted_text=(
            "Leave a Reply Cancel reply Your email address will not be published. "
            "Required fields are marked * Comment * Name * Email * Website"
        ),
        original_text="Read more",
        title="KnifeCut: Refining Thin Part Segmentation with Cutting Lines",
    )

    assert passed is False
    assert "too short" in reason


def test_quality_gate_allows_substantial_cjk_body_with_mixed_title_tokens() -> None:
    body = (
        "这篇发刊词介绍超牛链的内容方向、技术观察、人工智能应用和长期写作计划。"
        "正文没有重复英文问候词，但它是同一个页面里的完整中文文章段落。"
    ) * 30

    passed, reason = validate_extracted_text_quality(
        extracted_text=body,
        original_text="阅读全文",
        title="【发刊词】超牛链AI: hello world.",
    )

    assert passed is True
    assert reason == "passed"


def test_recent_failure_cooldown_does_not_block_sufficient_feed_content() -> None:
    long_text = "This feed body is already complete enough to read without source extraction. " * 10
    item = build_feed_item(
        summary_text=long_text,
        content_text=long_text,
        content_text_source=CONTENT_TEXT_SOURCE_FEED_CONTENT,
    )
    item.content_extraction_status = CONTENT_EXTRACTION_STATUS_FAILED
    item.content_extraction_error = "quality_failed: extracted text is too short"
    item.content_extraction_attempted_at = datetime.now(timezone.utc)

    def unexpected_client_factory() -> httpx.Client:
        raise AssertionError("sufficient feed content should not fetch during cooldown")

    result = extract_feed_item_content(item, client_factory=unexpected_client_factory)

    assert result.attempted is False
    assert result.status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert item.content_extraction_status == CONTENT_EXTRACTION_STATUS_NOT_NEEDED
    assert item.content_extraction_error is None


def test_storage_normalization_preserves_paragraph_breaks() -> None:
    normalized = normalize_extracted_text_for_storage(
        " First paragraph with   extra spacing.\r\n\r\n\r\nSecond paragraph.\nThird paragraph. "
    )

    assert normalized == "First paragraph with extra spacing.\n\nSecond paragraph.\nThird paragraph."


def test_fetch_html_does_not_follow_canonical_link_as_redirect() -> None:
    requested_urls: list[str] = []

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if str(request.url) == "https://example.com/notes/212":
                return httpx.Response(
                    200,
                    text='<html><head><link rel="canonical" href="/"></head><body>Article shell</body></html>',
                )
            return httpx.Response(200, text="<html><body>Site homepage</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    html, debug = feed_content_extraction._fetch_html("https://example.com/notes/212", client_factory)

    assert requested_urls == ["https://example.com/notes/212"]
    assert html == '<html><head><link rel="canonical" href="/"></head><body>Article shell</body></html>'
    assert debug["url"] == "https://example.com/notes/212"
    assert "html_redirects" not in debug


def test_fetch_html_retries_without_trailing_slash_after_404() -> None:
    requested_urls: list[str] = []

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if str(request.url) == "https://example.com/posts/intro/":
                return httpx.Response(404, text="<html><body>Not found</body></html>")
            return httpx.Response(200, text="<html><body>Article body</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    html, debug = feed_content_extraction._fetch_html("https://example.com/posts/intro/", client_factory)

    assert requested_urls == ["https://example.com/posts/intro/", "https://example.com/posts/intro"]
    assert html == "<html><body>Article body</body></html>"
    assert debug["url"] == "https://example.com/posts/intro"
    assert debug["url_fallbacks"] == [
        {
            "kind": "drop_trailing_slash_on_404",
            "from_url": "https://example.com/posts/intro/",
            "requested_url": "https://example.com/posts/intro",
            "to_url": "https://example.com/posts/intro",
            "previous_http_status": 404,
            "http_status": 200,
        }
    ]


def test_static_article_selector_accepts_common_entry_content_container() -> None:
    body_text = (
        "This article explains a reliable extraction path with enough concrete detail for the "
        "quality gate to accept the body. "
    ) * 5
    html = f"""
    <html>
      <body>
        <nav>Home About Links</nav>
        <div class="entry-content">
          <h1>Reliable extraction path</h1>
          <p>{body_text}</p>
        </div>
      </body>
    </html>
    """

    extracted = feed_content_extraction._extract_html_with_static_article_selector(
        html,
        "https://example.com/reliable-extraction-path",
    )

    assert "Reliable extraction path" in feed_content_extraction.normalize_visible_text(extracted)
    assert "Home About Links" not in feed_content_extraction.normalize_visible_text(extracted)


def test_extract_404_marks_dead_link_as_skipped() -> None:
    item = build_feed_item(
        title="Missing article",
        summary_text="Read more https://example.com/missing",
        content_text="Read more https://example.com/missing",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.original_url = "https://example.com/missing"
    item.canonical_url = "https://example.com/missing"

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="<html><body>Not found</body></html>", request=request)

        return httpx.Client(transport=httpx.MockTransport(handler))

    result = extract_feed_item_content(item, record_debug=True, client_factory=client_factory)

    assert result.attempted is True
    assert result.status == CONTENT_EXTRACTION_STATUS_SKIPPED
    assert result.error is None
    assert item.content_extraction_status == CONTENT_EXTRACTION_STATUS_SKIPPED
    assert item.content_extraction_error is None
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["terminal"] == {
        "status": CONTENT_EXTRACTION_STATUS_SKIPPED,
        "reason": "dead_link",
        "error": "fetch_failed: HTTP 404",
    }


def test_fetch_html_follows_late_meta_refresh_redirect() -> None:
    requested_urls: list[str] = []
    late_padding = "x" * 150_000

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if str(request.url) == "https://example.com/notes/213":
                return httpx.Response(
                    200,
                    text=(
                        f"<html><head></head><body>{late_padding}"
                        '<meta http-equiv="refresh" content="0;url=/notes/2026/ai-month-log">'
                        "</body></html>"
                    ),
                )
            return httpx.Response(200, text="<html><body>Final article body</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    html, debug = feed_content_extraction._fetch_html("https://example.com/notes/213", client_factory)

    assert requested_urls == ["https://example.com/notes/213", "https://example.com/notes/2026/ai-month-log"]
    assert html == "<html><body>Final article body</body></html>"
    assert debug["url"] == "https://example.com/notes/2026/ai-month-log"
    assert debug["html_redirects"] == [
        {
            "kind": "meta_refresh",
            "from_url": "https://example.com/notes/213",
            "to_url": "https://example.com/notes/2026/ai-month-log",
            "requested_url": "https://example.com/notes/2026/ai-month-log",
            "http_status": 200,
        }
    ]


def test_extraction_uses_html_redirect_target_for_extractor(monkeypatch) -> None:
    item = build_feed_item(
        title="Example Story",
        summary_text='<p>Read more <a href="https://example.com/notes/212">https://example.com/notes/212</a></p>',
        content_text='<p>Read more <a href="https://example.com/notes/212">https://example.com/notes/212</a></p>',
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    captured: dict[str, str] = {}

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://example.com/post/1":
                return httpx.Response(
                    200,
                    text=(
                        '<html><head><meta http-equiv="refresh" '
                        'content="1;url=https://example.com/posts/canonical"></head><body>Loading</body></html>'
                    ),
                )
            return httpx.Response(200, text="<html><body>Final page</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fake_extract(html: str, url: str) -> str:
        captured["html"] = html
        captured["url"] = url
        return f"<article><p>{'Example Story extracted article body with enough readable detail. ' * 8}</p></article>"

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fake_extract)

    result = extract_feed_item_content(item, force=True, client_factory=client_factory)

    assert result.succeeded is True
    assert captured["html"] == "<html><body>Final page</body></html>"
    assert captured["url"] == "https://example.com/posts/canonical"
    assert item.content_text_source == "extracted_page"
    assert item.content_extraction_error is None


def test_readability_extraction_writes_reader_html_paragraphs_and_absolute_urls() -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    article_text = " ".join(
        [
            "Example Story extracted article paragraph with enough readable detail for the quality gate.",
            "It keeps enough useful body text to replace a short RSS jump prompt.",
        ]
        * 8
    )

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=f"""
                <html>
                  <head><title>Example Story</title></head>
                  <body>
                    <nav>Navigation should not dominate the extracted article.</nav>
                    <article>
                      <h1>Example Story</h1>
                      <p>{article_text}</p>
                      <p>Second paragraph keeps structure for the Reader detail pane.</p>
                      <figure>
                        <img src="/images/story.jpg" alt="Story image" srcset="/images/story-2x.jpg 2x">
                      </figure>
                      <p><a href="../related/page.html">Related story</a></p>
                    </article>
                  </body>
                </html>
                """,
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert item.content_text_source == "extracted_page"
    assert item.content_text is not None
    assert "<p>" in item.content_text
    assert "Second paragraph keeps structure" in item.content_text
    assert 'src="https://example.com/images/story.jpg"' in item.content_text
    assert "srcset" not in item.content_text
    assert 'href="https://example.com/related/page.html"' in item.content_text
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["extractor"]["name"] == "readability-lxml"
    diagnostics = read_content_extraction_diagnostics(item)
    assert diagnostics.method == "readability-lxml"
    assert diagnostics.final_url == "https://example.com/post/1"
    assert diagnostics.visible_char_count is not None
    assert diagnostics.visible_char_count > 100


def test_readability_failure_falls_back_to_trafilatura(monkeypatch) -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>Example Story</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fail_readability(html: str, url: str) -> str:
        raise FeedContentExtractionError("readability boom")

    def fallback_extract(html: str, url: str) -> str:
        return (
            "Example Story fallback article body with enough readable detail for the quality gate. "
            "It explains the main idea and avoids looking like a jump prompt.\n\n"
            "Second fallback paragraph keeps a separate reading block with additional details. "
            "This paragraph is long enough to prove fallback content can still be saved."
        )

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fail_readability)
    monkeypatch.setattr(feed_content_extraction, "_extract_text_with_trafilatura", fallback_extract)

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert item.content_text_source == "extracted_page"
    assert item.content_text is not None
    assert "\n\nSecond fallback paragraph" in item.content_text
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["readability"]["error"] == "readability boom"
    assert item.content_extraction_debug_json["extractor"]["name"] == "trafilatura"
    diagnostics = read_content_extraction_diagnostics(item)
    assert diagnostics.method == "trafilatura"
    assert diagnostics.final_url == "https://example.com/post/1"
    assert diagnostics.visible_char_count is not None
    assert diagnostics.visible_char_count > 100


def test_static_article_selector_fallback_handles_vitepress_body(monkeypatch) -> None:
    item = build_feed_item(
        title="VitePress Story",
        summary_text="阅读全文 https://example.com/post/1",
        content_text="阅读全文 https://example.com/post/1",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    article_body = (
        "VitePress Story article body contains enough useful prose for the reader. "
        "It should not be replaced by sidebar navigation or the site homepage. "
    ) * 6

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=f"""
                <html>
                  <body>
                    <aside><a href="/a">Nav A</a><a href="/b">Nav B</a></aside>
                    <main class="VPDoc">
                      <div class="content">
                        <div class="vp-doc">
                          <h1>VitePress Story</h1>
                          <p>{article_body}</p>
                        </div>
                      </div>
                    </main>
                  </body>
                </html>
                """,
            )

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fail_readability(html: str, url: str) -> str:
        raise FeedContentExtractionError("readability selected navigation")

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fail_readability)

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert item.content_text is not None
    assert "VitePress Story article body" in item.content_text
    assert "Nav A" not in item.content_text
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["extractor"]["name"] == "static-article-selector"


def test_quality_gate_allows_link_heavy_article_with_prose() -> None:
    link_heavy_article = (
        "分享我的开源项目Thank-Mirror\n"
        "# 项目\n"
        "- name: Thanks-Mirror\n"
        "- link: https://github.com/example/Thanks-Mirror\n"
        "- source: https://example.com/source\n"
        "本项目灵感来自开源社区的镜像整理经验，这里分享的是包管理器直接可用，"
        "质量好，速度快的镜像，以及一些其他常用软件和系统镜像的国内镜像。\n"
        "在此，对那些提供公共仓库镜像的企业或组织，致以诚挚感谢。"
        "这些镜像可以帮助开发者在网络不稳定时继续完成日常安装、构建和测试工作。\n"
        + "\n".join(f"- mirror {index}: https://mirror{index}.example.com/path" for index in range(10))
    )

    passed, reason = validate_extracted_text_quality(
        extracted_text=link_heavy_article,
        original_text="阅读全文",
        title="分享我的开源项目Thank-Mirror",
    )

    assert passed is True
    assert reason == "passed"


def test_quality_gate_still_rejects_plain_link_collection() -> None:
    link_collection = "\n".join(
        f"- href=https://example.com/{index} source=nav url=https://example.com/{index}/next" for index in range(12)
    )

    passed, reason = validate_extracted_text_quality(
        extracted_text=link_collection,
        original_text="Read more",
        title="Example Story",
    )

    assert passed is False
    assert "navigation or link collection" in reason


def test_content_extraction_diagnostics_return_nulls_when_debug_payload_is_missing() -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    item.content_extraction_debug_json = None

    diagnostics = read_content_extraction_diagnostics(item)

    assert diagnostics.method is None
    assert diagnostics.final_url is None
    assert diagnostics.visible_char_count is None


def test_too_short_extraction_retries_once_and_records_attempts(monkeypatch) -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    requested_urls: list[str] = []
    readability_calls = 0

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(200, text="<html><body>Example Story</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def flaky_readability(html: str, url: str) -> str:
        nonlocal readability_calls
        readability_calls += 1
        if readability_calls == 1:
            raise FeedContentExtractionError("quality_failed: extracted text is too short")
        return (
            "Example Story retried article body with enough readable detail for the quality gate. "
            "The second fetch should be used after the short first extraction.\n\n"
            "Second retried paragraph keeps enough additional information for storage."
        )

    def fail_fallback(html: str, url: str) -> str:
        raise FeedContentExtractionError("quality_failed: extracted text is too short")

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", flaky_readability)
    monkeypatch.setattr(feed_content_extraction, "_extract_text_with_trafilatura", fail_fallback)

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert requested_urls == ["https://example.com/post/1", "https://example.com/post/1"]
    assert readability_calls == 2
    assert item.content_extraction_debug_json is not None
    assert "quality_failed: extracted text is too short" in item.content_extraction_debug_json["retry"]["reason"]
    first_attempt_error = item.content_extraction_debug_json["previous_attempts"][0]["error"]
    assert "quality_failed: extracted text is too short" in first_attempt_error
    assert item.content_extraction_debug_json["extractor"]["name"] == "readability-lxml"


def test_transient_fetch_failure_retries_once(monkeypatch) -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )
    requested_urls: list[str] = []

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            if len(requested_urls) == 1:
                return httpx.Response(500, text="<html><body>temporary failure</body></html>")
            return httpx.Response(200, text="<html><body>Example Story</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fake_readability(html: str, url: str) -> str:
        return (
            "Example Story article body recovered after a transient fetch failure. "
            "It has enough useful detail for the retry path to pass quality checks.\n\n"
            "The second paragraph confirms the successful response was used."
        )

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fake_readability)

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert requested_urls == ["https://example.com/post/1", "https://example.com/post/1"]
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["retry"]["reason"] == "fetch_failed: HTTP 500"
    assert item.content_extraction_debug_json["extractor"]["name"] == "readability-lxml"


def test_forced_reextraction_quality_compares_against_original_feed_text(monkeypatch) -> None:
    item = build_feed_item(
        summary_text="前往原站查看：https://example.com/post/1",
        content_text="Existing extracted page body. " * 40,
        content_text_source="extracted_page",
    )
    item.feed_content_text = "前往原站查看：https://example.com/post/1"
    item.content_extraction_status = "succeeded"

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>Example Story</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fail_readability(html: str, url: str) -> str:
        raise FeedContentExtractionError("readability too short")

    def fallback_extract(html: str, url: str) -> str:
        return (
            "Example Story re-extracted article body with enough readable detail for the original RSS jump prompt. "
            "It is intentionally shorter than the previous extracted body but still a valid page body.\n\n"
            "Second re-extracted paragraph keeps useful structure and should pass because the quality baseline is the "
            "preserved feed_content_text, not the existing extracted content."
        )

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fail_readability)
    monkeypatch.setattr(feed_content_extraction, "_extract_text_with_trafilatura", fallback_extract)

    result = extract_feed_item_content(item, force=True, record_debug=True, client_factory=client_factory)

    assert result.succeeded is True
    assert item.content_text is not None
    assert "Second re-extracted paragraph" in item.content_text
    assert item.content_extraction_status == "succeeded"
    assert item.content_extraction_error is None
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["quality"]["passed"] is True
    assert item.content_extraction_debug_json["extractor"]["name"] == "trafilatura"


def test_manual_debug_failure_does_not_overwrite_feed_content() -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    def failing_client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        return httpx.Client(transport=httpx.MockTransport(handler))

    result = extract_feed_item_content(
        item,
        force=True,
        record_debug=True,
        client_factory=failing_client_factory,
    )

    assert result.attempted is True
    assert result.succeeded is False
    assert item.content_text == "Read more"
    assert item.content_extraction_status == CONTENT_EXTRACTION_STATUS_FAILED
    assert item.content_extraction_error is not None
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["trigger"] == "manual"
    assert item.content_extraction_debug_json["decision"]["reason"] == "manual_force"


def test_extractor_failures_do_not_overwrite_feed_content(monkeypatch) -> None:
    item = build_feed_item(
        summary_text="Read more",
        content_text="Read more",
        content_text_source=CONTENT_TEXT_SOURCE_FEED_SUMMARY,
    )

    def client_factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html><body>Example Story</body></html>")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def fail_readability(html: str, url: str) -> str:
        raise FeedContentExtractionError("readability rejected")

    def fail_fallback(html: str, url: str) -> str:
        raise FeedContentExtractionError("trafilatura rejected")

    monkeypatch.setattr(feed_content_extraction, "_extract_html_with_readability", fail_readability)
    monkeypatch.setattr(feed_content_extraction, "_extract_text_with_trafilatura", fail_fallback)

    result = extract_feed_item_content(
        item,
        force=True,
        record_debug=True,
        client_factory=client_factory,
    )

    assert result.attempted is True
    assert result.succeeded is False
    assert item.content_text == "Read more"
    assert item.content_extraction_status == CONTENT_EXTRACTION_STATUS_FAILED
    assert item.content_extraction_error is not None
    assert "readability_failed: readability rejected" in item.content_extraction_error
    assert "fallback_failed: trafilatura rejected" in item.content_extraction_error
    assert item.content_extraction_debug_json is not None
    assert item.content_extraction_debug_json["readability"]["error"] == "readability rejected"
    assert item.content_extraction_debug_json["fallback"]["error"] == "trafilatura rejected"


def build_feed_item(
    *,
    title: str = "Example Story",
    summary_text: str,
    content_text: str,
    content_text_source: str,
) -> FeedItem:
    return FeedItem(
        original_url="https://example.com/post/1",
        canonical_url="https://example.com/post/1",
        title=title,
        author_name=None,
        published_at=None,
        summary_text=summary_text,
        feed_content_text=content_text,
        content_text=content_text,
        content_text_source=content_text_source,
        content_extraction_status="not_checked",
        topic_hints=None,
    )
