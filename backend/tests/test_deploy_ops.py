"""Offline tests for VPS deployment kit, backup script, and proxy host guard (006)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from conftest import make_settings
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_docker_and_compose_configs_are_valid() -> None:
    dockerfile = ROOT / "Dockerfile"
    dockerignore = ROOT / ".dockerignore"
    compose = ROOT / "docker-compose.prod.yml"

    assert dockerfile.is_file(), "Dockerfile is missing"
    assert dockerignore.is_file(), ".dockerignore is missing"
    assert compose.is_file(), "docker-compose.prod.yml is missing"

    df_text = dockerfile.read_text(encoding="utf-8")
    assert "npm run build" in df_text
    assert "uv sync" in df_text
    assert "daas" in df_text and "serve" in df_text
    assert "HEALTHCHECK" in df_text and "/api/health" in df_text

    di_text = dockerignore.read_text(encoding="utf-8")
    for pattern in (".git", "storage", "node_modules", ".venv", ".env"):
        assert pattern in di_text

    cp_text = compose.read_text(encoding="utf-8")
    assert "127.0.0.1:" in cp_text
    assert '"8000:8000"' not in cp_text
    assert "./storage:/app/storage" in cp_text


def test_deploy_templates_and_backup_script() -> None:
    systemd_unit = ROOT / "deploy" / "systemd" / "daas-studio.service"
    caddyfile = ROOT / "deploy" / "caddy" / "Caddyfile"
    backup_script = ROOT / "deploy" / "scripts" / "backup-and-upload.sh"
    deploy_readme = ROOT / "deploy" / "README.md"

    for path in (systemd_unit, caddyfile, backup_script, deploy_readme):
        assert path.is_file(), f"Deploy artifact missing: {path}"

    script_text = backup_script.read_text(encoding="utf-8")
    assert "/api/health" in script_text
    assert "/api/maintenance/backup" in script_text
    assert "runtime.json" in script_text
    assert "daas backup" in script_text
    assert "KEEP_DAYS" in script_text
    assert "BACKUP_REMOTE" in script_text

    bash = shutil.which("bash")
    assert bash is not None
    syntax = subprocess.run([bash, "-n", str(backup_script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    unit_text = systemd_unit.read_text(encoding="utf-8")
    assert "ExecStart=" in unit_text
    assert "Restart=" in unit_text

    caddy_text = caddyfile.read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in caddy_text


def test_host_guard_allows_cors_domain_behind_proxy(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        host="0.0.0.0",
        cors_origins="https://studio.example.com",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        ok_proxy = client.get("/api/health", headers={"Host": "studio.example.com"})
        assert ok_proxy.status_code == 200
        assert ok_proxy.json()["status"] == "ok"

        ok_local = client.get("/api/health", headers={"Host": "127.0.0.1:8000"})
        assert ok_local.status_code == 200

        blocked = client.get("/api/health", headers={"Host": "evil.example"})
        assert blocked.status_code == 403

        token = app.state.studio.token
        backup = client.post(
            "/api/maintenance/backup",
            headers={"Host": "studio.example.com", "Authorization": f"Bearer {token}"},
        )
        assert backup.status_code == 200
        payload = backup.json()
        assert Path(payload["path"]).is_file()
        assert payload["bytes"] > 0
