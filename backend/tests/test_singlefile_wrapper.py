from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import reset_settings_cache
from app.core.safety import CAPTURE_RESPONSE_MAX_BYTES
from app.services.capture_tools import singlefile_wrapper
from app.services.capture_tools.models import CaptureToolDiagnostic


@pytest.fixture(autouse=True)
def _bypass_network_preflight(monkeypatch) -> None:
    monkeypatch.setattr(
        singlefile_wrapper,
        "_preflight_capture_url",
        lambda url: singlefile_wrapper.validate_public_http_url(
            url,
            purpose="SingleFile capture",
            resolve_dns=False,
        ),
    )


def test_singlefile_wrapper_builds_mv3_extension_invocation(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    browser_path = tmp_path / "chromium"
    browser_path.write_text("#!/bin/sh", encoding="utf-8")
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    monkeypatch.setenv("SINGLEFILE_EXTENSION_ID", "abcdefghijklmnopabcdefghijklmnop")
    monkeypatch.setenv("SINGLEFILE_BROWSER_EXECUTABLE_PATH", str(browser_path))
    monkeypatch.setenv("SINGLEFILE_CAPTURE_TIMEOUT_SECONDS", "180")
    reset_settings_cache()

    invocation = singlefile_wrapper.build_singlefile_invocation(
        "https://1.1.1.1/path",
        tmp_path / "singlefile.html",
    )

    assert invocation.url == "https://1.1.1.1/path"
    assert invocation.extension_path == extension_path
    assert invocation.extension_id == "abcdefghijklmnopabcdefghijklmnop"
    assert invocation.extension_version == "9.9.9"
    assert invocation.browser_executable_path == str(browser_path)
    assert invocation.timeout_seconds == 180
    assert f"--disable-extensions-except={extension_path}" in invocation.browser_args
    assert f"--load-extension={extension_path}" in invocation.browser_args
    assert invocation.save_options["backgroundSave"] is True
    assert invocation.save_options["filenameConflictAction"] == "overwrite"
    assert invocation.tool_args_json()["integration_mode"] == "mv3_extension"
    assert "argv" not in invocation.tool_args_json()

    reset_settings_cache()


def test_singlefile_wrapper_returns_success_from_extension_capture(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    reset_settings_cache()
    output_path = tmp_path / "singlefile.html"

    def fake_capture(invocation):
        return singlefile_wrapper.SingleFileExtensionCapture(
            html_bytes=b"<!doctype html><html><title>Archived</title><body>ok</body></html>",
            status={
                "requestId": invocation.request_id,
                "status": "succeeded",
                "downloads": [{"resolvedFilename": "/tmp/downloaded.html", "status": "complete"}],
            },
            downloaded_path="/tmp/downloaded.html",
            profile_dir="/tmp/profile",
            download_dir="/tmp/downloads",
            diagnostics=[
                CaptureToolDiagnostic(
                    code="singlefile_precapture_completed",
                    message="pre-capture done",
                    level="info",
                )
            ],
        )

    monkeypatch.setattr(singlefile_wrapper, "_run_playwright_capture", fake_capture)

    result = singlefile_wrapper.archive_page_with_singlefile(" https://1.1.1.1/article ", output_path=output_path)

    assert result.succeeded is True
    assert result.html_bytes == b"<!doctype html><html><title>Archived</title><body>ok</body></html>"
    assert result.exit_code == 0
    assert result.tool_args_json["profile"] == singlefile_wrapper.SINGLEFILE_PROFILE
    assert result.tool_args_json["integration_mode"] == "mv3_extension"
    assert result.tool_args_json["extension_status"]["status"] == "succeeded"
    assert result.diagnostics[-1].code == "singlefile_extension_completed"
    assert output_path.read_bytes() == result.html_bytes

    reset_settings_cache()


def test_singlefile_wrapper_returns_failure_for_extension_runtime_error(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    reset_settings_cache()

    def fake_capture(_invocation):
        raise singlefile_wrapper.SingleFileExtensionCaptureError(
            "bridge unavailable",
            diagnostic_code="singlefile_extension_bridge_unavailable",
            status={"status": "failed"},
            stderr_tail="no chrome.runtime",
        )

    monkeypatch.setattr(singlefile_wrapper, "_run_playwright_capture", fake_capture)

    result = singlefile_wrapper.archive_page_with_singlefile(
        "https://1.1.1.1/fail",
        output_path=tmp_path / "singlefile.html",
    )

    assert result.succeeded is False
    assert result.exit_code is None
    assert result.error_summary == "bridge unavailable"
    assert result.stderr_tail == "no chrome.runtime"
    assert result.tool_args_json["extension_status"] == {"status": "failed"}
    assert result.diagnostics[0].code == "singlefile_extension_bridge_unavailable"

    reset_settings_cache()


def test_singlefile_wrapper_returns_failure_when_extension_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(tmp_path / "missing-extension"))
    reset_settings_cache()

    result = singlefile_wrapper.archive_page_with_singlefile(
        "https://1.1.1.1/missing-runtime",
        output_path=tmp_path / "singlefile.html",
    )

    assert result.succeeded is False
    assert result.diagnostics[0].code == "singlefile_extension_unavailable"
    assert "extension path not found" in (result.error_summary or "")

    reset_settings_cache()


def test_singlefile_wrapper_rejects_oversized_output_before_writing(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    reset_settings_cache()
    output_path = tmp_path / "singlefile.html"

    def fake_capture(_invocation):
        return singlefile_wrapper.SingleFileExtensionCapture(
            html_bytes=b"<!doctype html><html>" + (b"x" * CAPTURE_RESPONSE_MAX_BYTES),
            status={"status": "succeeded", "downloads": [{"status": "complete"}]},
            downloaded_path="/tmp/huge.html",
            profile_dir="/tmp/profile",
            download_dir="/tmp/downloads",
        )

    monkeypatch.setattr(singlefile_wrapper, "_run_playwright_capture", fake_capture)

    result = singlefile_wrapper.archive_page_with_singlefile("https://1.1.1.1/huge", output_path=output_path)

    assert result.succeeded is False
    assert result.diagnostics[0].code == "singlefile_output_too_large"
    assert not output_path.exists()

    reset_settings_cache()


def test_singlefile_wrapper_rejects_non_html_output_before_writing(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    reset_settings_cache()
    output_path = tmp_path / "singlefile.html"

    def fake_capture(_invocation):
        return singlefile_wrapper.SingleFileExtensionCapture(
            html_bytes=b"not html",
            status={"status": "succeeded", "downloads": [{"status": "complete"}]},
            downloaded_path="/tmp/not-html",
            profile_dir="/tmp/profile",
            download_dir="/tmp/downloads",
        )

    monkeypatch.setattr(singlefile_wrapper, "_run_playwright_capture", fake_capture)

    result = singlefile_wrapper.archive_page_with_singlefile("https://1.1.1.1/not-html", output_path=output_path)

    assert result.succeeded is False
    assert result.diagnostics[0].code == "singlefile_output_not_html"
    assert not output_path.exists()

    reset_settings_cache()


def test_singlefile_wrapper_rejects_private_url_before_invocation(tmp_path) -> None:
    with pytest.raises(ValueError, match="non-public"):
        singlefile_wrapper.build_singlefile_invocation(
            "http://127.0.0.1/internal",
            tmp_path / "singlefile.html",
        )


def test_build_unavailable_singlefile_args_uses_extension_runtime(tmp_path, monkeypatch) -> None:
    extension_path = _make_extension(tmp_path)
    monkeypatch.setenv("SINGLEFILE_EXTENSION_PATH", str(extension_path))
    monkeypatch.setenv("SINGLEFILE_EXTENSION_ID", "abcdefghijklmnopabcdefghijklmnop")
    reset_settings_cache()

    args = singlefile_wrapper.build_unavailable_singlefile_args(" https://1.1.1.1 ", tmp_path / "singlefile.html")

    assert args["integration_mode"] == "mv3_extension"
    assert args["extension_path"] == str(extension_path)
    assert args["extension_id"] == "abcdefghijklmnopabcdefghijklmnop"
    assert args["browser_args"] == []
    assert "argv" not in args

    reset_settings_cache()


def _make_extension(tmp_path: Path) -> Path:
    extension_path = tmp_path / "singlefile-mv3"
    extension_path.mkdir()
    (extension_path / "manifest.json").write_text(
        json.dumps(
            {
                "name": "SingleFile InfoFlow Runtime",
                "version": "9.9.9",
                "manifest_version": 3,
            }
        ),
        encoding="utf-8",
    )
    return extension_path
