from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
import httpx

from app.core.security import redact_text

OPML_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
FEED_RESPONSE_MAX_BYTES = 20 * 1024 * 1024
OPML_IMPORT_ENTRY_MAX_COUNT = 5000
FEED_ENTRY_MAX_COUNT = 2000
CAPTURE_RESPONSE_MAX_BYTES = 50 * 1024 * 1024
PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES = 64 * 1024
PROFILE_CANDIDATE_JSON_MAX_BYTES = 1 * 1024 * 1024
DEFAULT_REDIRECT_LIMIT = 5
MULTIPART_BODY_OVERHEAD_MAX_BYTES = 1 * 1024 * 1024
PREVIEW_REDACTION_OVERLAP_BYTES = 512


class SafetyError(ValueError):
    pass


class XMLSafetyError(SafetyError):
    pass


class URLSafetyError(SafetyError):
    pass


def parse_xml_document(raw_xml: str | bytes, *, error_message: str) -> Any:
    try:
        return SafeElementTree.fromstring(raw_xml)
    except (SafeElementTree.ParseError, DefusedXmlException) as exc:
        raise XMLSafetyError(error_message) from exc


def validate_public_http_url(
    url: str,
    *,
    purpose: str = "remote fetch",
    resolve_dns: bool = False,
    allow_private_network: bool = False,
) -> str:
    normalized = normalize_remote_http_url(url, purpose=purpose, allow_localhost=allow_private_network)
    if not allow_private_network:
        _validate_direct_ip_host(normalized, purpose=purpose)
    if resolve_dns and not allow_private_network:
        _validate_url_resolves_to_public_ips(normalized, purpose=purpose)
    return normalized


def normalize_remote_http_url(url: str, *, purpose: str = "remote fetch", allow_localhost: bool = False) -> str:
    raw = url.strip()
    if not raw:
        raise URLSafetyError(f"{purpose} URL must not be empty")
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise URLSafetyError(f"{purpose} URL must be a valid public http or https URL")
    if parsed.username or parsed.password:
        raise URLSafetyError(f"{purpose} URL must not include embedded credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLSafetyError(f"{purpose} URL contains an invalid port") from exc
    if port and ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        port = None

    hostname = parsed.hostname.lower()
    if not allow_localhost and (hostname == "localhost" or hostname.endswith(".localhost")):
        raise URLSafetyError(f"{purpose} URL must not target localhost")
    host_netloc = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host_netloc if port is None else f"{host_netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit(SplitResult(scheme=scheme, netloc=netloc, path=path, query=parsed.query, fragment=""))


def safe_http_get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int,
    purpose: str,
    redirect_limit: int = DEFAULT_REDIRECT_LIMIT,
    allow_private_network: bool = False,
) -> httpx.Response:
    resolve_dns = not allow_private_network and not _client_uses_mock_transport(client)
    current_url = validate_public_http_url(
        url,
        purpose=purpose,
        resolve_dns=resolve_dns,
        allow_private_network=allow_private_network,
    )
    private_network_origin = _url_origin(current_url) if allow_private_network else None
    request_headers = headers

    for redirect_count in range(redirect_limit + 1):
        with client.stream("GET", current_url, headers=request_headers, follow_redirects=False) as response:
            response = _materialize_limited_response(response, max_bytes=max_bytes, purpose=purpose)

        if not _is_redirect_response(response):
            return response

        location = response.headers.get("Location")
        if not location:
            return response
        if redirect_count >= redirect_limit:
            raise URLSafetyError(f"{purpose} exceeded redirect limit")
        current_url = _validate_redirect_target_url(
            urljoin(str(response.url), location),
            purpose=purpose,
            resolve_dns=resolve_dns,
            allow_private_network=allow_private_network,
            private_network_origin=private_network_origin,
        )
        request_headers = None

    raise URLSafetyError(f"{purpose} exceeded redirect limit")


def validate_public_http_redirect_chain(
    client: httpx.Client,
    url: str,
    *,
    purpose: str,
    redirect_limit: int = DEFAULT_REDIRECT_LIMIT,
    allow_private_network: bool = False,
) -> str:
    resolve_dns = not allow_private_network and not _client_uses_mock_transport(client)
    current_url = validate_public_http_url(
        url,
        purpose=purpose,
        resolve_dns=resolve_dns,
        allow_private_network=allow_private_network,
    )
    private_network_origin = _url_origin(current_url) if allow_private_network else None
    for redirect_count in range(redirect_limit + 1):
        with client.stream("GET", current_url, follow_redirects=False) as response:
            status_code = response.status_code
            location = response.headers.get("Location")
            response_url = str(response.url)
        if status_code not in {301, 302, 303, 307, 308}:
            return response_url
        if not location:
            return response_url
        if redirect_count >= redirect_limit:
            raise URLSafetyError(f"{purpose} exceeded redirect limit")
        current_url = _validate_redirect_target_url(
            urljoin(response_url, location),
            purpose=purpose,
            resolve_dns=resolve_dns,
            allow_private_network=allow_private_network,
            private_network_origin=private_network_origin,
        )
    raise URLSafetyError(f"{purpose} exceeded redirect limit")


