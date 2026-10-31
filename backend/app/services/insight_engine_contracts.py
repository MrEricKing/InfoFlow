from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TAXONOMY_CONTRACT_VERSION = "taxonomy_v1"
CONTENT_TYPE_CONTRACT_VERSION = "content_type_v1"
REASON_CODES_CONTRACT_VERSION = "reason_codes_v1"

TAXONOMY_FALLBACK_ID = "uncategorized"
CONTENT_TYPE_FALLBACK_ID = "unknown"


@dataclass(frozen=True, slots=True)
class TaxonomyItem:
    id: str
    label_zh: str
    description: str


@dataclass(frozen=True, slots=True)
class ContentTypeOption:
    id: str
    label_zh: str


@dataclass(frozen=True, slots=True)
class ReasonCode:
    code: str
    label_zh: str
    source: str
    ai_output_allowed: bool


def _normalize_key(value: str) -> str:
    return " ".join(value.split()).strip()


TAXONOMY_ITEMS: tuple[TaxonomyItem, ...] = (
    TaxonomyItem("ai_technology", "AI 与技术", "AI、模型、自动化、技术趋势、数字工具"),
    TaxonomyItem("software_systems", "软件与系统", "编程、架构、数据库、后端、前端、工程实践"),
    TaxonomyItem("product_business", "产品与商业", "产品设计、商业模式、创业、公司经营、市场判断"),
    TaxonomyItem("economy_finance", "经济与金融", "宏观经济、投资、金融市场、产业周期"),
    TaxonomyItem("policy_society", "政策与社会", "政策、公共议题、社会变化、治理、法律环境"),
    TaxonomyItem("world_news", "国际与时事", "国际新闻、地缘政治、重大事件"),
    TaxonomyItem("science_research", "科学与研究", "科学发现、论文、实验、研究方法"),
    TaxonomyItem("education_learning", "教育与学习", "学习方法、课程、知识体系、个人成长"),
    TaxonomyItem("knowledge_information", "知识与信息管理", "阅读、笔记、知识库、信息流、内容组织"),
    TaxonomyItem("work_career", "工作与职业", "工作方式、组织协作、职业发展、效率方法"),
    TaxonomyItem("design_creativity", "设计与创意", "UI/UX、视觉、内容创作、写作、创意方法"),
    TaxonomyItem("culture_media", "文化与媒体", "书影音、媒体观察、内容行业、公共表达"),
    TaxonomyItem("health_life", "健康与生活", "健康、生活方式、家庭、日常决策"),
    TaxonomyItem("tools_products", "工具与产品动态", "新工具、新产品、平台更新、服务变化"),
    TaxonomyItem("security_privacy", "安全与隐私", "安全、隐私、数据风险、权限、合规"),
    TaxonomyItem("other", "其他", "能归类但不属于以上大类"),
    TaxonomyItem(TAXONOMY_FALLBACK_ID, "待确认", "当前分类体系无法稳定覆盖，需要用户确认"),
)

CONTENT_TYPE_OPTIONS: tuple[ContentTypeOption, ...] = (
    ContentTypeOption("news_update", "新闻快讯"),
    ContentTypeOption("analysis", "分析评论"),
    ContentTypeOption("tutorial", "教程指南"),
    ContentTypeOption("case_study", "案例复盘"),
    ContentTypeOption("reference", "资料参考"),
    ContentTypeOption("release_note", "发布更新"),
    ContentTypeOption("opinion", "观点文章"),
    ContentTypeOption("list_digest", "清单汇总"),
    ContentTypeOption("research_paper", "研究论文"),
    ContentTypeOption(CONTENT_TYPE_FALLBACK_ID, "未确定"),
)

REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode("marked_not_interested", "已标记不感兴趣", "backend_state", False),
    ReasonCode("positive_category_history", "命中过去正反馈较多的领域", "backend_stats", False),
    ReasonCode("negative_category_history", "命中过去负反馈较多的领域", "backend_stats", False),
    ReasonCode("positive_source_history", "来源历史反馈较好", "backend_stats", False),
    ReasonCode("negative_source_history", "来源历史反馈较差", "backend_stats", False),
    ReasonCode("profile_focus_match", "命中当前关注方向", "profile_backend", False),
    ReasonCode("high_actionability", "有明确可执行价值", "ai_content_signal", True),
    ReasonCode("deep_reference_value", "适合长期保存或反复引用", "ai_content_signal", True),
    ReasonCode("specific_insight", "有具体洞察、案例或数据", "ai_content_signal", True),
    ReasonCode("implementation_detail", "包含实现细节或操作步骤", "ai_content_signal", True),
    ReasonCode("thin_content", "内容偏薄，信息量不足", "ai_content_signal", True),
    ReasonCode("marketing_or_pr", "营销或发布稿倾向明显", "ai_content_signal", True),
    ReasonCode("repetitive_update", "像重复转述或常规更新", "ai_content_signal", True),
    ReasonCode("low_content_confidence", "正文不足，判断置信度低", "backend_or_ai", True),
)

_TAXONOMY_BY_ID = {item.id: item for item in TAXONOMY_ITEMS}
_TAXONOMY_ID_BY_LABEL = {_normalize_key(item.label_zh): item.id for item in TAXONOMY_ITEMS}
_CONTENT_TYPE_BY_ID = {option.id: option for option in CONTENT_TYPE_OPTIONS}
_CONTENT_TYPE_ID_BY_LABEL = {_normalize_key(option.label_zh): option.id for option in CONTENT_TYPE_OPTIONS}
_REASON_BY_CODE = {reason.code: reason for reason in REASON_CODES}

AI_ALLOWED_REASON_CODES = tuple(reason.code for reason in REASON_CODES if reason.ai_output_allowed)


def build_taxonomy_context() -> dict[str, Any]:
    return {
        "fallback_id": TAXONOMY_FALLBACK_ID,
        "items": [
            {
                "id": item.id,
                "label_zh": item.label_zh,
                "description": item.description,
            }
            for item in TAXONOMY_ITEMS
        ],
        "instruction": (
            "topics must contain only taxonomy item ids. Taxonomy is a domain category, "
            "not a quality or priority signal."
        ),
    }


def build_content_type_options_context() -> dict[str, Any]:
    return {
        "fallback_id": CONTENT_TYPE_FALLBACK_ID,
        "options": [
            {
                "id": option.id,
                "label_zh": option.label_zh,
            }
            for option in CONTENT_TYPE_OPTIONS
        ],
        "instruction": "content_type must contain exactly one content type id.",
    }


def build_reason_codes_context() -> dict[str, Any]:
    return {
        "ai_allowed_codes": list(AI_ALLOWED_REASON_CODES),
        "codes": [
            {
                "code": reason.code,
                "label_zh": reason.label_zh,
            }
            for reason in REASON_CODES
            if reason.ai_output_allowed
        ],
        "instruction": "priority_reasons[].code must use only codes listed in ai_allowed_codes.",
    }


def canonicalize_taxonomy_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_key(value)
    if normalized in _TAXONOMY_BY_ID:
        return normalized
    return _TAXONOMY_ID_BY_LABEL.get(normalized)


def canonicalize_content_type_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_key(value)
    if normalized in _CONTENT_TYPE_BY_ID:
        return normalized
    return _CONTENT_TYPE_ID_BY_LABEL.get(normalized)


def get_taxonomy_label(taxonomy_id: str) -> str:
    item = _TAXONOMY_BY_ID.get(taxonomy_id) or _TAXONOMY_BY_ID[TAXONOMY_FALLBACK_ID]
    return item.label_zh


def get_content_type_label(content_type_id: str | None) -> str | None:
    if content_type_id is None:
        return None
    option = _CONTENT_TYPE_BY_ID.get(content_type_id)
    return option.label_zh if option is not None else None


def get_reason_code_label(code: str) -> str | None:
    reason = _REASON_BY_CODE.get(code)
    return reason.label_zh if reason is not None else None


def get_ai_allowed_reason_label(code: str) -> str | None:
    reason = _REASON_BY_CODE.get(code)
    if reason is None or not reason.ai_output_allowed:
        return None
    return reason.label_zh
