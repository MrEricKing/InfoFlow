from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal
from urllib.parse import SplitResult, unquote, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AppSetting

SourceKindValue = Literal["rss", "rsshub"]

SETTING_RSSHUB_BASE_URL = "rsshub_base_url"
DEFAULT_RSSHUB_BASE_URL = "http://localhost:1200"
KNOWN_RSSHUB_BASE_URLS = (
    DEFAULT_RSSHUB_BASE_URL,
    "http://rsshub:1200",
    "https://rsshub.app",
)


class SourceValidationError(ValueError):
    pass


@dataclass(slots=True)
class EffectiveSetting:
    key: str
    value: str
    source: str


@dataclass(slots=True)
class SourceUrlResolution:
    detected_kind: SourceKindValue
    input_url: str
    canonical_url: str
    resolved_url: str
    rsshub_base_url: str | None = None
    rsshub_route: str | None = None


def normalize_http_url(value: str | None) -> str | None:
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise SourceValidationError("URL must be a valid http or https URL")

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceValidationError("URL contains an invalid port") from exc
    if port and ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        port = None

    netloc = hostname if port is None else f"{hostname}:{port}"
    if parsed.username:
        credentials = parsed.username
        if parsed.password:
            credentials = f"{credentials}:{parsed.password}"
        netloc = f"{credentials}@{netloc}"

    path = parsed.path or "/"

    return urlunsplit(
        SplitResult(
            scheme=scheme,
            netloc=netloc,
            path=path,
            query=parsed.query,
            fragment="",
        )
    )


def normalize_rsshub_base_url(value: str | None) -> str:
    normalized = normalize_http_url(value)
    if normalized is None:
        raise SourceValidationError("rsshub_base_url must not be empty")

    parsed = urlsplit(normalized)
    if parsed.username or parsed.password:
        raise SourceValidationError("rsshub_base_url must not include credentials")
    if parsed.query or parsed.fragment:
        raise SourceValidationError("rsshub_base_url must not include query or fragment")

    decoded_path = unquote(parsed.path or "", errors="replace")
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise SourceValidationError("rsshub_base_url path must not contain traversal segments")

    path = (parsed.path or "").rstrip("/")
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=parsed.netloc,
            path=path,
            query="",
            fragment="",
        )
    )


def get_effective_rsshub_base_url(session: Session) -> EffectiveSetting:
    setting = session.scalar(select(AppSetting).where(AppSetting.key == SETTING_RSSHUB_BASE_URL).limit(1))
    if setting is not None:
        return EffectiveSetting(
            key=SETTING_RSSHUB_BASE_URL,
            value=normalize_rsshub_base_url(setting.value),
            source="db",
        )

    configured_value = get_settings().rsshub_base_url
    normalized = normalize_rsshub_base_url(configured_value)
    source = "env" if "RSSHUB_BASE_URL" in os.environ or normalized != DEFAULT_RSSHUB_BASE_URL else "default"
    return EffectiveSetting(key=SETTING_RSSHUB_BASE_URL, value=normalized, source=source)


def get_known_rsshub_base_urls(session: Session) -> list[str]:
    known: list[str] = []
    for value in (get_effective_rsshub_base_url(session).value, *KNOWN_RSSHUB_BASE_URLS):
        normalized = normalize_rsshub_base_url(value)
        if normalized not in known:
            known.append(normalized)
    return known


def detect_source_url(value: str, session: Session) -> SourceUrlResolution:
    raw = value.strip()
    if not raw:
        raise SourceValidationError("Source URL must not be empty")

    if raw.lower().startswith("rsshub://"):
        canonical_url = normalize_rsshub_source_url(raw)
        return _resolve_rsshub_canonical_url(canonical_url, session, input_url=raw)

    normalized_http_url = normalize_http_url(raw)
    if normalized_http_url is None:
        raise SourceValidationError("Source URL must not be empty")

    rsshub_url = try_convert_known_rsshub_instance_url(normalized_http_url, session)
    if rsshub_url is not None:
        return _resolve_rsshub_canonical_url(rsshub_url, session, input_url=raw)

    return SourceUrlResolution(
        detected_kind="rss",
        input_url=raw,
        canonical_url=normalized_http_url,
        resolved_url=normalized_http_url,
    )


