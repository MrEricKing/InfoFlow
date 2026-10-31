from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import time
from typing import Any

import httpx

from app.core.safety import PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES, build_sanitized_body_preview
from app.services.app_settings import (
    AI_PROVIDER_DEEPSEEK,
    AI_PROVIDER_LM_STUDIO,
    AI_PROVIDER_OPENAI_COMPATIBLE,
    AIProviderConfig,
    SUPPORTED_AI_PROVIDERS,
    build_ai_chat_completions_url,
)

JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}
JSON_RESPONSE_FORMAT = JSON_OBJECT_RESPONSE_FORMAT
CONNECTION_TEST_MAX_TOKENS = 1024
DEEPSEEK_TRANSIENT_RETRY_STATUS_CODES = frozenset({429, 503})
DEEPSEEK_TRANSIENT_RETRY_DELAYS_SECONDS = (15.0, 15.0)
MAX_RETRY_AFTER_DELAY_SECONDS = 15.0
CONNECTION_TEST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
    },
    "required": ["ok"],
}
_JSON_CODE_FENCE_PATTERN = re.compile(r"^```(?:json|JSON)?\s*(?P<body>.*?)\s*```$", re.DOTALL)


class AIProviderError(RuntimeError):
    pass


class AIProviderInvalidJSONError(AIProviderError):
    pass


class AIProviderHTTPError(AIProviderError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AICompletionResult:
    content: str
    latency_ms: int
    raw_response: dict[str, Any]


def create_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(180.0, connect=10.0),
        follow_redirects=True,
        trust_env=False,
    )


