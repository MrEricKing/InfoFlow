from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, get_settings
from app.core.security import (
    SecretConfigurationError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_value,
    mask_secret as mask_secret_value,
    validate_encryption_key,
)
from app.db.models import AppSetting
from app.schemas.settings import (
    AIProfile,
    AIProfileSettingsRead,
    AIProfileSettingsUpdateRequest,
    AISettingsRead,
    AISettingsUpdateRequest,
    InsightEngineSettingsRead,
    InsightEngineSettingsUpdateRequest,
    RsshubSettingsRead,
    RsshubSettingsUpdateRequest,
    StoragePathRead,
    StoragePathsRead,
    StoragePathsUpdateRequest,
)
from app.services.source_resolver import (
    SETTING_RSSHUB_BASE_URL,
    get_effective_rsshub_base_url,
    normalize_rsshub_base_url,
)

SETTING_KNOWLEDGE_BASE_ROOT = "knowledge_base_root"
SETTING_AI_PROVIDER = "ai_provider"
SETTING_AI_ENABLED = "ai_enabled"
SETTING_AI_BASE_URL = "ai_base_url"
SETTING_AI_MODEL = "ai_model"
SETTING_AI_API_KEY = "ai_api_key"
SETTING_AI_DEEPSEEK_THINKING = "ai_deepseek_thinking"
SETTING_AI_ARTIFACT_CONCURRENCY = "ai_artifact_concurrency"
SETTING_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED = "auto_low_priority_archiving_enabled"
SETTING_LOW_PRIORITY_SCORE_THRESHOLD = "low_priority_score_threshold"

AI_PROVIDER_DEEPSEEK = "deepseek"
AI_PROVIDER_OPENAI_COMPATIBLE = "openai-compatible"
AI_PROVIDER_LM_STUDIO = "lm-studio"
SUPPORTED_AI_PROVIDERS = (AI_PROVIDER_DEEPSEEK, AI_PROVIDER_OPENAI_COMPATIBLE, AI_PROVIDER_LM_STUDIO)
AI_DEEPSEEK_THINKING_DISABLED = "disabled"
AI_DEEPSEEK_THINKING_ENABLED = "enabled"
AI_DEEPSEEK_THINKING_VALUES = (AI_DEEPSEEK_THINKING_DISABLED, AI_DEEPSEEK_THINKING_ENABLED)
AI_PROFILE_GENERAL = "general"
AI_PROFILE_SENSITIVE = "sensitive"
AI_PROFILES: tuple[AIProfile, ...] = (AI_PROFILE_GENERAL, AI_PROFILE_SENSITIVE)
AI_ROUTE_DISABLED = "disabled"
AI_SOURCE_ROUTES = (AI_PROFILE_GENERAL, AI_PROFILE_SENSITIVE, AI_ROUTE_DISABLED)
DEFAULT_AI_PROFILE_PROVIDERS: dict[AIProfile, str] = {
    AI_PROFILE_GENERAL: AI_PROVIDER_DEEPSEEK,
    AI_PROFILE_SENSITIVE: AI_PROVIDER_OPENAI_COMPATIBLE,
}
DEFAULT_AI_ENABLED = False
DEFAULT_AI_PROFILE_BASE_URLS: dict[AIProfile, str] = {
    AI_PROFILE_GENERAL: "https://api.deepseek.com",
    AI_PROFILE_SENSITIVE: "https://api.openai.com",
}
DEFAULT_AI_PROFILE_MODELS: dict[AIProfile, str] = {
    AI_PROFILE_GENERAL: "deepseek-v4-flash",
    AI_PROFILE_SENSITIVE: "gpt-4.1-mini",
}
DEFAULT_AI_ARTIFACT_CONCURRENCY_BY_PROVIDER: dict[str, int] = {
    AI_PROVIDER_DEEPSEEK: 8,
    AI_PROVIDER_OPENAI_COMPATIBLE: 1,
    AI_PROVIDER_LM_STUDIO: 1,
}
MIN_AI_ARTIFACT_CONCURRENCY = 1
MAX_AI_ARTIFACT_CONCURRENCY = 32
DEFAULT_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED = False
DEFAULT_LOW_PRIORITY_SCORE_THRESHOLD = 50
INFOFLOW_AI_API_KEY_SETTING_ATTRS: dict[AIProfile, str] = {
    AI_PROFILE_GENERAL: "ai_general_api_key",
    AI_PROFILE_SENSITIVE: "ai_sensitive_api_key",
}

