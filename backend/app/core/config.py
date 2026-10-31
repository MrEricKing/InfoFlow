from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
PACKAGE_NAME = "infoflowaggregator-backend"


def get_app_version() -> str:
    source_version = _get_source_project_version()
    if source_version is not None:
        return source_version
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "3.0.0"


def _get_source_project_version() -> str | None:
    pyproject_path = BACKEND_DIR / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = payload.get("project")
    if not isinstance(project, dict):
        return None
    source_version = project.get("version")
    if isinstance(source_version, str) and source_version.strip():
        return source_version.strip()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "InfoFlowAggregator API"
    app_version: str = get_app_version()
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:55432/infoflowaggregator"
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    rsshub_base_url: str = "http://localhost:1200"
    knowledge_base_root: Path = PROJECT_ROOT / "KnowledgeBase"
    crawl4ai_version: str = "0.8.6"
    singlefile_version: str = "1.22.98"
    singlefile_extension_path: Path = PROJECT_ROOT / "vendor" / "singlefile-mv3"
    singlefile_extension_id: str = "jkeljiefjnokjkioahopjglagkbocpbh"
    singlefile_browser_executable_path: Path | None = None
    singlefile_capture_timeout_seconds: int = 120
    static_files_dir: Path | None = None
    allow_unauthenticated_production_api: bool = Field(
        default=False,
        validation_alias="INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API",
    )
    settings_encryption_key: str | None = Field(default=None, validation_alias="INFOFLOW_SETTINGS_ENCRYPTION_KEY")
    ai_general_api_key: str | None = Field(default=None, validation_alias="INFOFLOW_AI_GENERAL_API_KEY")
    ai_sensitive_api_key: str | None = Field(default=None, validation_alias="INFOFLOW_AI_SENSITIVE_API_KEY")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_cors(self) -> "Settings":
        if self.environment.strip().lower() == "production" and "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS must not include '*' when ENVIRONMENT=production")
        if self.environment.strip().lower() == "production" and not self.allow_unauthenticated_production_api:
            raise ValueError(
                "Production API startup requires INFOFLOW_ALLOW_UNAUTHENTICATED_PRODUCTION_API=true "
                "until backend authentication is implemented; deploy behind a trusted access-control boundary."
            )
        return self

    @field_validator(
        "knowledge_base_root",
        "singlefile_extension_path",
        "singlefile_browser_executable_path",
        "static_files_dir",
        mode="before",
    )
    @classmethod
    def parse_paths(cls, value: object) -> object:
        if value in (None, ""):
            return None if value in (None, "") else value
        if isinstance(value, str):
            return Path(value)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