def canonicalize_source_url(kind: SourceKindValue, value: str, session: Session) -> str:
    raw = value.strip()
    if not raw:
        raise SourceValidationError("Source URL must not be empty")

    if kind == "rss":
        if raw.lower().startswith("rsshub://"):
            raise SourceValidationError("RSS source URL must be a valid http or https URL")
        normalized = normalize_http_url(raw)
        if normalized is None:
            raise SourceValidationError("Source URL must not be empty")
        return normalized

    if kind != "rsshub":
        raise SourceValidationError("Source kind must be rss or rsshub")

    if raw.lower().startswith("rsshub://"):
        return normalize_rsshub_source_url(raw)

    normalized_http_url = normalize_http_url(raw)
    if normalized_http_url is None:
        raise SourceValidationError("Source URL must not be empty")

    rsshub_url = try_convert_known_rsshub_instance_url(normalized_http_url, session)
    if rsshub_url is None:
        raise SourceValidationError("RSSHub source URL must be rsshub://route/path or a known RSSHub instance URL")
    return rsshub_url


def resolve_source_fetch_url(kind: SourceKindValue, canonical_url: str, session: Session) -> SourceUrlResolution:
    canonical = canonicalize_source_url(kind, canonical_url, session)
    if kind == "rss":
        return SourceUrlResolution(
            detected_kind="rss",
            input_url=canonical_url,
            canonical_url=canonical,
            resolved_url=canonical,
        )

    return _resolve_rsshub_canonical_url(canonical, session, input_url=canonical_url)


def normalize_rsshub_source_url(value: str) -> str:
    raw = value.strip()
    if not raw.lower().startswith("rsshub://"):
        raise SourceValidationError("RSSHub source URL must start with rsshub://")

    route_part = raw[len("rsshub://") :]
    if route_part.startswith("/"):
        raise SourceValidationError("RSSHub route must be relative and must not start with a slash")
    route = route_part
    validate_rsshub_route(route)
    return f"rsshub://{route}"


def try_convert_known_rsshub_instance_url(value: str, session: Session) -> str | None:
    parsed_value = urlsplit(value)
    for base_url in get_known_rsshub_base_urls(session):
        parsed_base = urlsplit(base_url)
        if parsed_value.scheme != parsed_base.scheme or parsed_value.netloc.lower() != parsed_base.netloc.lower():
            continue

        base_path = parsed_base.path.rstrip("/")
        if base_path:
            if parsed_value.path == base_path:
                route = ""
            elif parsed_value.path.startswith(f"{base_path}/"):
                route = parsed_value.path[len(base_path) + 1 :]
            else:
                continue
        else:
            route = parsed_value.path.lstrip("/")

        if parsed_value.query:
            route = f"{route}?{parsed_value.query}"
        if not route:
            continue

        validate_rsshub_route(route)
        return f"rsshub://{route}"

    return None


def validate_rsshub_route(route: str) -> None:
    if not route:
        raise SourceValidationError("RSSHub route must not be empty")
    if route.startswith("/") or route.startswith("//"):
        raise SourceValidationError("RSSHub route must be relative to the RSSHub base URL")
    if "#" in route:
        raise SourceValidationError("RSSHub route must not include URL fragments")
    if "\\" in route:
        raise SourceValidationError("RSSHub route must not contain backslashes")
    if any(ord(character) < 32 for character in route):
        raise SourceValidationError("RSSHub route must not contain control characters")
    if "://" in route:
        raise SourceValidationError("RSSHub route must not embed a full external URL")

    decoded_route = unquote(route, errors="replace")
    if decoded_route.startswith("/") or decoded_route.startswith("//"):
        raise SourceValidationError("RSSHub route must not resolve to an absolute or protocol-relative URL")
    if "://" in decoded_route:
        raise SourceValidationError("RSSHub route must not embed a full external URL")
    if "\\" in decoded_route:
        raise SourceValidationError("RSSHub route must not contain backslashes")

    decoded_path = decoded_route.split("?", 1)[0]
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise SourceValidationError("RSSHub route must not contain path traversal segments")


def _resolve_rsshub_canonical_url(canonical_url: str, session: Session, *, input_url: str) -> SourceUrlResolution:
    route = canonical_url.removeprefix("rsshub://")
    validate_rsshub_route(route)
    base_url = get_effective_rsshub_base_url(session).value
    resolved_url = normalize_http_url(f"{base_url.rstrip('/')}/{route}")
    if resolved_url is None:
        raise SourceValidationError("Resolved RSSHub URL is invalid")

    return SourceUrlResolution(
        detected_kind="rsshub",
        input_url=input_url,
        canonical_url=canonical_url,
        resolved_url=resolved_url,
        rsshub_base_url=base_url,
        rsshub_route=route,
    )
