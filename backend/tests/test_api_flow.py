"""End-to-end API flow: plan → calibrate → collect → enrich → snapshot → export."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest
from conftest import DEMO_BASE, FixtureFetcher, make_settings
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime import Studio

TERMINAL = {"succeeded", "failed", "partial", "cancelled"}
PROMPT = (
    "Збери стоматологічні клініки Києва: назва, місто, адреса, телефон, сайт, "
    "рейтинг, відгуки, email, онлайн-запис і технології сайту"
)


def _client(tmp_path: Path) -> tuple[TestClient, FixtureFetcher]:
    fetcher = FixtureFetcher()
    settings = make_settings(tmp_path)
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=fetcher))
    return TestClient(app), fetcher


def _wait(client: TestClient, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        run = payload["run"]
        if run["status"] in TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish: {client.get(f'/api/runs/{run_id}').json()}")


@pytest.fixture
def client(tmp_path: Path):
    client, fetcher = _client(tmp_path)
    with client as test_client:
        test_client.fetcher = fetcher  # type: ignore[attr-defined]
        yield test_client


def test_full_pipeline(client: TestClient) -> None:
    created = client.post(
        "/api/tasks",
        json={"prompt": PROMPT, "seeds": [f"{DEMO_BASE}/catalog.html"]},
    )
    assert created.status_code == 201, created.text
    task = created.json()["task"]
    task_id = task["id"]
    spec = created.json()["spec"]
    keys = {column["key"] for column in spec["columns"]}
    assert {"name", "city", "phone", "website", "rating"} <= keys
    assert spec["entity"] == "local_business"
    assert any(item["enricher"] == "site_audit" for item in spec["enrichment"])

    # Collection before calibration is refused (cost guard).
    refused = client.post(f"/api/tasks/{task_id}/run", json={})
    assert refused.status_code == 409

    calibration = client.post(f"/api/tasks/{task_id}/calibrate")
    assert calibration.status_code == 200, calibration.text
    calibration_run = _wait(client, calibration.json()["run"]["id"])["run"]
    assert calibration_run["status"] in {"succeeded", "partial"}

    report = client.get(f"/api/tasks/{task_id}/calibration").json()["calibration"]
    assert report is not None
    assert report["records_found"] >= 3
    assert report["strategy"] in {"css", "json_ld"}
    assert report["coverage"]["name"] == 100.0
    assert report["samples"], "calibration must show preview rows"

    collect = client.post(f"/api/tasks/{task_id}/run", json={})
    assert collect.status_code == 200, collect.text
    collect_run = _wait(client, collect.json()["run"]["id"])["run"]
    assert collect_run["status"] == "succeeded", collect_run
    assert collect_run["stats"]["records_new"] == 3

    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["status"] == "done"
    assert detail["dataset"]["dataset"]["row_count"] == 3
    assert detail["dataset"]["quality"]["name"] == 100.0

    records = client.get(f"/api/tasks/{task_id}/records").json()
    assert records["total"] == 3
    alpha = next(
        item for item in records["items"] if item["payload"]["name"].startswith("Альфа")
    )
    assert alpha["payload"]["phone"] == "+380441112233"
    assert alpha["payload"]["website"] == "https://alpha.example"
    assert alpha["provenance"]["name"]["method"] in {"css", "json_ld"}

    filtered = client.get(f"/api/tasks/{task_id}/records", params={"f_city": "Київ"}).json()
    assert filtered["total"] == 2

    # Enrichment (site audit) fills email, tech stack, booking flag.
    enrich = client.post(f"/api/tasks/{task_id}/enrich", json={"column_keys": None})
    assert enrich.status_code == 200, enrich.text
    enrich_run = _wait(client, enrich.json()["run"]["id"])["run"]
    assert enrich_run["status"] == "succeeded", enrich_run
    assert enrich_run["stats"]["enrichments_run"] >= 1

    enriched = client.get(f"/api/tasks/{task_id}/records").json()
    alpha = next(
        item for item in enriched["items"] if item["payload"]["name"].startswith("Альфа")
    )
    assert alpha["payload"]["email"] == "info@alpha.example"
    assert "WordPress" in alpha["payload"]["tech_stack"]
    assert alpha["payload"]["has_online_booking"] is True

    # Re-collecting must preserve enrichment-only columns.
    recollect = client.post(f"/api/tasks/{task_id}/run", json={})
    assert recollect.status_code == 200, recollect.text
    recollect_run = _wait(client, recollect.json()["run"]["id"])["run"]
    assert recollect_run["status"] in {"succeeded", "partial"}
    preserved = client.get(f"/api/tasks/{task_id}/records").json()
    alpha = next(
        item for item in preserved["items"] if item["payload"]["name"].startswith("Альфа")
    )
    assert alpha["payload"]["email"] == "info@alpha.example"
    assert "WordPress" in alpha["payload"]["tech_stack"]

    snapshot = client.post(f"/api/tasks/{task_id}/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    snapshot_run = _wait(client, snapshot.json()["run"]["id"])["run"]
    assert snapshot_run["status"] == "succeeded", snapshot_run

    changes = client.get(f"/api/tasks/{task_id}/changes").json()["items"]
    types = {event["type"] for event in changes}
    assert "new_entity" in types
    assert "field_changed" in types

    export = client.post(
        f"/api/tasks/{task_id}/exports", json={"format": "csv", "mask_pii": True}
    )
    assert export.status_code == 200, export.text
    manifest = export.json()
    assert manifest["rows"] >= 3
    assert manifest["masked_pii"] is True
    download = client.get(manifest["download_url"])
    assert download.status_code == 200
    text = download.content.decode("utf-8-sig")
    assert "i***@alpha.example" in text
    assert "info@alpha.example" not in text
    assert "record_key" in text

    xlsx = client.post(
        f"/api/tasks/{task_id}/exports", json={"format": "xlsx", "mask_pii": False}
    )
    assert xlsx.status_code == 200
    assert client.get(xlsx.json()["download_url"]).status_code == 200

    duplicates = client.get(f"/api/tasks/{task_id}/duplicates").json()
    assert duplicates["scanned"] == 3

    events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
    assert any(event["type"] == "calibration_finished" for event in events)
    assert any(event["type"] == "run_finished" for event in events)


def test_auth_and_host_guard(tmp_path: Path) -> None:
    token = "test-token-0123456789abcdef"
    settings = make_settings(tmp_path, disable_auth=False, api_token=token)
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=FixtureFetcher()))
    with TestClient(app) as client:
        assert client.get("/api/tasks").status_code == 401
        assert client.get("/api/health").status_code == 200
        ok = client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert client.get("/api/tasks?token=" + token).status_code == 200
        blocked = client.get("/api/health", headers={"host": "evil.example"})
        assert blocked.status_code == 403


def test_task_editing_and_column_management(client: TestClient) -> None:
    created = client.post("/api/tasks", json={"prompt": PROMPT, "seeds": [f"{DEMO_BASE}/catalog.html"]})
    task_id = created.json()["task"]["id"]
    spec = created.json()["spec"]

    new_column = {
        "key": "pain_score",
        "title": "Оцінка болю",
        "dtype": "integer",
        "method": "llm",
        "prompt": "Оціни 0-100, наскільки компанія потребує послуг",
        "description": "AI-скоринг",
    }
    added = client.post(f"/api/tasks/{task_id}/columns", json={"field": new_column})
    assert added.status_code == 200, added.text
    assert "pain_score" in {column["key"] for column in added.json()["task"]["spec"]["columns"]}

    duplicate = client.post(f"/api/tasks/{task_id}/columns", json={"field": new_column})
    assert duplicate.status_code == 409

    deleted = client.delete(f"/api/tasks/{task_id}/columns/pain_score")
    assert deleted.status_code == 200
    assert "pain_score" not in {column["key"] for column in deleted.json()["task"]["spec"]["columns"]}

    patched = client.patch(
        f"/api/tasks/{task_id}",
        json={"spec": {**spec, "limits": {**spec["limits"], "max_pages": 7}}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["task"]["spec"]["limits"]["max_pages"] == 7

    sources = client.get("/api/sources").json()["items"]
    domain = "demo.local"
    assert any(item["domain"] == domain for item in sources)
    updated = client.put(
        f"/api/sources/{domain}",
        json={"tos_risk": "low", "license": "test-fixture", "notes": "local fixture", "reviewed": True},
    )
    assert updated.status_code == 200
    assert updated.json()["item"]["tos_risk"] == "low"

    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/exports").status_code == 200
    assert client.post("/api/maintenance/purge-cache?older_than_days=30").status_code == 200


def test_backup_endpoint(client: TestClient) -> None:
    studio = client.app.state.studio  # type: ignore[attr-defined]
    response = client.post("/api/maintenance/backup?include_cache=false")
    assert response.status_code == 200, response.text
    payload = response.json()
    archive = Path(payload["path"])
    try:
        assert archive.exists() and archive.stat().st_size > 0
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
        assert {"operational.sqlite", "analytics.duckdb", "backup-manifest.json"} <= names
    finally:
        archive.unlink(missing_ok=True)
        assert studio.paths.root.exists()