_SETTING_ENV_NAMES = {
    SETTING_KNOWLEDGE_BASE_ROOT: "KNOWLEDGE_BASE_ROOT",
}
_SETTING_DEFAULTS = {
    SETTING_KNOWLEDGE_BASE_ROOT: PROJECT_ROOT / "KnowledgeBase",
}


@dataclass(frozen=True, slots=True)
class AIProviderConfig:
    profile: AIProfile
    provider: str
    enabled: bool
    base_url: str
    model: str
    api_key: str | None
    deepseek_thinking: str
    artifact_concurrency: int = DEFAULT_AI_ARTIFACT_CONCURRENCY_BY_PROVIDER[
        AI_PROVIDER_OPENAI_COMPATIBLE
    ]

    @property
    def is_configured(self) -> bool:
        base_configured = (
            self.enabled
            and self.provider in SUPPORTED_AI_PROVIDERS
            and bool(self.base_url.strip())
            and bool(self.model.strip())
        )
        if not base_configured:
            return False
        if self.provider == AI_PROVIDER_DEEPSEEK:
            return bool((self.api_key or "").strip())
        if self.provider == AI_PROVIDER_OPENAI_COMPATIBLE:
            return True
        if self.provider == AI_PROVIDER_LM_STUDIO:
            return True
        return False

    @property
    def artifact_model_name(self) -> str:
        return f"{self.provider}/{self.model}"


def get_storage_paths(session: Session) -> StoragePathsRead:
    return StoragePathsRead(
        knowledge_base_root=get_storage_path(session, SETTING_KNOWLEDGE_BASE_ROOT),
    )


def get_rsshub_settings(session: Session) -> RsshubSettingsRead:
    effective = get_effective_rsshub_base_url(session)
    return RsshubSettingsRead(key=effective.key, value=effective.value, source=effective.source)


def get_ai_settings(session: Session) -> AISettingsRead:
    return AISettingsRead(
        profiles={
            profile: _build_ai_profile_settings_read(resolve_ai_provider_config(session, profile=profile))
            for profile in AI_PROFILES
        }
    )


def get_insight_engine_settings(session: Session) -> InsightEngineSettingsRead:
    return InsightEngineSettingsRead(
        auto_low_priority_archiving_enabled=_parse_bool(
            _get_setting_value(session, SETTING_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED),
            default=DEFAULT_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED,
        ),
        low_priority_score_threshold=_parse_int(
            _get_setting_value(session, SETTING_LOW_PRIORITY_SCORE_THRESHOLD),
            default=DEFAULT_LOW_PRIORITY_SCORE_THRESHOLD,
            minimum=0,
            maximum=100,
        ),
    )


def update_insight_engine_settings(
    session: Session,
    payload: InsightEngineSettingsUpdateRequest,
) -> InsightEngineSettingsRead:
    if "auto_low_priority_archiving_enabled" in payload.model_fields_set:
        if payload.auto_low_priority_archiving_enabled is None:
            _delete_setting(session, SETTING_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED)
        else:
            _set_setting(
                session,
                SETTING_AUTO_LOW_PRIORITY_ARCHIVING_ENABLED,
                "true" if payload.auto_low_priority_archiving_enabled else "false",
            )

    if "low_priority_score_threshold" in payload.model_fields_set:
        if payload.low_priority_score_threshold is None:
            _delete_setting(session, SETTING_LOW_PRIORITY_SCORE_THRESHOLD)
        else:
            _set_setting(session, SETTING_LOW_PRIORITY_SCORE_THRESHOLD, str(payload.low_priority_score_threshold))

    session.flush()
    return get_insight_engine_settings(session)


def update_ai_settings(session: Session, payload: AISettingsUpdateRequest) -> AISettingsRead:
    for profile, profile_payload in payload.profiles.items():
        _update_ai_profile_settings(session, profile=profile, payload=profile_payload)

    session.flush()
    return get_ai_settings(session)


def update_rsshub_settings(session: Session, payload: RsshubSettingsUpdateRequest) -> RsshubSettingsRead:
    if "rsshub_base_url" not in payload.model_fields_set:
        return get_rsshub_settings(session)

    raw_value = payload.rsshub_base_url
    if raw_value is None or not raw_value.strip():
        _delete_setting(session, SETTING_RSSHUB_BASE_URL)
    else:
        _set_setting(session, SETTING_RSSHUB_BASE_URL, normalize_rsshub_base_url(raw_value))

    session.flush()
    return get_rsshub_settings(session)


