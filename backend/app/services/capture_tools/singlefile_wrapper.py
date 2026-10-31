from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings
from app.core.safety import (
    CAPTURE_RESPONSE_MAX_BYTES,
    safe_http_get,
    validate_public_http_redirect_chain,
    validate_public_http_url,
)
from app.services.capture_tools.browser_locale import (
    CAPTURE_ACCEPT_LANGUAGE,
    CAPTURE_BROWSER_LOCALE,
    build_accept_language_headers,
)
from app.services.capture_tools.models import CaptureToolDiagnostic, CaptureToolUnavailableError, SingleFileArchiveResult

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised only in broken runtime images.
    PlaywrightError = RuntimeError  # type: ignore[assignment]
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    sync_playwright = None  # type: ignore[assignment]


SINGLEFILE_TOOL_NAME = "singlefile_extension"
SINGLEFILE_PROFILE = "singlefile_extension_archive_v1"
SINGLEFILE_EXTENSION_ID = "jkeljiefjnokjkioahopjglagkbocpbh"
SINGLEFILE_EXTENSION_VERSION = "1.22.98"
SINGLEFILE_EXTENSION_COMMIT = "d8c0ac26add39adf33643b1cff4299e796237ca0"
SINGLEFILE_TIMEOUT_SECONDS = 120
SINGLEFILE_PAGE_LOAD_TIMEOUT_MS = 60_000
SINGLEFILE_PRECAPTURE_TIMEOUT_MS = 35_000
SINGLEFILE_EXTENSION_READY_TIMEOUT_MS = 15_000
SINGLEFILE_DOWNLOAD_POLL_INTERVAL_SECONDS = 0.5
SINGLEFILE_TAIL_LIMIT = 4000

SINGLEFILE_EXTENSION_OPTIONS = {
    "backgroundSave": True,
    "confirmFilename": False,
    "openSavedPage": False,
    "filenameConflictAction": "overwrite",
    "selfExtractingArchive": False,
    "compressContent": False,
    "blockScripts": True,
    "removeHiddenElements": False,
    "removeUnusedStyles": False,
    "loadDeferredImages": True,
    "blockAlternativeImages": False,
    "removeAlternativeImages": False,
    "removeAlternativeMedias": False,
}

SINGLEFILE_BROWSER_ARGS = (
    "--allow-file-access-from-files",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-popup-blocking",
    "--disable-setuid-sandbox",
    "--no-default-browser-check",
    "--no-first-run",
    "--no-sandbox",
    f"--lang={CAPTURE_BROWSER_LOCALE}",
)

INFOFLOW_SEND_MESSAGE_SCRIPT = """
([extensionId, payload]) => new Promise((resolve) => {
  chrome.runtime.sendMessage(extensionId, payload, (response) => {
    resolve({
      response,
      error: chrome.runtime.lastError && chrome.runtime.lastError.message || null
    });
  });
})
"""


@dataclass(frozen=True, slots=True)
class SingleFileInvocation:
    url: str
    output_path: Path
    extension_path: Path
    extension_id: str
    extension_version: str
    browser_executable_path: str | None
    request_id: str
    browser_args: list[str]
    save_options: dict[str, object]
    profile: str = SINGLEFILE_PROFILE
    timeout_seconds: int = SINGLEFILE_TIMEOUT_SECONDS

    def tool_args_json(self) -> dict[str, object]:
        return {
            "url": self.url,
            "integration_mode": "mv3_extension",
            "profile": self.profile,
            "output_path": str(self.output_path),
            "timeout_seconds": self.timeout_seconds,
            "request_id": self.request_id,
            "extension_path": str(self.extension_path),
            "extension_id": self.extension_id,
            "extension_version": self.extension_version,
            "extension_commit": SINGLEFILE_EXTENSION_COMMIT,
            "browser_executable_path": self.browser_executable_path,
            "browser_args": self.browser_args,
            "save_options": self.save_options,
            "pre_capture_script_path": _resolve_precapture_script_path(),
        }


