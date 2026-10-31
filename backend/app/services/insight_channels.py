from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import InsightChannel, InsightChannelCatalogState, Source, SourceChannelTendency
from app.schemas.settings import InsightChannelCreateRequest, InsightChannelRead, InsightChannelUpdateRequest
from app.schemas.source import (
    SourceChannelTendencyRead,
    SourceInsightRoutingRead,
    SourceInsightRoutingUpdateRequest,
)


NEEDS_REVIEW_CHANNEL_ID = "needs_review"
CHANNEL_CATALOG_STATE_ID = 1
CHANNEL_ID_PATTERN = re.compile(r"^[a-z0-9_]{2,128}$")
CHANNEL_SEMANTIC_FIELDS = (
    "label",
    "description",
    "focus_prompt",
    "boundary_prompt",
    "reading_depth",
    "default_flow_policy",
    "sort_order",
    "enabled",
)

DEFAULT_INSIGHT_CHANNELS = [
    {
        "id": "tcp_artificial_muscle_core",
        "label": "TCP / 人工肌肉核心研究",
        "description": "TCP、纤维扭转缠绕、人工肌肉、柔性驱动中的核心研究内容。",
        "focus_prompt": "Prioritize TCP artificial muscle mechanisms, fabrication, testing, and actuator research details.",
        "boundary_prompt": "Exclude generic robotics news unless it directly informs TCP artificial muscle research.",
        "reading_depth": "deep",
        "default_flow_policy": "normal",
        "sort_order": 10,
    },
    {
        "id": "soft_actuation_horizon",
        "label": "柔性驱动视野拓展",
        "description": "软体机器人、柔性驱动、材料与器件方向的相邻研究视野。",
        "focus_prompt": "Prioritize adjacent soft actuation, soft robotics, compliant materials, and device-level research.",
        "boundary_prompt": "Exclude broad consumer robotics or automation items without soft-actuation relevance.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 20,
    },
    {
        "id": "academic_horizon",
        "label": "学术视野拓展",
        "description": "跨学科研究前沿、科研方法、学术工作流与通用研究能力。",
        "focus_prompt": "Prioritize research methods, academic workflows, frontier papers, and durable scholarly context.",
        "boundary_prompt": "Exclude generic campus or publication-industry news unless it changes research practice.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 30,
    },
    {
        "id": "ai_agents_frontier",
        "label": "AI & Agents 前沿",
        "description": "AI、Agents、模型能力、研究进展与一线研究者观点。",
        "focus_prompt": "Prioritize AI model capability, agents, frontier research, tooling shifts, and practitioner signals.",
        "boundary_prompt": "Exclude generic AI productivity tips without technical or strategic substance.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 40,
    },
    {
        "id": "open_source_tools_practice",
        "label": "开源工具与工程实践",
        "description": "开源软件、开发工具、工程实践、贡献者博客与工具链演进。",
        "focus_prompt": "Prioritize open-source tools, engineering workflows, implementation practice, and maintainable systems.",
        "boundary_prompt": "Exclude shallow launch posts unless there is concrete implementation or workflow value.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 50,
    },
    {
        "id": "securities_investing",
        "label": "证券投资",
        "description": "传统证券、宏观、公司、策略、资产配置与投资研究。",
        "focus_prompt": "Prioritize securities investing, macro context, company analysis, strategy, and portfolio implications.",
        "boundary_prompt": "Exclude clickbait market commentary without clear evidence, thesis, or decision relevance.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 60,
    },
    {
        "id": "cryptocurrency",
        "label": "加密货币",
        "description": "加密投资、空投、套利、链上机会、风险事件与市场结构。",
        "focus_prompt": "Prioritize crypto market structure, risk events, on-chain signals, protocol changes, and actionable context.",
        "boundary_prompt": "Exclude hype, promotional airdrop chatter, and unsourced speculation.",
        "reading_depth": "light",
        "default_flow_policy": "normal",
        "sort_order": 70,
    },
    {
        "id": NEEDS_REVIEW_CHANNEL_ID,
        "label": "待归类",
        "description": "需要人工归类或等待后续 reroute 的文章。",
        "focus_prompt": "Use this channel when no active channel fits, confidence is too low, or lifecycle fallback requires review.",
        "boundary_prompt": "Do not use this channel when a more specific active channel clearly fits the article.",
        "reading_depth": "medium",
        "default_flow_policy": "normal",
        "sort_order": 9990,
        "is_system": True,
    },
]


class InsightChannelNotFoundError(ValueError):
    pass


class InsightChannelConflictError(ValueError):
    pass


class InsightChannelValidationError(ValueError):
    pass


class SourceRoutingNotFoundError(ValueError):
    pass


class SourceRoutingValidationError(ValueError):
    pass


