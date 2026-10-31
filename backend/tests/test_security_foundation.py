from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.core.config import get_settings, reset_settings_cache
from app.core.safety import (
    FEED_ENTRY_MAX_COUNT,
    FEED_RESPONSE_MAX_BYTES,
    OPML_IMPORT_ENTRY_MAX_COUNT,
    OPML_UPLOAD_MAX_BYTES,
    PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES,
    URLSafetyError,
    build_sanitized_body_preview,
    safe_http_get,
    validate_public_http_redirect_chain,
    validate_public_http_url,
)
from app.core.security import (
    PayloadValidationError,
    SecretConfigurationError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_value,
    validate_encryption_key,
    validate_operation_payload,
)
from app.db.models import AppSetting
from app.db.session import get_session_factory
from app.services.app_settings import (
    AI_PROFILE_GENERAL,
    INFOFLOW_AI_API_KEY_SETTING_ATTRS,
    resolve_ai_provider_config,
    validate_ai_secret_settings_for_startup,
)
from app.services.source_registry import SourceValidationError, decode_opml_payload, parse_opml
from app.services.source_sync import SourceSyncError, parse_feed_document


def test_secret_encryption_roundtrip_and_wrong_key_failure() -> None:
    key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()

    encrypted = encrypt_secret("sk-test-secret", key)

    assert is_encrypted_value(encrypted)
    assert encrypted != "sk-test-secret"
    assert decrypt_secret(encrypted, key) == "sk-test-secret"
    with pytest.raises(SecretConfigurationError, match="malformed"):
        validate_encryption_key("not-a-valid-fernet-key")
    with pytest.raises(SecretConfigurationError, match="cannot decrypt"):
        decrypt_secret(encrypted, wrong_key)


def test_operation_payload_validation_rejects_secret_large_content_and_oversize() -> None:
    with pytest.raises(PayloadValidationError, match="secret-like"):
        validate_operation_payload({"api_key": "sk-secret"})
    with pytest.raises(PayloadValidationError, match="large-content"):
        validate_operation_payload({"raw_html": "<html></html>"})
    with pytest.raises(PayloadValidationError, match="16KB"):
        validate_operation_payload({"ids": list(range(10_000))})

    assert validate_operation_payload({"saved_item_id": 1, "mode": "build_missing"}) == {
        "saved_item_id": 1,
        "mode": "build_missing",
    }


def test_safe_xml_rejects_opml_and_feed_xxe_payloads() -> None:
    xxe = """<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<opml version="2.0"><body><outline text="Bad" xmlUrl="&xxe;" /></body></opml>
"""
    with pytest.raises(SourceValidationError, match="not valid XML"):
        parse_opml(xxe)

    feed_xxe = """<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<rss version="2.0"><channel><title>&xxe;</title></channel></rss>
"""
    with pytest.raises(SourceSyncError, match="not valid XML"):
        parse_feed_document(feed_xxe)


def test_size_caps_reject_opml_feed_and_preview_over_limits() -> None:
    with pytest.raises(SourceValidationError, match="10MB"):
        decode_opml_payload(b"x" * (OPML_UPLOAD_MAX_BYTES + 1))

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Length": str(FEED_RESPONSE_MAX_BYTES + 1)},
                text="too large",
                request=request,
            )
        )
    )
    with client:
        with pytest.raises(URLSafetyError, match="byte limit"):
            safe_http_get(client, "https://feeds.example.com/rss.xml", max_bytes=FEED_RESPONSE_MAX_BYTES, purpose="Feed")

    preview = build_sanitized_body_preview(
        f"Authorization: Bearer sk-secret-token\napi_key=sk-secret-token\n{'x' * (PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES + 5)}",
        max_bytes=PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES,
    )
    assert preview["truncated"] is True
    assert "sk-secret-token" not in preview["preview"]
    assert "Authorization: Bearer [REDACTED]" in preview["preview"]
    assert "api_key=[REDACTED]" in preview["preview"]


def test_opml_entry_count_limit_allows_boundary_and_rejects_over_limit() -> None:
    entries = parse_opml(_build_opml_with_entry_count(OPML_IMPORT_ENTRY_MAX_COUNT))
    assert len(entries) == OPML_IMPORT_ENTRY_MAX_COUNT

    with pytest.raises(SourceValidationError, match=f"{OPML_IMPORT_ENTRY_MAX_COUNT} entry limit"):
        parse_opml(_build_opml_with_entry_count(OPML_IMPORT_ENTRY_MAX_COUNT + 1))


def test_feed_entry_count_limit_allows_boundary_and_rejects_over_limit() -> None:
    document = parse_feed_document(_build_rss_with_item_count(FEED_ENTRY_MAX_COUNT))
    assert len(document.entries) == FEED_ENTRY_MAX_COUNT

    with pytest.raises(SourceSyncError, match=f"{FEED_ENTRY_MAX_COUNT} entry limit"):
        parse_feed_document(_build_rss_with_item_count(FEED_ENTRY_MAX_COUNT + 1))


def test_sanitized_preview_redacts_secret_that_crosses_truncation_boundary() -> None:
    preview = build_sanitized_body_preview(
        f"{'x' * 30} sk-{'a' * 700}",
        max_bytes=35,
    )

    assert preview["truncated"] is True
    assert "sk-" not in preview["preview"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/feed.xml",
        "https://user:pass@example.com/feed.xml",
        "http://localhost/feed.xml",
        "http://10.0.0.1/feed.xml",
        "http://127.0.0.1/feed.xml",
        "http://198.18.0.136/feed.xml",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1/feed.xml",
        "http://0.0.0.0/feed.xml",
    ],
)
def test_ssrf_guard_rejects_unsafe_urls(url: str) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok", request=request))) as client:
        with pytest.raises(URLSafetyError):
            safe_http_get(client, url, max_bytes=1024, purpose="test")