async def read_request_body_with_limit(request: Any, *, max_bytes: int, purpose: str) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            parsed_content_length = None
        if parsed_content_length is not None and parsed_content_length > max_bytes:
            raise SafetyError(f"{purpose} payload exceeds {max_bytes} byte limit")

    chunks: list[bytes] = []
    total_size = 0
    async for chunk in request.stream():
        total_size += len(chunk)
        if total_size > max_bytes:
            raise SafetyError(f"{purpose} payload exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def build_sanitized_body_preview(body: str | bytes | None, *, max_bytes: int = PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES) -> dict[str, Any]:
    if body is None:
        return {"preview": None, "original_byte_length": 0, "truncated": False}
    raw = body.encode("utf-8", errors="replace") if isinstance(body, str) else body
    preview_source = raw[: max_bytes + PREVIEW_REDACTION_OVERLAP_BYTES]
    redacted_text = redact_text(preview_source.decode("utf-8", errors="replace"))
    preview_text = redacted_text.encode("utf-8", errors="replace")[:max_bytes].decode("utf-8", errors="replace")
    return {
        "preview": preview_text,
        "original_byte_length": len(raw),
        "truncated": len(raw) > max_bytes,
        "truncation_reason": f"preview limited to {max_bytes} bytes" if len(raw) > max_bytes else None,
    }


def _validate_url_resolves_to_public_ips(url: str, *, purpose: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise URLSafetyError(f"{purpose} URL must include a host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    direct_ip = _parse_ip_address(host)
    if direct_ip is not None:
        _validate_public_ip(direct_ip, purpose=purpose)
        return

    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLSafetyError(f"{purpose} host could not be resolved") from exc

    if not records:
        raise URLSafetyError(f"{purpose} host could not be resolved")
    for record in records:
        address = record[4][0]
        ip_address = _parse_ip_address(address)
        if ip_address is None:
            raise URLSafetyError(f"{purpose} resolved to an invalid IP address")
        _validate_public_ip(ip_address, purpose=purpose)


def _client_uses_mock_transport(client: httpx.Client) -> bool:
    return isinstance(getattr(client, "_transport", None), httpx.MockTransport)


def _materialize_limited_response(response: httpx.Response, *, max_bytes: int, purpose: str) -> httpx.Response:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
        except ValueError:
            parsed_content_length = None
        if parsed_content_length is not None and parsed_content_length > max_bytes:
            raise URLSafetyError(f"{purpose} response exceeds {max_bytes} byte limit")

    chunks: list[bytes] = []
    total_size = 0
    for chunk in response.iter_bytes():
        total_size += len(chunk)
        if total_size > max_bytes:
            raise URLSafetyError(f"{purpose} response exceeds {max_bytes} byte limit")
        chunks.append(chunk)
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
        extensions=response.extensions,
    )


def _validate_direct_ip_host(url: str, *, purpose: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise URLSafetyError(f"{purpose} URL must include a host")
    direct_ip = _parse_ip_address(host)
    if direct_ip is not None:
        _validate_public_ip(direct_ip, purpose=purpose)


def _validate_redirect_target_url(
    url: str,
    *,
    purpose: str,
    resolve_dns: bool,
    allow_private_network: bool,
    private_network_origin: tuple[str, str, int | None] | None,
) -> str:
    if not allow_private_network:
        return validate_public_http_url(url, purpose=purpose, resolve_dns=resolve_dns)

    try:
        return validate_public_http_url(url, purpose=purpose, resolve_dns=resolve_dns)
    except URLSafetyError as public_error:
        try:
            normalized = validate_public_http_url(
                url,
                purpose=purpose,
                resolve_dns=False,
                allow_private_network=True,
            )
        except URLSafetyError:
            raise public_error from None
        if private_network_origin is not None and _url_origin(normalized) == private_network_origin:
            return normalized
        raise public_error from None


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return (parsed.scheme, parsed.hostname or "", parsed.port)


def _validate_public_ip(ip_address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, purpose: str) -> None:
    if (
        ip_address.is_private
        or ip_address.is_loopback
        or ip_address.is_link_local
        or ip_address.is_multicast
        or ip_address.is_unspecified
        or ip_address.is_reserved
    ):
        raise URLSafetyError(f"{purpose} URL resolved to a non-public IP address")


def _parse_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_redirect_response(response: httpx.Response) -> bool:
    return response.status_code in {301, 302, 303, 307, 308}