def ensure_default_channels(session: Session) -> list[InsightChannel]:
    ensure_channel_catalog_state(session)
    defaults_by_id = {row["id"]: row for row in DEFAULT_INSIGHT_CHANNELS}
    default_ids = list(defaults_by_id)
    existing_channels = list(session.scalars(select(InsightChannel).where(InsightChannel.id.in_(default_ids))))
    existing_default_ids = {channel.id for channel in existing_channels}
    missing_defaults = [row for row in DEFAULT_INSIGHT_CHANNELS if row["id"] not in existing_default_ids]
    if missing_defaults:
        session.add_all(
            InsightChannel(
                enabled=True,
                is_system=bool(row.get("is_system")),
                **{key: value for key, value in row.items() if key != "is_system"},
            )
            for row in missing_defaults
        )
        session.flush()

    for channel in existing_channels:
        default = defaults_by_id[channel.id]
        changed = False
        if not (channel.focus_prompt or "").strip():
            channel.focus_prompt = default["focus_prompt"]
            changed = True
        if not (channel.boundary_prompt or "").strip():
            channel.boundary_prompt = default["boundary_prompt"]
            changed = True
        if not (channel.default_flow_policy or "").strip():
            channel.default_flow_policy = default["default_flow_policy"]
            changed = True
        should_be_system = bool(default.get("is_system"))
        if channel.is_system != should_be_system:
            channel.is_system = should_be_system
            changed = True
        if channel.id == NEEDS_REVIEW_CHANNEL_ID:
            for field in (
                "label",
                "description",
                "focus_prompt",
                "boundary_prompt",
                "reading_depth",
                "default_flow_policy",
                "sort_order",
            ):
                if getattr(channel, field) != default[field]:
                    setattr(channel, field, default[field])
                    changed = True
            if not channel.enabled:
                channel.enabled = True
                changed = True
            if channel.retired_at is not None:
                channel.retired_at = None
                changed = True
        if changed:
            session.add(channel)
    if existing_channels:
        session.flush()

    return list_channels(session, include_disabled=True)


def ensure_channel_catalog_state(session: Session, *, for_update: bool = False) -> InsightChannelCatalogState:
    statement = select(InsightChannelCatalogState).where(InsightChannelCatalogState.id == CHANNEL_CATALOG_STATE_ID)
    if for_update:
        statement = statement.with_for_update()
    state = session.scalar(statement)
    if state is None:
        state = InsightChannelCatalogState(id=CHANNEL_CATALOG_STATE_ID, channel_catalog_version=1)
        session.add(state)
        session.flush()
    return state


def get_channel_catalog_version(session: Session) -> int:
    return ensure_channel_catalog_state(session).channel_catalog_version


def bump_channel_catalog_version(session: Session) -> int:
    state = ensure_channel_catalog_state(session, for_update=True)
    state.channel_catalog_version += 1
    state.updated_at = _now_utc()
    session.add(state)
    session.flush()
    return state.channel_catalog_version


def validate_expected_catalog_version(session: Session, expected_version: int | None) -> None:
    if expected_version is None:
        return
    current_version = ensure_channel_catalog_state(session, for_update=True).channel_catalog_version
    if current_version != expected_version:
        raise InsightChannelConflictError(
            f"Insight channel catalog version changed; expected {expected_version}, current {current_version}"
        )


def list_channels(
    session: Session,
    include_disabled: bool = True,
    include_retired: bool = True,
) -> list[InsightChannel]:
    statement = select(InsightChannel)
    if not include_disabled:
        statement = statement.where(InsightChannel.enabled.is_(True))
    if not include_retired:
        statement = statement.where(InsightChannel.retired_at.is_(None))
    statement = statement.order_by(InsightChannel.sort_order.asc(), InsightChannel.id.asc())
    return list(session.scalars(statement))


def list_active_channels(session: Session) -> list[InsightChannel]:
    return list_channels(session, include_disabled=False, include_retired=False)


def build_channel_read(channel: InsightChannel, *, channel_catalog_version: int) -> InsightChannelRead:
    return InsightChannelRead(
        id=channel.id,
        label=channel.label,
        description=channel.description,
        focus_prompt=channel.focus_prompt,
        boundary_prompt=channel.boundary_prompt,
        reading_depth=channel.reading_depth,
        default_flow_policy=channel.default_flow_policy,
        sort_order=channel.sort_order,
        enabled=channel.enabled,
        is_system=channel.is_system,
        retired_at=channel.retired_at,
        channel_catalog_version=channel_catalog_version,
    )


def channel_is_active(channel: InsightChannel | None) -> bool:
    return bool(channel is not None and channel.enabled and channel.retired_at is None)


def create_channel(session: Session, payload: InsightChannelCreateRequest) -> InsightChannel:
    validate_expected_catalog_version(session, payload.expected_channel_catalog_version)
    channel_id = _validate_new_channel_id(payload.id)
    if session.get(InsightChannel, channel_id) is not None:
        raise InsightChannelConflictError("Insight channel already exists")

    channel = InsightChannel(
        id=channel_id,
        label=_required_text(payload.label, "label"),
        description=(payload.description or "").strip(),
        focus_prompt=(payload.focus_prompt or "").strip(),
        boundary_prompt=(payload.boundary_prompt or "").strip(),
        reading_depth=payload.reading_depth,
        default_flow_policy=payload.default_flow_policy,
        sort_order=payload.sort_order,
        enabled=payload.enabled,
        is_system=False,
        retired_at=None,
    )
    session.add(channel)
    session.flush()
    bump_channel_catalog_version(session)
    session.refresh(channel)
    return channel


