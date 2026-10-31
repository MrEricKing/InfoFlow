from __future__ import annotations

from email import policy
from email.parser import BytesParser
from typing import TypedDict

from fastapi import Request

from app.core.safety import MULTIPART_BODY_OVERHEAD_MAX_BYTES, SafetyError, read_request_body_with_limit
from app.services.knowledge_constants import UPLOAD_MAX_BYTES
from app.services.knowledge_errors import UploadMaterialValidationError


class MultipartUploadPayload(TypedDict):
    file_bytes: bytes
    filename: str | None
    content_type: str | None
    material_type: str
    title: str | None
    user_note: str | None
    ai_route: str


async def read_multipart_upload_request(request: Request, *, purpose: str) -> MultipartUploadPayload:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        raise UploadMaterialValidationError("Upload request must be multipart/form-data")

    try:
        body = await read_request_body_with_limit(
            request,
            max_bytes=UPLOAD_MAX_BYTES + MULTIPART_BODY_OVERHEAD_MAX_BYTES,
            purpose=purpose,
        )
    except SafetyError as exc:
        raise UploadMaterialValidationError(str(exc)) from exc

    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    if not message.is_multipart():
        raise UploadMaterialValidationError("Upload request must include multipart form data")

    fields: dict[str, str] = {}
    file_bytes: bytes | None = None
    filename: str | None = None
    file_content_type: str | None = None
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name:
            continue
        payload = part.get_payload(decode=True) or b""
        part_filename = part.get_filename()
        if name == "file":
            file_bytes = payload
            filename = part_filename
            file_content_type = part.get_content_type()
            continue
        try:
            fields[name] = payload.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise UploadMaterialValidationError(f"Form field {name} must be UTF-8 encoded") from exc

    if file_bytes is None:
        raise UploadMaterialValidationError("Upload request must include a file field")

    return {
        "file_bytes": file_bytes,
        "filename": filename,
        "content_type": file_content_type,
        "material_type": fields.get("material_type") or "",
        "title": fields.get("title") or None,
        "user_note": fields.get("user_note") or None,
        "ai_route": fields.get("ai_route") or "sensitive",
    }
