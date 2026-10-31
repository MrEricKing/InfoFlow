from __future__ import annotations

import json
import tomllib
from pathlib import Path


def test_project_version_metadata_is_aligned() -> None:
    root_dir = Path(__file__).resolve().parents[2]

    root_package = json.loads((root_dir / "package.json").read_text())
    frontend_package = json.loads((root_dir / "frontend" / "package.json").read_text())
    frontend_lock = json.loads((root_dir / "frontend" / "package-lock.json").read_text())
    backend_project = tomllib.loads((root_dir / "backend" / "pyproject.toml").read_text())["project"]

    expected_version = root_package["version"]

    assert frontend_package["version"] == expected_version
    assert frontend_lock["version"] == expected_version
    assert frontend_lock["packages"][""]["version"] == expected_version
    assert backend_project["version"] == expected_version
