from __future__ import annotations

import os
import subprocess
from typing import Any

from app.core.config import PROJECT_ROOT

GIT_COMMAND_TIMEOUT_SECONDS = 2


def get_runtime_version_info(app_version: str) -> dict[str, Any]:
    """Return local runtime version details without requiring release metadata changes."""
    git_commit = _git_stdout("rev-parse", "HEAD")
    git_branch = _git_stdout("branch", "--show-current")
    git_status = _git_stdout("status", "--porcelain=v1", "--untracked-files=all")

    source = "git" if git_commit else "environment" if _environment_commit() else "unavailable"
    if git_commit is None:
        git_commit = _environment_commit()
    git_commit_short = git_commit[:7] if git_commit else None

    git_dirty: bool | None
    dirty_file_count: int | None
    if git_status is None:
        git_dirty = _environment_dirty()
        dirty_file_count = None
    else:
        dirty_lines = [line for line in git_status.splitlines() if line.strip()]
        git_dirty = bool(dirty_lines)
        dirty_file_count = len(dirty_lines)

    commit_display = git_commit_short
    if commit_display and git_dirty:
        commit_display = f"{commit_display}-dirty"

    return {
        "version": app_version,
        "git_commit": git_commit,
        "git_commit_short": git_commit_short,
        "git_commit_display": commit_display,
        "git_branch": git_branch or None,
        "git_dirty": git_dirty,
        "git_dirty_file_count": dirty_file_count,
        "version_source": source,
    }


def _git_stdout(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _environment_commit() -> str | None:
    for key in ("INFOFLOW_GIT_COMMIT", "GITHUB_SHA", "COMMIT_SHA"):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return None


def _environment_dirty() -> bool | None:
    value = os.getenv("INFOFLOW_GIT_DIRTY")
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "dirty"}