def update_storage_paths(session: Session, payload: StoragePathsUpdateRequest) -> StoragePathsRead:
    for key in (SETTING_KNOWLEDGE_BASE_ROOT,):
        if key not in payload.model_fields_set:
            continue

        raw_value = getattr(payload, key)
        if raw_value is None or not raw_value.strip():
            _delete_setting(session, key)
            continue

        resolved_path = normalize_storage_path(raw_value)
        _set_setting(session, key, str(resolved_path))

    session.flush()
    return get_storage_paths(session)


def validate_storage_paths(
    session: Session,
    *,
    knowledge_base_root: str | None = None,
) -> StoragePathsRead:
    return StoragePathsRead(
        knowledge_base_root=_build_storage_path_read(
            key=SETTING_KNOWLEDGE_BASE_ROOT,
            value=knowledge_base_root,
            source="input" if knowledge_base_root is not None else None,
            session=session,
        ),
    )


def resolve_knowledge_base_root(session: Session) -> Path:
    return Path(get_storage_path(session, SETTING_KNOWLEDGE_BASE_ROOT).resolved_path)


def resolve_ai_provider_config(session: Session, *, profile: AIProfile = AI_PROFILE_GENERAL) -> AIProviderConfig:
    _validate_ai_profile(profile)
    provider = _get_ai_profile_setting_value(session, profile, SETTING_AI_PROVIDER) or DEFAULT_AI_PROFILE_PROVIDERS[profile]
    enabled = _parse_bool(
        _get_ai_profile_setting_value(session, profile, SETTING_AI_ENABLED),
        default=DEFAULT_AI_ENABLED,
    )
    base_url = _get_ai_profile_setting_value(session, profile, SETTING_AI_BASE_URL) or DEFAULT_AI_PROFILE_BASE_URLS[profile]
    model = _get_ai_profile_setting_value(session, profile, SETTING_AI_MODEL) or DEFAULT_AI_PROFILE_MODELS[profile]
    api_key = _get_ai_profile_setting_value(session, profile, SETTING_AI_API_KEY)
    deepseek_thinking = (
        _get_ai_profile_setting_value(session, profile, SETTING_AI_DEEPSEEK_THINKING)
        or AI_DEEPSEEK_THINKING_DISABLED
    )
    artifact_concurrency = _parse_int(
        _get_ai_profile_setting_value(session, profile, SETTING_AI_ARTIFACT_CONCURRENCY),
        default=default_ai_artifact_concurrency(provider),
        minimum=MIN_AI_ARTIFACT_CONCURRENCY,
        maximum=MAX_AI_ARTIFACT_CONCURRENCY,
    )
    return AIProviderConfig(
        profile=profile,
        provider=provider,
        enabled=enabled,
        base_url=base_url,
        model=model,
        api_key=api_key,
        deepseek_thinking=deepseek_thinking,
        artifact_concurrency=artifact_concurrency,
    )


def validate_ai_secret_settings_for_startup(session: Session) -> None:
    settings = get_settings()
    encryption_key = settings.settings_encryption_key
    if encryption_key:
        validate_encryption_key(encryption_key)

    secret_keys = set(_ai_api_key_setting_keys())
    statement = select(AppSetting).where(AppSetting.key.in_(secret_keys))
    for setting in session.scalars(statement):
        if not setting.value.strip():
            continue
        if is_encrypted_value(setting.value):
            decrypt_secret(setting.value, encryption_key)
            continue
        if encryption_key:
            setting.value = encrypt_secret(setting.value, encryption_key)
            session.add(setting)
            continue
        if settings.environment.strip().lower() == "production":
            raise SecretConfigurationError(
                "Plaintext AI API key settings require INFOFLOW_SETTINGS_ENCRYPTION_KEY in production"
            )
    session.flush()


