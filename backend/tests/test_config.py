import json
import tomllib

from pydantic import ValidationError
import pytest

from app.core.config import BACKEND_DIR, PROJECT_ROOT, Settings, get_app_version, get_settings, reset_settings_cache
from app.main import create_app


def test_app_version_matches_backend_project_metadata() -> None:
    payload = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))

    assert get_app_version() == payload["project"]["version"]


def test_runtime_requirements_match_backend_project_dependencies() -> None:
    payload = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    expected = payload["project"]["dependencies"]
    runtime_requirements_path = BACKEND_DIR / "requirements-runtime.txt"
    actual = [
        line.strip()
        for line in runtime_requirements_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert actual == expected


def test_root_backend_test_scripts_default_to_backend_cwd_and_xdist() -> None:
    payload = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = payload["scripts"]

    for script_name in ("test:backend", "test:backend:parallel", "test:backend:config"):
        script = scripts[script_name]
        assert "node ./scripts/python.mjs --cwd backend" in script
        assert "-m pytest" in script
        assert "-n 4 --dist worksteal" in script

    postgres_script = scripts["test:backend:postgres"]
    assert "node ./scripts/python.mjs --cwd backend" in postgres_script
    assert "tests/test_postgres_integration.py" in postgres_script
    assert "-m postgres" in postgres_script
    assert "-n 6 --dist worksteal" in postgres_script
    assert "--dist worksteal" not in scripts["test:backend:serial"]
    assert "--dist worksteal" not in scripts["test:backend:postgres:serial"]


def test_cors_origins_accept_comma_separated_env(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173, http://127.0.0.1:5173")
    reset_settings_cache()

    try:
        assert get_settings().cors_origins == [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    finally:
        reset_settings_cache()


def test_production_cors_rejects_wildcard_origin(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://infoflow.example.com,*")
    monkeypatch.setenv("INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API", "true")
    reset_settings_cache()

    try:
        with pytest.raises(ValidationError, match="CORS_ORIGINS must not include '\\*'"):
            get_settings()
    finally:
        reset_settings_cache()


def test_production_api_requires_explicit_external_access_control_acknowledgement(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://infoflow.example.com")
    monkeypatch.delenv("INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API", raising=False)
    reset_settings_cache()

    try:
        with pytest.raises(ValidationError, match="INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API=true"):
            get_settings()
    finally:
        reset_settings_cache()


def test_production_api_allows_explicit_external_access_control_acknowledgement(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://infoflow.example.com")
    monkeypatch.setenv("INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API", "true")
    reset_settings_cache()

    try:
        assert get_settings().allow_unauthenticated_production_api is True
    finally:
        reset_settings_cache()


def test_production_api_disables_openapi_and_docs(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("CORS_ORIGINS", "https://infoflow.example.com")
    monkeypatch.setenv("INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API", "true")
    reset_settings_cache()

    try:
        app = create_app()
        assert app.openapi_url is None
        route_paths = {getattr(route, "path", None) for route in app.routes}
        assert not ({"/api/openapi.json", "/api/docs", "/api/redoc"} & route_paths)
    finally:
        reset_settings_cache()


def test_development_cors_allows_wildcard_origin(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("CORS_ORIGINS", "*")
    reset_settings_cache()

    try:
        assert get_settings().cors_origins == ["*"]
    finally:
        reset_settings_cache()


def test_reset_settings_cache_applies_environment_changes(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "InfoFlow first")
    reset_settings_cache()

    try:
        assert get_settings().app_name == "InfoFlow first"
        monkeypatch.setenv("APP_NAME", "InfoFlow second")
        assert get_settings().app_name == "InfoFlow first"
        reset_settings_cache()
        assert get_settings().app_name == "InfoFlow second"
    finally:
        reset_settings_cache()


def test_ai_key_overrides_load_from_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "INFOFLOW_AI_GENERAL_API_KEY=sk-general-from-env-file",
                "INFOFLOW_AI_SENSITIVE_API_KEY=sk-sensitive-from-env-file",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.ai_general_api_key == "sk-general-from-env-file"
    assert settings.ai_sensitive_api_key == "sk-sensitive-from-env-file"
