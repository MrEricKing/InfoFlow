from __future__ import annotations

import json
import re
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTED_VALUE_PREFIX = "enc:v1:"
MAX_OPERATION_PAYLOAD_BYTES = 16 * 1024

SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
)

LARGE_CONTENT_KEY_FRAGMENTS = (
    "article_body",
    "body_html",
    "content_html",
    "content_text",
    "full_text",
    "html",
    "raw_body",
    "raw_bytes",
    "raw_content",
    "raw_html",
    "singlefile",
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"'}]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password)\s*[:=]\s*[\"']?)[^\"'\s,;}]+"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{3,}(?=[^A-Za-z0-9._-]|$)"),
)


class SecretConfigurationError(ValueError):
    pass


class PayloadValidationError(ValueError):
    pass


def is_encrypted_value(value: str | None) -> bool:
    return bool(value and value.startswith(ENCRYPTED_VALUE_PREFIX))


def encrypt_secret(value: str, key: str | None) -> str:
    fernet = _build_fernet(key)
    token = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def decrypt_secret(value: str, key: str | None) -> str:
    if not is_encrypted_value(value):
        return value
    fernet = _build_fernet(key)
    token = value[len(ENCRYPTED_VALUE_PREFIX) :].encode("ascii")
    try:
        return fernet.decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise SecretConfigurationError(
            "INFOFLOW_SETTINGS_ENCRYPTION_KEY cannot decrypt existing encrypted settings"
        ) from exc


def validate_encryption_key(key: str | None) -> None:
    _build_fernet(key)


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, entry in value.items():
            if is_sensitive_key(key):
                redacted[key] = "[REDACTED]"
            elif is_large_content_key(key):
                redacted[key] = "[OMITTED]"
            else:
                redacted[key] = redact_payload(entry)
        return redacted
    if isinstance(value, list):
        return [redact_payload(entry) for entry in value]
    return value


def redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", redacted)
    return redacted


def validate_operation_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    _validate_payload_keys(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_OPERATION_PAYLOAD_BYTES:
        raise PayloadValidationError("Operation/job payload exceeds 16KB limit")
    return value


def sanitized_operation_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    validated = validate_operation_payload(value)
    redacted = redact_payload(validated)
    return redacted if isinstance(redacted, dict) else None


def is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def is_large_content_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in LARGE_CONTENT_KEY_FRAGMENTS)


def _validate_payload_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, entry in value.items():
            key_path = f"{path}.{key}"
            if is_sensitive_key(key):
                raise PayloadValidationError(f"Operation/job payload contains secret-like field: {key_path}")
            if is_large_content_key(key):
                raise PayloadValidationError(f"Operation/job payload contains large-content field: {key_path}")
            _validate_payload_keys(entry, path=key_path)
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            _validate_payload_keys(entry, path=f"{path}[{index}]")


def _build_fernet(key: str | None) -> Fernet:
    if not key or not key.strip():
        raise SecretConfigurationError("INFOFLOW_SETTINGS_ENCRYPTION_KEY is required for encrypted settings")
    try:
        return Fernet(key.strip().encode("ascii"))
    except Exception as exc:
        raise SecretConfigurationError("INFOFLOW_SETTINGS_ENCRYPTION_KEY is malformed") from exc


def _normalize_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")