def resolve_ai_provider_config_for_test(
    session: Session,
    *,
    profile: AIProfile = AI_PROFILE_GENERAL,
    payload: AIProfileSettingsUpdateRequest | None = None,
) -> AIProviderConfig:
    config = resolve_ai_provider_config(session, profile=profile)
    if payload is None:
        return config

    provider = config.provider
    enabled = config.enabled
    base_url = config.base_url
    model = config.model
    api_key = config.api_key
    deepseek_thinking = config.deepseek_thinking
    artifact_concurrency = config.artifact_concurrency

    if "provider" in payload.model_fields_set:
        provider = payload.provider or DEFAULT_AI_PROFILE_PROVIDERS[profile]
        if provider not in SUPPORTED_AI_PROVIDERS:
            raise ValueError("Unsupported AI provider")

    if "enabled" in payload.model_fields_set:
        enabled = DEFAULT_AI_ENABLED if payload.enabled is None else payload.enabled

    if "base_url" in payload.model_fields_set:
        if payload.base_url is None:
            base_url = DEFAULT_AI_PROFILE_BASE_URLS[profile]
        elif payload.base_url.strip():
            base_url = payload.base_url.strip()
        else:
            base_url = ""

    if "model" in payload.model_fields_set:
        if payload.model is None:
            model = DEFAULT_AI_PROFILE_MODELS[profile]
        else:
            model = payload.model.strip()

    if "api_key" in payload.model_fields_set:
        if payload.api_key is None:
            api_key = None
        elif payload.api_key.strip():
            api_key = payload.api_key.strip()

    if "deepseek_thinking" in payload.model_fields_set:
        if payload.deepseek_thinking is None or not payload.deepseek_thinking.strip():
            deepseek_thinking = AI_DEEPSEEK_THINKING_DISABLED
        else:
            deepseek_thinking = payload.deepseek_thinking.strip().lower()
            if deepseek_thinking not in AI_DEEPSEEK_THINKING_VALUES:
                raise ValueError("Unsupported DeepSeek thinking mode")

    if "artifact_concurrency" in payload.model_fields_set:
        artifact_concurrency = (
            default_ai_artifact_concurrency(provider)
            if payload.artifact_concurrency is None
            else _clamp_int(
                payload.artifact_concurrency,
                minimum=MIN_AI_ARTIFACT_CONCURRENCY,
                maximum=MAX_AI_ARTIFACT_CONCURRENCY,
            )
        )

    return AIProviderConfig(
        profile=profile,
        provider=provider,
        enabled=enabled,
        base_url=base_url,
        model=model,
        api_key=api_key,
        deepseek_thinking=deepseek_thinking,
        artifact_concurrency=artifact_concurrency,
    )


def resolve_ai_provider_config_for_source_route(session: Session, ai_route: str | None) -> AIProviderConfig | None:
    route = normalize_ai_source_route(ai_route)
    if route == AI_ROUTE_DISABLED:
        return None
    if route == AI_PROFILE_SENSITIVE:
        return resolve_ai_provider_config(session, profile=AI_PROFILE_SENSITIVE)
    return resolve_ai_provider_config(session, profile=AI_PROFILE_GENERAL)


def normalize_ai_source_route(ai_route: str | None) -> str:
    if ai_route is None:
        return AI_PROFILE_GENERAL
    route = ai_route.strip().lower()
    if route not in AI_SOURCE_ROUTES:
        raise ValueError(f"Unsupported source AI route: {ai_route}")
    return route


def normalize_ai_base_url(value: str) -> str:
    raw_value = value.strip()
    parsed = urlparse(raw_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("AI base URL must be a valid http or https URL")
    if parsed.username or parsed.password:
        raise ValueError("AI base URL must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("AI base URL must not include query strings or fragments")

    path_parts = [part for part in parsed.path.split("/") if part]
    if any(part == ".." for part in path_parts):
        raise ValueError("AI base URL must not include path traversal segments")
    if path_parts and path_parts[-1].lower() == "v1":
        path_parts = path_parts[:-1]

    path = f"/{'/'.join(path_parts)}" if path_parts else ""
    normalized = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))
    return normalized.rstrip("/")


def build_ai_chat_completions_url(config: AIProviderConfig) -> str:
    normalized_base_url = normalize_ai_base_url(config.base_url)
    if config.provider == AI_PROVIDER_DEEPSEEK:
        return f"{normalized_base_url}/chat/completions"
    if config.provider == AI_PROVIDER_OPENAI_COMPATIBLE:
        return f"{normalized_base_url}/v1/chat/completions"
    if config.provider == AI_PROVIDER_LM_STUDIO:
        return f"{normalized_base_url}/v1/chat/completions"
    raise ValueError("Unsupported AI provider")


def default_ai_artifact_concurrency(provider: str) -> int:
    return DEFAULT_AI_ARTIFACT_CONCURRENCY_BY_PROVIDER.get(provider, 1)


def effective_ai_artifact_concurrency(session: Session, profile: AIProfile) -> int:
    return resolve_ai_provider_config(session, profile=profile).artifact_concurrency