def request_chat_completion_json(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.2,
    json_schema_name: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> AICompletionResult:
    if config.provider not in SUPPORTED_AI_PROVIDERS:
        raise AIProviderError("Unsupported AI provider")
    if not config.is_configured:
        raise AIProviderError("AI provider is not configured")

    if config.provider == AI_PROVIDER_DEEPSEEK:
        return request_deepseek_chat_completion_json(
            config,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    if config.provider == AI_PROVIDER_OPENAI_COMPATIBLE:
        return request_openai_compatible_chat_completion_json(
            config,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    if config.provider == AI_PROVIDER_LM_STUDIO:
        return request_lm_studio_chat_completion_json(
            config,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_schema_name=json_schema_name,
            json_schema=json_schema,
        )

    raise AIProviderError("Unsupported AI provider")


def request_deepseek_chat_completion_json(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.2,
) -> AICompletionResult:
    url = build_ai_chat_completions_url(config)
    headers = build_chat_completion_headers(config)
    body = build_deepseek_chat_completion_body(
        config.model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        deepseek_thinking=config.deepseek_thinking,
    )

    with create_http_client() as client:
        started_at = time.perf_counter()
        try:
            raw_response = _post_chat_completion(
                client,
                url=url,
                headers=headers,
                body={**body, "response_format": build_deepseek_response_format()},
                retry_status_codes=DEEPSEEK_TRANSIENT_RETRY_STATUS_CODES,
            )
        except AIProviderHTTPError as exc:
            if exc.status_code not in {400, 422}:
                raise
            raw_response = _post_chat_completion(
                client,
                url=url,
                headers=headers,
                body=body,
                retry_status_codes=DEEPSEEK_TRANSIENT_RETRY_STATUS_CODES,
            )
            content = _extract_message_content(raw_response)
        else:
            try:
                content = _extract_message_content(raw_response)
            except AIProviderError as exc:
                if not _should_retry_without_response_format(config, exc):
                    raise
                raw_response = _post_chat_completion(
                    client,
                    url=url,
                    headers=headers,
                    body=body,
                    retry_status_codes=DEEPSEEK_TRANSIENT_RETRY_STATUS_CODES,
                )
                content = _extract_message_content(raw_response)
        latency_ms = int((time.perf_counter() - started_at) * 1000)

    return AICompletionResult(
        content=content,
        latency_ms=latency_ms,
        raw_response=raw_response,
    )


def request_openai_compatible_chat_completion_json(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.2,
) -> AICompletionResult:
    url = build_ai_chat_completions_url(config)
    headers = build_chat_completion_headers(config)
    body = build_openai_compatible_chat_completion_body(
        config.model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    with create_http_client() as client:
        started_at = time.perf_counter()
        try:
            raw_response = _post_chat_completion(
                client,
                url=url,
                headers=headers,
                body={**body, "response_format": build_openai_compatible_response_format()},
            )
        except AIProviderHTTPError as exc:
            if exc.status_code not in {400, 422}:
                raise
            raw_response = _post_chat_completion(client, url=url, headers=headers, body=body)
        content = _extract_message_content(raw_response)
        latency_ms = int((time.perf_counter() - started_at) * 1000)

    return AICompletionResult(
        content=content,
        latency_ms=latency_ms,
        raw_response=raw_response,
    )


def request_lm_studio_chat_completion_json(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float = 0.2,
    json_schema_name: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> AICompletionResult:
    url = build_ai_chat_completions_url(config)
    headers = build_chat_completion_headers(config)
    body = build_lm_studio_chat_completion_body(
        config.model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    with create_http_client() as client:
        started_at = time.perf_counter()
        raw_response = _post_chat_completion(
            client,
            url=url,
            headers=headers,
            body={
                **body,
                "response_format": build_lm_studio_response_format(
                    json_schema_name=json_schema_name,
                    json_schema=json_schema,
                ),
            },
        )
        content = _extract_message_content(raw_response)
        latency_ms = int((time.perf_counter() - started_at) * 1000)

    return AICompletionResult(
        content=content,
        latency_ms=latency_ms,
        raw_response=raw_response,
    )


def test_chat_completion_connection(config: AIProviderConfig) -> AICompletionResult:
    result = request_chat_completion_json(
        config,
        messages=[
            {"role": "system", "content": "Return only valid JSON."},
            {"role": "user", "content": 'Return exactly this JSON object: {"ok": true}'},
        ],
        max_tokens=CONNECTION_TEST_MAX_TOKENS,
        temperature=0,
        json_schema_name="connection_test",
        json_schema=CONNECTION_TEST_RESPONSE_SCHEMA,
    )
    parse_json_object_content(result.content)
    return result


def parse_json_object_content(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(_strip_json_code_fence(content))
    except json.JSONDecodeError as exc:
        raise AIProviderInvalidJSONError("AI provider returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AIProviderInvalidJSONError("AI provider returned a non-object JSON value")
    return payload


def _post_chat_completion(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    retry_status_codes: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    max_attempts = len(DEEPSEEK_TRANSIENT_RETRY_DELAYS_SECONDS) + 1 if retry_status_codes else 1
    for attempt_index in range(max_attempts):
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise AIProviderError(f"AI provider request failed: {exc}") from exc

        if response.status_code >= 400:
            if response.status_code in retry_status_codes and attempt_index < max_attempts - 1:
                time.sleep(_retry_delay_seconds(response, attempt_index))
                continue
            raise AIProviderHTTPError(response.status_code, _format_response_error(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise AIProviderError("AI provider returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise AIProviderError("AI provider returned a non-object response")
        return payload

    raise AIProviderError("AI provider request failed")


def build_chat_completion_body(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    if config.provider == AI_PROVIDER_DEEPSEEK:
        return build_deepseek_chat_completion_body(
            config.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            deepseek_thinking=config.deepseek_thinking,
        )
    if config.provider == AI_PROVIDER_OPENAI_COMPATIBLE:
        return build_openai_compatible_chat_completion_body(
            config.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    if config.provider == AI_PROVIDER_LM_STUDIO:
        return build_lm_studio_chat_completion_body(
            config.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    raise AIProviderError("Unsupported AI provider")


def build_chat_completion_headers(config: AIProviderConfig) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    api_key = (config.api_key or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def build_deepseek_chat_completion_body(
    model: str,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    deepseek_thinking: str,
) -> dict[str, Any]:
    body = build_base_chat_completion_body(
        model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    body["thinking"] = {"type": deepseek_thinking}
    return body


def build_openai_compatible_chat_completion_body(
    model: str,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    return build_base_chat_completion_body(
        model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def build_lm_studio_chat_completion_body(
    model: str,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    return build_base_chat_completion_body(
        model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def build_base_chat_completion_body(
    model: str,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }


def build_json_schema_response_format(*, name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def build_deepseek_response_format() -> dict[str, str]:
    return dict(JSON_OBJECT_RESPONSE_FORMAT)


def build_openai_compatible_response_format() -> dict[str, str]:
    return dict(JSON_OBJECT_RESPONSE_FORMAT)


def build_lm_studio_response_format(
    *,
    json_schema_name: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if json_schema_name and json_schema:
        return build_json_schema_response_format(name=json_schema_name, schema=json_schema)
    return dict(JSON_OBJECT_RESPONSE_FORMAT)


def build_structured_response_format(
    config: AIProviderConfig,
    *,
    json_schema_name: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if config.provider == AI_PROVIDER_DEEPSEEK:
        return build_deepseek_response_format()
    if config.provider == AI_PROVIDER_OPENAI_COMPATIBLE:
        return build_openai_compatible_response_format()
    if config.provider == AI_PROVIDER_LM_STUDIO:
        return build_lm_studio_response_format(
            json_schema_name=json_schema_name,
            json_schema=json_schema,
        )
    raise AIProviderError("Unsupported AI provider")


def _build_chat_completion_body(
    config: AIProviderConfig,
    *,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    return build_chat_completion_body(
        config,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIProviderError("AI provider response is missing choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise AIProviderError("AI provider response choice is invalid")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise AIProviderError("AI provider response is missing message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("AI provider response message content is empty")
    return content.strip()


def _should_retry_without_response_format(config: AIProviderConfig, error: AIProviderError) -> bool:
    return config.provider == AI_PROVIDER_DEEPSEEK and "message content is empty" in str(error)


def _strip_json_code_fence(content: str) -> str:
    stripped = content.strip()
    match = _JSON_CODE_FENCE_PATTERN.fullmatch(stripped)
    if match is None:
        return stripped
    return match.group("body").strip()


def _format_response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        if body:
            preview = build_sanitized_body_preview(body, max_bytes=PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES)
            suffix = _diagnostic_preview_suffix(preview)
            return f"AI provider HTTP {response.status_code}: {preview['preview']}{suffix}"
        return f"AI provider HTTP {response.status_code}"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            preview = build_sanitized_body_preview(error["message"], max_bytes=PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES)
            suffix = _diagnostic_preview_suffix(preview)
            return f"AI provider HTTP {response.status_code}: {preview['preview']}{suffix}"
        if isinstance(payload.get("message"), str):
            preview = build_sanitized_body_preview(payload["message"], max_bytes=PROVIDER_DIAGNOSTIC_PREVIEW_MAX_BYTES)
            suffix = _diagnostic_preview_suffix(preview)
            return f"AI provider HTTP {response.status_code}: {preview['preview']}{suffix}"
    return f"AI provider HTTP {response.status_code}"


def _diagnostic_preview_suffix(preview: dict[str, Any]) -> str:
    if not preview.get("truncated"):
        return ""
    reason = preview.get("truncation_reason") or "preview truncated"
    return f" ({reason}; original_byte_length={preview.get('original_byte_length')})"


def _retry_delay_seconds(response: httpx.Response, attempt_index: int) -> float:
    retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER_DELAY_SECONDS)
    return DEEPSEEK_TRANSIENT_RETRY_DELAYS_SECONDS[attempt_index]


def _parse_retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None

    raw_value = value.strip()
    if not raw_value:
        return None

    try:
        return max(0.0, float(raw_value))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