def update_channel(session: Session, channel_id: str, payload: InsightChannelUpdateRequest) -> InsightChannel:
    channel = session.get(InsightChannel, channel_id)
    if channel is None:
        raise InsightChannelNotFoundError("Insight channel not found")
    if channel.retired_at is not None:
        raise InsightChannelValidationError("Retired insight channels cannot be edited")
    if channel.is_system:
        raise InsightChannelValidationError("System insight channels cannot be edited")

    update_data = payload.model_dump(exclude_unset=True)
    expected_version = update_data.pop("expected_channel_catalog_version", None)
    validate_expected_catalog_version(session, expected_version)
    changed = False
    for field, value in update_data.items():
        if field in {"label", "description", "focus_prompt", "boundary_prompt"}:
            value = _required_text(value, field) if field == "label" else str(value).strip()
        if getattr(channel, field) != value:
            changed = True
        setattr(channel, field, value)
    if changed:
        bump_channel_catalog_version(session)
    session.flush()
    return channel


def get_source_routing(session: Session, source_id: int) -> SourceInsightRoutingRead:
    source = session.get(Source, source_id)
    if source is None:
        raise SourceRoutingNotFoundError("Source not found")

    tendencies = _list_source_tendencies(session, source_id)
    return SourceInsightRoutingRead(
        source_id=source.id,
        flow_policy=source.flow_policy or "normal",
        channel_tendencies=[
            SourceChannelTendencyRead(
                channel_id=tendency.channel_id,
                tendency=tendency.tendency,
                is_primary=tendency.is_primary,
                notes=tendency.notes,
            )
            for tendency in tendencies
        ],
    )


def update_source_routing(
    session: Session,
    source_id: int,
    payload: SourceInsightRoutingUpdateRequest,
) -> SourceInsightRoutingRead:
    source = session.get(Source, source_id)
    if source is None:
        raise SourceRoutingNotFoundError("Source not found")

    primary_count = sum(1 for tendency in payload.channel_tendencies if tendency.is_primary)
    if primary_count > 1:
        raise SourceRoutingValidationError("At most one source channel tendency can be primary")

    channel_ids = [tendency.channel_id for tendency in payload.channel_tendencies]
    duplicate_channel_ids = sorted({channel_id for channel_id in channel_ids if channel_ids.count(channel_id) > 1})
    if duplicate_channel_ids:
        raise SourceRoutingValidationError(
            f"Duplicate channel ids are not allowed: {', '.join(duplicate_channel_ids)}"
        )

    if channel_ids:
        known_channel_ids = set(
            session.scalars(
                select(InsightChannel.id)
                .where(InsightChannel.id.in_(channel_ids))
                .where(InsightChannel.enabled.is_(True))
                .where(InsightChannel.retired_at.is_(None))
            ).all()
        )
        unknown_channel_ids = sorted(set(channel_ids) - known_channel_ids)
        if unknown_channel_ids:
            raise SourceRoutingValidationError(
                f"Insight channel is not active or does not exist: {unknown_channel_ids[0]}"
            )

    source.flow_policy = payload.flow_policy
    session.execute(delete(SourceChannelTendency).where(SourceChannelTendency.source_id == source.id))
    session.add_all(
        SourceChannelTendency(
            source_id=source.id,
            channel_id=tendency.channel_id,
            tendency=tendency.tendency,
            is_primary=tendency.is_primary,
            notes=tendency.notes,
        )
        for tendency in payload.channel_tendencies
    )
    session.flush()
    return get_source_routing(session, source.id)


def _list_source_tendencies(session: Session, source_id: int) -> list[SourceChannelTendency]:
    statement = (
        select(SourceChannelTendency)
        .where(SourceChannelTendency.source_id == source_id)
        .order_by(
            SourceChannelTendency.is_primary.desc(),
            SourceChannelTendency.tendency.desc(),
            SourceChannelTendency.channel_id.asc(),
        )
    )
    return list(session.scalars(statement))


def _validate_new_channel_id(value: str) -> str:
    channel_id = value.strip()
    if channel_id == NEEDS_REVIEW_CHANNEL_ID:
        raise InsightChannelValidationError("needs_review is a reserved system channel id")
    if not CHANNEL_ID_PATTERN.fullmatch(channel_id):
        raise InsightChannelValidationError(
            "Insight channel id must be 2-128 lowercase letters, numbers, or underscores"
        )
    return channel_id


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise InsightChannelValidationError(f"Insight channel {field_name} must not be blank")
    return normalized


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