def mask_secret(value: str | None) -> str | None:
    return mask_secret_value(value)


def _build_ai_profile_settings_read(config: AIProviderConfig) -> AIProfileSettingsRead:
    return AIProfileSettingsRead(
        profile=config.profile,
        provider=config.provider,
        enabled=config.enabled,
        base_url=config.base_url,
        model=config.model,
        deepseek_thinking=config.deepseek_thinking,
        artifact_concurrency=config.artifact_concurrency,
        api_key_configured=bool(config.api_key),
        api_key_masked=mask_secret(config.api_key),
    )


def _update_ai_profile_settings(
    session: Session,
    *,
    profile: AIProfile,
    payload: AIProfileSettingsUpdateRequest,
) -> None:
    _validate_ai_profile(profile)

    if "provider" in payload.model_fields_set:
        provider = payload.provider or DEFAULT_AI_PROFILE_PROVIDERS[profile]
        if provider not in SUPPORTED_AI_PROVIDERS:
            raise ValueError("Unsupported AI provider")
        _set_profile_setting(session, profile, SETTING_AI_PROVIDER, provider)

    if "deepseek_thinking" in payload.model_fields_set:
        if payload.deepseek_thinking is None or not payload.deepseek_thinking.strip():
            _delete_profile_setting(session, profile, SETTING_AI_DEEPSEEK_THINKING)
        else:
            deepseek_thinking = payload.deepseek_thinking.strip().lower()
            if deepseek_thinking not in AI_DEEPSEEK_THINKING_VALUES:
                raise ValueError("Unsupported DeepSeek thinking mode")
            _set_profile_setting(session, profile, SETTING_AI_DEEPSEEK_THINKING, deepseek_thinking)

    if "enabled" in payload.model_fields_set:
        if payload.enabled is None:
            _delete_profile_setting(session, profile, SETTING_AI_ENABLED)
        else:
            _set_profile_setting(session, profile, SETTING_AI_ENABLED, "true" if payload.enabled else "false")

    if "base_url" in payload.model_fields_set:
        if payload.base_url is None or not payload.base_url.strip():
            _delete_profile_setting(session, profile, SETTING_AI_BASE_URL)
        else:
            _set_profile_setting(session, profile, SETTING_AI_BASE_URL, normalize_ai_base_url(payload.base_url))

    if "model" in payload.model_fields_set:
        if payload.model is None or not payload.model.strip():
            _delete_profile_setting(session, profile, SETTING_AI_MODEL)
        else:
            _set_profile_setting(session, profile, SETTING_AI_MODEL, payload.model.strip())

    if "api_key" in payload.model_fields_set:
        if payload.api_key is None or not payload.api_key.strip():
            _delete_profile_setting(session, profile, SETTING_AI_API_KEY)
        else:
            _set_profile_secret_setting(session, profile, SETTING_AI_API_KEY, payload.api_key.strip())

    if "artifact_concurrency" in payload.model_fields_set:
        if payload.artifact_concurrency is None:
            _delete_profile_setting(session, profile, SETTING_AI_ARTIFACT_CONCURRENCY)
        else:
            _set_profile_setting(
                session,
                profile,
                SETTING_AI_ARTIFACT_CONCURRENCY,
                str(
                    _clamp_int(
                        payload.artifact_concurrency,
                        minimum=MIN_AI_ARTIFACT_CONCURRENCY,
                        maximum=MAX_AI_ARTIFACT_CONCURRENCY,
                    )
                ),
            )


def get_storage_path(session: Session, key: str) -> StoragePathRead:
    setting = _get_setting(session, key)
    if setting is not None:
        return _build_storage_path_read(key=key, value=setting.value, source="db", session=session)

    settings = get_settings()
    configured_value = getattr(settings, key)
    source = _resolve_config_source(key, configured_value)
    return _build_storage_path_read(key=key, value=str(configured_value), source=source, session=session)


def normalize_storage_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _build_storage_path_read(
    *,
    key: str,
    value: str | None,
    source: str | None,
    session: Session,
) -> StoragePathRead:
    if value is None:
        effective = get_storage_path(session, key)
        value = effective.value
        source = effective.source

    resolved_path = normalize_storage_path(value)
    exists = resolved_path.exists()
    is_dir = resolved_path.is_dir()
    is_writable, error = _probe_writable(resolved_path)
    return StoragePathRead(
        key=key,
        value=str(value),
        resolved_path=str(resolved_path),
        source=source or "default",
        exists=exists,
        is_dir=is_dir,
        is_writable=is_writable,
        is_inside_project=_is_inside_project(resolved_path),
        error=error,
    )