def test_url_validation_is_proxy_aware_for_domain_dns(monkeypatch) -> None:
    def fail_dns_validation(*args, **kwargs) -> None:
        raise AssertionError("DNS public-IP validation should not run in proxy-aware mode")

    monkeypatch.setattr("app.core.safety._validate_url_resolves_to_public_ips", fail_dns_validation)

    assert validate_public_http_url("https://feeds.example.com/rss.xml", purpose="Feed") == "https://feeds.example.com/rss.xml"


def test_url_validation_rejects_public_hostname_that_resolves_to_private_ip(monkeypatch) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "feeds.example.com"
        return [(None, None, None, None, ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(URLSafetyError, match="non-public"):
        validate_public_http_url("https://feeds.example.com/rss.xml", purpose="test", resolve_dns=True)


def test_safe_http_get_skips_dns_validation_for_mock_transport(monkeypatch) -> None:
    def fake_getaddrinfo(*args, **kwargs):
        raise AssertionError("MockTransport-backed tests should not run DNS public-IP validation")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok", request=request))) as client:
        response = safe_http_get(client, "https://feeds.example.com/rss.xml", max_bytes=1024, purpose="test")

    assert response.status_code == 200


def test_safe_http_get_private_network_exception_skips_dns_rebinding_guard(monkeypatch) -> None:
    def fake_getaddrinfo(*args, **kwargs):
        raise AssertionError("private-network exception should not run DNS public-IP validation")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok", request=request))) as client:
        response = safe_http_get(
            client,
            "http://rsshub:1200/feed.xml",
            max_bytes=1024,
            purpose="RSSHub",
            allow_private_network=True,
        )

    assert response.status_code == 200


def test_ssrf_guard_revalidates_redirect_targets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://public.example.com/feed.xml":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(URLSafetyError, match="non-public"):
            safe_http_get(client, "https://public.example.com/feed.xml", max_bytes=1024, purpose="test")


def test_private_network_exception_keeps_redirects_on_same_origin() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://rsshub:1200/route":
            return httpx.Response(302, headers={"Location": "/feed.xml"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = safe_http_get(
            client,
            "http://rsshub:1200/route",
            max_bytes=1024,
            purpose="RSSHub fetch",
            allow_private_network=True,
        )

    assert response.status_code == 200
    assert str(response.url) == "http://rsshub:1200/feed.xml"


def test_private_network_exception_rejects_redirect_to_different_private_origin() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://rsshub:1200/route":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"}, request=request)
        return httpx.Response(200, text="metadata", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(URLSafetyError, match="non-public"):
            safe_http_get(
                client,
                "http://rsshub:1200/route",
                max_bytes=1024,
                purpose="RSSHub fetch",
                allow_private_network=True,
            )


def test_redirect_preflight_revalidates_targets_without_reading_body() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if str(request.url) == "https://public.example.com/article":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, request=request)
        return httpx.Response(200, content=b"x" * 1024, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(URLSafetyError, match="non-public"):
            validate_public_http_redirect_chain(client, "https://public.example.com/article", purpose="Capture")

    assert requested_urls == ["https://public.example.com/article"]


def test_plaintext_ai_profile_key_encrypts_when_encryption_key_available(client) -> None:
    setting_key = INFOFLOW_AI_API_KEY_SETTING_ATTRS[AI_PROFILE_GENERAL]
    with get_session_factory()() as session:
        session.add(AppSetting(key=setting_key, value="sk-profile-plaintext"))
        session.commit()

    with get_session_factory()() as session:
        validate_ai_secret_settings_for_startup(session)
        session.commit()

    with get_session_factory()() as session:
        stored = session.scalar(select(AppSetting.value).where(AppSetting.key == setting_key))
        config = resolve_ai_provider_config(session, profile=AI_PROFILE_GENERAL)

    assert is_encrypted_value(stored)
    assert stored != "sk-profile-plaintext"
    assert config.api_key == "sk-profile-plaintext"


def test_encrypted_ai_key_requires_configured_decryption_key(client, monkeypatch) -> None:
    setting_key = INFOFLOW_AI_API_KEY_SETTING_ATTRS[AI_PROFILE_GENERAL]
    current_key = get_settings().settings_encryption_key
    assert current_key
    with get_session_factory()() as session:
        session.add(AppSetting(key=setting_key, value=encrypt_secret("sk-secret", current_key)))
        session.commit()

    monkeypatch.delenv("INFOFLOW_SETTINGS_ENCRYPTION_KEY", raising=False)
    reset_settings_cache()
    try:
        with get_session_factory()() as session:
            with pytest.raises(SecretConfigurationError, match="required"):
                validate_ai_secret_settings_for_startup(session)
    finally:
        reset_settings_cache()


def _build_opml_with_entry_count(count: int) -> str:
    outlines = "\n".join(
        f'      <outline text="Feed {index}" xmlUrl="https://example.com/feed-{index}.xml" />'
        for index in range(count)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <body>
{outlines}
  </body>
</opml>
"""


def _build_rss_with_item_count(count: int) -> str:
    items = "\n".join(
        f"""    <item>
      <guid>https://example.com/posts/{index}</guid>
      <title>Post {index}</title>
      <link>https://example.com/posts/{index}</link>
    </item>"""
        for index in range(count)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Large Feed</title>
{items}
  </channel>
</rss>
"""
