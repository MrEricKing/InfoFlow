from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from app.services.source_resolver import SourceValidationError, normalize_http_url

SourceIconSource = Literal[
    "rss_channel_image",
    "atom_icon",
    "atom_logo",
    "opml_site_favicon",
    "source_site_favicon",
    "fallback_avatar",
]

ICON_SOURCE_PRIORITY: dict[str, int] = {
    "rss_channel_image": 1,
    "atom_icon": 2,
    "atom_logo": 3,
    "opml_site_favicon": 4,
    "source_site_favicon": 5,
    "fallback_avatar": 6,
}


@dataclass(slots=True)
class ResolvedSourceIcon:
    icon_url: str | None
    icon_source: SourceIconSource


def normalize_icon_url(value: str | None, *, base_url: str | None = None) -> str | None:
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    if base_url:
        raw = urljoin(base_url, raw)

    try:
        normalized = normalize_http_url(raw)
    except SourceValidationError:
        return None

    if normalized is None:
        return None

    parsed = urlsplit(normalized)
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=parsed.netloc,
            path=parsed.path,
            query=parsed.query,
            fragment="",
        )
    )


def resolve_feed_icon(
    value: str | None,
    icon_source: SourceIconSource,
    *,
    base_url: str | None = None,
) -> ResolvedSourceIcon | None:
    if icon_source == "fallback_avatar":
        return fallback_icon()

    icon_url = normalize_icon_url(value, base_url=base_url)
    if icon_url is None:
        return None
    return ResolvedSourceIcon(icon_url=icon_url, icon_source=icon_source)


def resolve_supplied_icon(
    icon_url: str | None,
    icon_source: str | None,
    *,
    base_url: str | None = None,
) -> ResolvedSourceIcon | None:
    if icon_source is None:
        return None
    if icon_source not in ICON_SOURCE_PRIORITY:
        raise SourceValidationError("icon_source is not supported")
    if icon_source == "fallback_avatar":
        return fallback_icon()

    normalized_icon_url = normalize_icon_url(icon_url, base_url=base_url)
    if normalized_icon_url is None:
        raise SourceValidationError("icon_url must be a valid http or https URL for the selected icon_source")
    return ResolvedSourceIcon(
        icon_url=normalized_icon_url,
        icon_source=icon_source,  # type: ignore[arg-type]
    )


def resolve_opml_site_icon(site_url: str | None, source_url: str | None) -> ResolvedSourceIcon:
    return resolve_site_favicon(site_url, source_url, icon_source="opml_site_favicon")


def resolve_source_site_icon(site_url: str | None, source_url: str | None) -> ResolvedSourceIcon:
    return resolve_site_favicon(site_url, source_url, icon_source="source_site_favicon")


def resolve_site_favicon(
    site_url: str | None,
    source_url: str | None,
    *,
    icon_source: SourceIconSource,
) -> ResolvedSourceIcon:
    for candidate_url in (site_url, source_url):
        favicon_url = build_favicon_url(candidate_url)
        if favicon_url is not None:
            return ResolvedSourceIcon(icon_url=favicon_url, icon_source=icon_source)
    return fallback_icon()


def build_favicon_url(value: str | None) -> str | None:
    normalized = normalize_icon_url(value)
    if normalized is None:
        return None

    parsed = urlsplit(normalized)
    return urlunsplit(
        SplitResult(
            scheme=parsed.scheme,
            netloc=parsed.netloc,
            path="/favicon.ico",
            query="",
            fragment="",
        )
    )


def fallback_icon() -> ResolvedSourceIcon:
    return ResolvedSourceIcon(icon_url=None, icon_source="fallback_avatar")


def choose_preview_icon(
    feed_icon_url: str | None,
    feed_icon_source: str | None,
    *,
    site_url: str | None,
    source_url: str | None,
) -> ResolvedSourceIcon:
    feed_icon = resolve_supplied_icon(feed_icon_url, feed_icon_source, base_url=site_url or source_url)
    if feed_icon is not None:
        return feed_icon
    return resolve_source_site_icon(site_url, source_url)


def apply_source_icon(
    source: object,
    candidate: ResolvedSourceIcon,
    *,
    only_if_missing: bool = False,
    upgrade_only: bool = True,
) -> bool:
    current_icon_url = getattr(source, "icon_url", None)
    current_icon_source = getattr(source, "icon_source", None)

    if only_if_missing and current_icon_source is not None:
        return False
    if upgrade_only and not should_replace_icon(current_icon_url, current_icon_source, candidate):
        return False

    if current_icon_url == candidate.icon_url and current_icon_source == candidate.icon_source:
        return False

    setattr(source, "icon_url", candidate.icon_url)
    setattr(source, "icon_source", candidate.icon_source)
    setattr(source, "icon_updated_at", now_utc())
    return True


def should_replace_icon(
    current_icon_url: str | None,
    current_icon_source: str | None,
    candidate: ResolvedSourceIcon,
) -> bool:
    if current_icon_source is None:
        return True

    current_priority = ICON_SOURCE_PRIORITY.get(current_icon_source, len(ICON_SOURCE_PRIORITY) + 1)
    candidate_priority = ICON_SOURCE_PRIORITY[candidate.icon_source]
    if candidate_priority < current_priority:
        return True
    if candidate_priority > current_priority:
        return False

    return candidate.icon_url != current_icon_url


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