@dataclass(frozen=True, slots=True)
class SingleFileExtensionCapture:
    html_bytes: bytes
    status: dict[str, Any]
    downloaded_path: str | None
    profile_dir: str
    download_dir: str
    diagnostics: list[CaptureToolDiagnostic] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _DisplaySession:
    env: dict[str, str]
    process: subprocess.Popen[bytes] | None = None

    def close(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


class SingleFileExtensionCaptureError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str,
        status: dict[str, Any] | None = None,
        stdout_tail: str | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.status = status
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail


def get_singlefile_runtime_info() -> dict[str, object]:
    settings = get_settings()
    extension_path: Path | None
    try:
        extension_path = _resolve_extension_path()
        extension_available = True
    except CaptureToolUnavailableError:
        extension_path = None
        extension_available = False

    browser_path: str | None
    try:
        browser_path = _resolve_browser_executable_path()
    except CaptureToolUnavailableError:
        browser_path = None

    return {
        "tool_name": SINGLEFILE_TOOL_NAME,
        "tool_version": settings.singlefile_version,
        "integration_mode": "mv3_extension",
        "profile": SINGLEFILE_PROFILE,
        "extension_path": str(extension_path) if extension_path is not None else None,
        "extension_available": extension_available,
        "extension_id": settings.singlefile_extension_id,
        "extension_commit": SINGLEFILE_EXTENSION_COMMIT,
        "browser_executable_path": browser_path,
    }


def build_singlefile_invocation(
    url: str,
    output_path: Path,
    *,
    settings_file_path: Path | None = None,
) -> SingleFileInvocation:
    del settings_file_path
    normalized_url = validate_public_http_url(url, purpose="SingleFile capture", resolve_dns=False)
    settings = get_settings()
    extension_path = _resolve_extension_path()
    manifest = _read_extension_manifest(extension_path)
    extension_id = _resolve_extension_id()
    browser_args = [
        *SINGLEFILE_BROWSER_ARGS,
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
    ]

    return SingleFileInvocation(
        url=normalized_url,
        output_path=output_path,
        extension_path=extension_path,
        extension_id=extension_id,
        extension_version=_manifest_version(manifest) or settings.singlefile_version,
        browser_executable_path=_resolve_browser_executable_path(),
        request_id=f"infoflow-{uuid4().hex}",
        browser_args=browser_args,
        save_options=dict(SINGLEFILE_EXTENSION_OPTIONS),
        profile=SINGLEFILE_PROFILE,
        timeout_seconds=settings.singlefile_capture_timeout_seconds,
    )


def archive_page_with_singlefile(url: str, output_path: Path | None = None) -> SingleFileArchiveResult:
    normalized_url = _preflight_capture_url(url)

    if output_path is not None:
        return _archive_page_to_path(normalized_url, output_path)

    with tempfile.TemporaryDirectory(prefix="singlefile-archive-") as temp_dir:
        return _archive_page_to_path(normalized_url, Path(temp_dir) / "singlefile.html")


def build_unavailable_singlefile_args(url: str, output_path: Path) -> dict[str, object]:
    settings = get_settings()
    return {
        "url": url.strip(),
        "integration_mode": "mv3_extension",
        "profile": SINGLEFILE_PROFILE,
        "output_path": str(output_path),
        "timeout_seconds": settings.singlefile_capture_timeout_seconds,
        "request_id": None,
        "extension_path": str(settings.singlefile_extension_path),
        "extension_id": settings.singlefile_extension_id,
        "extension_version": settings.singlefile_version,
        "extension_commit": SINGLEFILE_EXTENSION_COMMIT,
        "browser_executable_path": (
            str(settings.singlefile_browser_executable_path) if settings.singlefile_browser_executable_path else None
        ),
        "browser_args": [],
        "save_options": dict(SINGLEFILE_EXTENSION_OPTIONS),
    }


def _archive_page_to_path(url: str, output_path: Path) -> SingleFileArchiveResult:
    try:
        invocation = build_singlefile_invocation(url, output_path)
        capture = _run_playwright_capture(invocation)
        return _build_success_result(invocation, capture)
    except CaptureToolUnavailableError as exc:
        return _failed_result(
            tool_args_json=build_unavailable_singlefile_args(url, output_path),
            exit_code=None,
            stdout_tail=None,
            stderr_tail=None,
            error_summary=str(exc),
            diagnostic_code="singlefile_extension_unavailable",
        )
    except SingleFileExtensionCaptureError as exc:
        tool_args_json = build_unavailable_singlefile_args(url, output_path)
        if exc.status is not None:
            tool_args_json["extension_status"] = exc.status
        return _failed_result(
            tool_args_json=tool_args_json,
            exit_code=None,
            stdout_tail=exc.stdout_tail,
            stderr_tail=exc.stderr_tail,
            error_summary=str(exc),
            diagnostic_code=exc.diagnostic_code,
        )
    except OSError as exc:
        return _failed_result(
            tool_args_json=build_unavailable_singlefile_args(url, output_path),
            exit_code=None,
            stdout_tail=None,
            stderr_tail=None,
            error_summary=str(exc),
            diagnostic_code="singlefile_output_write_error",
        )


def _run_playwright_capture(invocation: SingleFileInvocation) -> SingleFileExtensionCapture:
    if sync_playwright is None:
        raise CaptureToolUnavailableError("Playwright is not installed")

    diagnostics: list[CaptureToolDiagnostic] = []
    display = _start_xvfb_if_needed()
    try:
        with tempfile.TemporaryDirectory(prefix="singlefile-profile-") as profile_dir, tempfile.TemporaryDirectory(
            prefix="singlefile-downloads-"
        ) as download_dir:
            _write_profile_preferences(Path(profile_dir), Path(download_dir))
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    profile_dir,
                    accept_downloads=True,
                    args=invocation.browser_args,
                    downloads_path=download_dir,
                    env=display.env,
                    executable_path=invocation.browser_executable_path,
                    headless=False,
                    locale=CAPTURE_BROWSER_LOCALE,
                    viewport={"width": 1440, "height": 1800},
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    context.set_default_timeout(SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
                    context.set_default_navigation_timeout(SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
                    context.add_init_script(path=_resolve_precapture_script_path())
                    page.goto(invocation.url, wait_until="load", timeout=SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
                    page.bring_to_front()
                    diagnostics.extend(_run_precapture(page))
                    _wait_for_external_bridge(page)
                    status = _start_and_wait_for_capture(page, invocation)
                    html_bytes, downloaded_path = _read_downloaded_html(status, Path(download_dir))
                    return SingleFileExtensionCapture(
                        html_bytes=html_bytes,
                        status=status,
                        downloaded_path=downloaded_path,
                        profile_dir=profile_dir,
                        download_dir=download_dir,
                        diagnostics=diagnostics,
                    )
                finally:
                    context.close()
    except SingleFileExtensionCaptureError:
        raise
    except (PlaywrightError, OSError) as exc:
        raise SingleFileExtensionCaptureError(
            f"SingleFile MV3 extension capture failed: {exc}",
            diagnostic_code="singlefile_extension_runtime_error",
            stderr_tail=_tail(str(exc)),
        ) from exc
    finally:
        display.close()


def _run_precapture(page: Any) -> list[CaptureToolDiagnostic]:
    try:
        page.set_default_timeout(SINGLEFILE_PRECAPTURE_TIMEOUT_MS)
        page.evaluate("window.__infoflowSingleFilePreCapture || Promise.resolve()")
        page.set_default_timeout(SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        page.set_default_timeout(SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
        return [
            CaptureToolDiagnostic(
                code="singlefile_precapture_timeout",
                message="SingleFile pre-capture script did not settle before timeout; continuing capture",
                level="warning",
            )
        ]
    except PlaywrightError as exc:
        page.set_default_timeout(SINGLEFILE_PAGE_LOAD_TIMEOUT_MS)
        return [
            CaptureToolDiagnostic(
                code="singlefile_precapture_failed",
                message=f"SingleFile pre-capture script failed: {exc}",
                level="warning",
            )
        ]
    return [
        CaptureToolDiagnostic(
            code="singlefile_precapture_completed",
            message="SingleFile pre-capture script completed in the active tab",
            level="info",
        )
    ]


def _wait_for_external_bridge(page: Any) -> None:
    try:
        page.wait_for_function(
            "() => window.chrome && chrome.runtime && chrome.runtime.sendMessage",
            timeout=SINGLEFILE_EXTENSION_READY_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise SingleFileExtensionCaptureError(
            "SingleFile MV3 extension external messaging bridge was not available in the active tab",
            diagnostic_code="singlefile_extension_bridge_unavailable",
            stderr_tail=_tail(str(exc)),
        ) from exc


def _start_and_wait_for_capture(page: Any, invocation: SingleFileInvocation) -> dict[str, Any]:
    status = _send_infoflow_message(
        page,
        invocation.extension_id,
        {
            "method": "infoflow.capture.start",
            "requestId": invocation.request_id,
            "options": invocation.save_options,
        },
    )
    if not isinstance(status, dict):
        raise SingleFileExtensionCaptureError(
            "SingleFile MV3 extension did not return a capture status",
            diagnostic_code="singlefile_extension_empty_status",
        )

    deadline = time.monotonic() + invocation.timeout_seconds
    while time.monotonic() < deadline:
        if status.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(SINGLEFILE_DOWNLOAD_POLL_INTERVAL_SECONDS)
        polled_status = _send_infoflow_message(
            page,
            invocation.extension_id,
            {
                "method": "infoflow.capture.status",
                "requestId": invocation.request_id,
            },
        )
        if isinstance(polled_status, dict):
            status = polled_status

    if status.get("status") == "succeeded":
        return status

    if status.get("status") not in {"failed", "cancelled"}:
        _send_infoflow_message(
            page,
            invocation.extension_id,
            {
                "method": "infoflow.capture.cancel",
                "requestId": invocation.request_id,
            },
            raise_on_error=False,
        )
        raise SingleFileExtensionCaptureError(
            f"SingleFile MV3 extension timed out after {invocation.timeout_seconds} seconds",
            diagnostic_code="singlefile_extension_timeout",
            status=status,
            stdout_tail=_tail(json.dumps(status, ensure_ascii=False)),
        )

    error_summary = _status_error_summary(status)
    raise SingleFileExtensionCaptureError(
        f"SingleFile MV3 extension capture {status.get('status')}: {error_summary}",
        diagnostic_code="singlefile_extension_failed",
        status=status,
        stdout_tail=_tail(json.dumps(status, ensure_ascii=False)),
        stderr_tail=_tail(error_summary),
    )


def _send_infoflow_message(
    page: Any,
    extension_id: str,
    payload: dict[str, object],
    *,
    raise_on_error: bool = True,
) -> Any:
    result = page.evaluate(INFOFLOW_SEND_MESSAGE_SCRIPT, [extension_id, payload])
    if isinstance(result, dict) and result.get("error"):
        if raise_on_error:
            raise SingleFileExtensionCaptureError(
                f"SingleFile MV3 extension message failed: {result['error']}",
                diagnostic_code="singlefile_extension_message_failed",
                stderr_tail=str(result["error"]),
            )
        return None
    return result.get("response") if isinstance(result, dict) else None


def _read_downloaded_html(status: dict[str, Any], download_dir: Path) -> tuple[bytes, str | None]:
    path = _resolve_downloaded_path(status, download_dir)
    if path is None:
        raise SingleFileExtensionCaptureError(
            "SingleFile MV3 extension reported success but no downloaded HTML path was found",
            diagnostic_code="singlefile_output_missing",
            status=status,
            stdout_tail=_tail(json.dumps(status, ensure_ascii=False)),
        )
    try:
        html_bytes = path.read_bytes()
    except OSError as exc:
        raise SingleFileExtensionCaptureError(
            f"SingleFile MV3 extension output was not readable: {exc}",
            diagnostic_code="singlefile_output_missing",
            status=status,
            stdout_tail=_tail(json.dumps(status, ensure_ascii=False)),
            stderr_tail=str(exc),
        ) from exc
    return html_bytes, str(path)


def _resolve_downloaded_path(status: dict[str, Any], download_dir: Path) -> Path | None:
    downloads = status.get("downloads")
    if isinstance(downloads, list):
        for download in reversed(downloads):
            if not isinstance(download, dict):
                continue
            resolved_filename = download.get("resolvedFilename")
            if isinstance(resolved_filename, str) and resolved_filename:
                path = Path(resolved_filename)
                if path.exists():
                    return path
    candidates = sorted(
        [path for path in download_dir.glob("*") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _build_success_result(invocation: SingleFileInvocation, capture: SingleFileExtensionCapture) -> SingleFileArchiveResult:
    validation_error = _validate_html_bytes(capture.html_bytes)
    tool_args_json = {
        **invocation.tool_args_json(),
        "downloaded_path": capture.downloaded_path,
        "profile_dir": capture.profile_dir,
        "download_dir": capture.download_dir,
        "extension_status": capture.status,
    }
    if validation_error is not None:
        return _failed_result(
            tool_args_json=tool_args_json,
            exit_code=0,
            stdout_tail=_tail(json.dumps(capture.status, ensure_ascii=False)),
            stderr_tail=None,
            error_summary=validation_error[0],
            diagnostic_code=validation_error[1],
        )

    try:
        invocation.output_path.parent.mkdir(parents=True, exist_ok=True)
        invocation.output_path.write_bytes(capture.html_bytes)
    except OSError as exc:
        return _failed_result(
            tool_args_json=tool_args_json,
            exit_code=0,
            stdout_tail=_tail(json.dumps(capture.status, ensure_ascii=False)),
            stderr_tail=str(exc),
            error_summary=f"SingleFile MV3 extension output could not be written: {exc}",
            diagnostic_code="singlefile_output_write_error",
        )

    diagnostics = [
        *capture.diagnostics,
        CaptureToolDiagnostic(
            code="singlefile_extension_completed",
            message="SingleFile MV3 extension finished successfully",
            level="info",
        ),
    ]
    return SingleFileArchiveResult(
        succeeded=True,
        html_bytes=capture.html_bytes,
        exit_code=0,
        stdout_tail=_tail(json.dumps(capture.status, ensure_ascii=False)),
        stderr_tail=None,
        error_summary=None,
        tool_args_json=tool_args_json,
        diagnostics=diagnostics,
    )


def _validate_html_bytes(html_bytes: bytes) -> tuple[str, str] | None:
    if len(html_bytes) > CAPTURE_RESPONSE_MAX_BYTES:
        return f"SingleFile MV3 extension output exceeds {CAPTURE_RESPONSE_MAX_BYTES} byte limit", "singlefile_output_too_large"
    if not html_bytes:
        return "SingleFile MV3 extension produced an empty output file", "singlefile_output_empty"
    first_chunk = html_bytes[:8192].lower()
    if b"<html" not in first_chunk and b"<!doctype html" not in first_chunk:
        return "SingleFile MV3 extension output did not look like an HTML document", "singlefile_output_not_html"
    return None


def _write_profile_preferences(profile_dir: Path, download_dir: Path) -> None:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "Preferences").write_text(
        json.dumps(
            {
                "download": {
                    "default_directory": str(download_dir),
                    "directory_upgrade": True,
                    "prompt_for_download": False,
                },
                "intl": {"accept_languages": CAPTURE_ACCEPT_LANGUAGE},
                "safebrowsing": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )


def _start_xvfb_if_needed() -> _DisplaySession:
    env = os.environ.copy()
    if platform.system().lower() != "linux" or env.get("DISPLAY"):
        return _DisplaySession(env=env)

    xvfb_path = shutil.which("Xvfb")
    if not xvfb_path:
        raise CaptureToolUnavailableError("Xvfb is required for headful Chromium MV3 extension capture in Linux")

    last_error = ""
    for display_number in range(99, 109):
        display = f":{display_number}"
        process = subprocess.Popen(
            [xvfb_path, display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.25)
        if process.poll() is None:
            env["DISPLAY"] = display
            return _DisplaySession(env=env, process=process)
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        last_error = stderr or f"Xvfb exited with code {process.returncode}"

    raise CaptureToolUnavailableError(f"Could not start Xvfb for SingleFile MV3 extension capture: {last_error}")


def _resolve_extension_path() -> Path:
    extension_path = get_settings().singlefile_extension_path
    if not extension_path.exists():
        raise CaptureToolUnavailableError(f"SingleFile MV3 extension path not found: {extension_path}")
    if not (extension_path / "manifest.json").exists():
        raise CaptureToolUnavailableError(f"SingleFile MV3 extension manifest not found: {extension_path / 'manifest.json'}")
    return extension_path


def _read_extension_manifest(extension_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads((extension_path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureToolUnavailableError(f"SingleFile MV3 extension manifest is unreadable: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _manifest_version(manifest: dict[str, Any]) -> str | None:
    version = manifest.get("version")
    return version.strip() if isinstance(version, str) and version.strip() else None


def _resolve_extension_id() -> str:
    extension_id = get_settings().singlefile_extension_id.strip()
    if not extension_id:
        raise CaptureToolUnavailableError("SingleFile MV3 extension id is empty")
    return extension_id


def _resolve_browser_executable_path() -> str | None:
    browser_path = get_settings().singlefile_browser_executable_path
    if browser_path is None:
        return None
    if not browser_path.exists():
        raise CaptureToolUnavailableError(f"SingleFile browser executable not found: {browser_path}")
    return str(browser_path)


def _resolve_precapture_script_path() -> str:
    return str(Path(__file__).with_name("singlefile_precapture.js"))


def _preflight_capture_url(url: str) -> str:
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
        headers=build_accept_language_headers(),
    ) as client:
        return validate_public_http_redirect_chain(client, url, purpose="SingleFile capture")


def _status_error_summary(status: dict[str, Any]) -> str:
    errors = status.get("errors")
    if isinstance(errors, list) and errors:
        messages = []
        for error in errors:
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                messages.append(error["message"])
            elif isinstance(error, str):
                messages.append(error)
        if messages:
            return "; ".join(messages)
    return json.dumps(status, ensure_ascii=False)


def _failed_result(
    *,
    tool_args_json: dict[str, object],
    exit_code: int | None,
    stdout_tail: str | None,
    stderr_tail: str | None,
    error_summary: str,
    diagnostic_code: str,
) -> SingleFileArchiveResult:
    return SingleFileArchiveResult(
        succeeded=False,
        html_bytes=None,
        exit_code=exit_code,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        error_summary=error_summary,
        tool_args_json=tool_args_json,
        diagnostics=[
            CaptureToolDiagnostic(
                code=diagnostic_code,
                message=error_summary,
                level="error",
            )
        ],
    )


def _tail(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-SINGLEFILE_TAIL_LIMIT:] or None


__all__ = [
    "SINGLEFILE_PROFILE",
    "SINGLEFILE_TOOL_NAME",
    "archive_page_with_singlefile",
    "build_singlefile_invocation",
    "build_unavailable_singlefile_args",
    "get_singlefile_runtime_info",
    "safe_http_get",
    "validate_public_http_url",
    "validate_public_http_redirect_chain",
]
