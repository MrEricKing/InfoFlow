from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import reset_settings_cache
from app.main import create_app


def test_production_static_files_serve_spa_without_hijacking_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    static_root = tmp_path / "frontend-dist"
    assets_dir = static_root / "assets"
    assets_dir.mkdir(parents=True)
    (static_root / "index.html").write_text("<!doctype html><div id=\"root\"></div>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")

    monkeypatch.setenv("STATIC_FILES_DIR", str(static_root))
    reset_settings_cache()

    client = TestClient(create_app())

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/").text.startswith("<!doctype html>")
    assert client.get("/reader/deep/link").text.startswith("<!doctype html>")
    assert client.get("/assets/app.js").text == "console.log('ok')"

    reset_settings_cache()