def _resolve_config_source(key: str, configured_value: Path) -> str:
    if _SETTING_ENV_NAMES[key] in os.environ:
        return "env"
    if normalize_storage_path(configured_value) != normalize_storage_path(_SETTING_DEFAULTS[key]):
        return "env"
    return "default"


def _probe_writable(path: Path) -> tuple[bool, str | None]:
    if path.exists() and not path.is_dir():
        return False, "Path exists but is not a directory"

    probe_dir = path if path.exists() else _nearest_existing_parent(path)
    if probe_dir is None:
        return False, "No existing parent directory"
    if not probe_dir.is_dir():
        return False, "Nearest existing parent is not a directory"

    try:
        with tempfile.NamedTemporaryFile(prefix=".infoflow-write-test-", dir=probe_dir, delete=True):
            pass
    except OSError as exc:
        return False, str(exc)
    return True, None


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _is_inside_project(path: Path) -> bool:
    try:
        path.relative_to(PROJECT_ROOT.resolve(strict=False))
    except ValueError:
        return False
    return True


def _get_setting(session: Session, key: str) -> AppSetting | None:
    return session.scalar(select(AppSetting).where(AppSetting.key == key).limit(1))


def _get_setting_value(session: Session, key: str) -> str | None:
    setting = _get_setting(session, key)
    return setting.value if setting is not None else None


def _get_ai_profile_setting_value(session: Session, profile: AIProfile, setting_key: str) -> str | None:
    if setting_key == SETTING_AI_API_KEY:
        return _get_ai_api_key_setting_value(session, profile)

    return _get_setting_value(session, _ai_profile_setting_key(profile, setting_key))


def _set_setting(session: Session, key: str, value: str) -> None:
    setting = _get_setting(session, key)
    if setting is None:
        session.add(AppSetting(key=key, value=value))
    else:
        setting.value = value
        session.add(setting)


def _delete_setting(session: Session, key: str) -> None:
    setting = _get_setting(session, key)
    if setting is not None:
        session.delete(setting)


def _set_profile_setting(session: Session, profile: AIProfile, setting_key: str, value: str) -> None:
    _set_setting(session, _ai_profile_setting_key(profile, setting_key), value)


def _set_profile_secret_setting(session: Session, profile: AIProfile, setting_key: str, value: str) -> None:
    encryption_key = get_settings().settings_encryption_key
    validate_encryption_key(encryption_key)
    _set_profile_setting(session, profile, setting_key, encrypt_secret(value, encryption_key))


def _get_ai_api_key_setting_value(session: Session, profile: AIProfile) -> str | None:
    env_value = _get_env_ai_api_key(profile)
    if env_value:
        return env_value

    profile_key = _ai_profile_setting_key(profile, SETTING_AI_API_KEY)
    value = _get_setting_value(session, profile_key)
    if value is not None:
        return _decrypt_setting_secret(value)
    return None


def _decrypt_setting_secret(value: str) -> str:
    if not is_encrypted_value(value):
        return value
    return decrypt_secret(value, get_settings().settings_encryption_key)


def _get_env_ai_api_key(profile: AIProfile) -> str | None:
    value = getattr(get_settings(), INFOFLOW_AI_API_KEY_SETTING_ATTRS[profile])
    if value is None or not value.strip():
        return None
    return value.strip()


def _ai_api_key_setting_keys() -> tuple[str, ...]:
    return (
        _ai_profile_setting_key(AI_PROFILE_GENERAL, SETTING_AI_API_KEY),
        _ai_profile_setting_key(AI_PROFILE_SENSITIVE, SETTING_AI_API_KEY),
    )


def _delete_profile_setting(session: Session, profile: AIProfile, setting_key: str) -> None:
    _delete_setting(session, _ai_profile_setting_key(profile, setting_key))


def _ai_profile_setting_key(profile: AIProfile, setting_key: str) -> str:
    suffix = setting_key.removeprefix("ai_")
    return f"ai_{profile}_{suffix}"


def _validate_ai_profile(profile: str) -> None:
    if profile not in AI_PROFILES:
        raise ValueError("Unsupported AI profile")


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return _clamp_int(parsed, minimum=minimum, maximum=maximum)


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))
