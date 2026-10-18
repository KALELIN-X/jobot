"""Queue integration with real approval/storage and no browser or network."""
import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app


HEADERS = {"X-JobRadar-Client": "dashboard"}
PROFILE = {"name": "Alex Example", "email": "alex@example.test", "location": "Boston, MA",
           "work_authorized": True, "needs_sponsorship": False, "experience_months": 12,
           "graduation_date": "2027-05-15"}
RULES = {"companies": ["Example"], "company_boards": {"Example": "example"},
         "daily_limit": 3, "min_score": 60, "require_known_eligibility": True}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def configure_and_approve(client):
    assert client.post("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json=RULES).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("resume.txt", b"Built Python backend services and REST APIs.", "text/plain")}).status_code == 200
    reviewed = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    approved = client.post("/api/approve", headers=HEADERS,
                           json={"acknowledge": True, "expected_fingerprint": reviewed})
    assert approved.status_code == 200, approved.text


def add_job(client, identifier, score=95):
    client.app.state.store.save_job({
        "id": identifier, "company": "Example", "title": "Python Backend Engineer",
        "location": "Boston, MA", "work_model": "On Site", "ats": "lever",
        "apply_url": f"https://jobs.lever.co/example/{identifier}/apply",
        "description": "Build and maintain reliable Python backend services and REST APIs for customers.",
        "closed": False, "score": score,
        "score_detail": {"eligibility": [{"label": "Verified test condition", "status": "pass", "detail": "Synthetic test only"}]},
        "status": "queued",
    })


def await_idle(client, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if not state["automation"]["running"]:
            return state
        time.sleep(0.01)
    pytest.fail("Queue did not finish within the bounded test deadline")


def install_paused_runner(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    filled, refused = [], []

    async def fake_application(job, profile, resume_path, **options):
        filled.append(job["id"])
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            return {"status": "failed", "message": "Synthetic runner release timed out"}
        try:
            await options["before_submit"]()
        except ValueError as error:
            refused.append(str(error))
            return {"status": "needs_attention", "message": str(error)}
        return {"status": "submitted", "message": "Synthetic confirmation observed", "confirmation": "Synthetic confirmation #42"}

    monkeypatch.setattr("jobradar.autofill.run_application", fake_application)
    return entered, release, filled, refused


def test_dismiss_later_job_during_active_fill_prevents_its_fill(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "first", 99)
    add_job(client, "later", 90)
    entered, release, filled, refused = install_paused_runner(monkeypatch)
    try:
        assert client.post("/api/run", headers=HEADERS).status_code == 200
        assert entered.wait(3), "First application did not enter the synthetic runner"
        response = client.post("/api/jobs/later/status", headers=HEADERS, json={"status": "dismissed"})
        assert response.status_code == 200
    finally:
        release.set()
    await_idle(client)
    assert filled == ["first"]
    assert not refused
    assert client.app.state.store.job("later")["status"] == "dismissed"
    assert client.app.state.store.job("later")["attempted_at"] is None


@pytest.mark.parametrize("change", ["profile", "rules"])
def test_configuration_change_while_filling_refuses_before_submit(client, monkeypatch, change):
    configure_and_approve(client)
    add_job(client, "changing")
    entered, release, filled, refused = install_paused_runner(monkeypatch)
    try:
        assert client.post("/api/run", headers=HEADERS).status_code == 200
        assert entered.wait(3), "Application did not enter the synthetic runner"
        body = {**PROFILE, "email": "changed@example.test"} if change == "profile" else {**RULES, "daily_limit": 2}
        response = client.post(f"/api/{change}", headers=HEADERS, json=body)
        assert response.status_code == 200
    finally:
        release.set()
    state = await_idle(client)
    saved = client.app.state.store.job("changing")
    assert filled == ["changing"]
    assert len(refused) == 1
    assert "stopped" in refused[0].lower() or "configuration changed" in refused[0].lower()
    assert saved["status"] == "needs_attention"
    assert saved["attempted_at"] is None
    assert not state["approval"]["approved"]
    with client.app.state.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_approved_success_reserves_attempt_before_recording_submitted(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "success")
    observed = {}

    async def fake_application(job, profile, resume_path, **options):
        assert options["submit"] is True
        assert resume_path.is_file()
        assert client.app.state.store.job(job["id"])["status"] == "filling"
        await options["before_submit"]()
        saved = client.app.state.store.job(job["id"])
        observed.update(status=saved["status"], attempted_at=saved["attempted_at"])
        with client.app.state.store.connection() as connection:
            observed["attempts"] = [dict(row) for row in connection.execute("SELECT * FROM attempts")]
        return {"status": "submitted", "message": "Synthetic receipt observed", "confirmation": "Synthetic receipt #42"}

    monkeypatch.setattr("jobradar.autofill.run_application", fake_application)
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = await_idle(client)
    saved = client.app.state.store.job("success")
    assert observed["status"] == "submitting"
    assert observed["attempted_at"]
    assert len(observed["attempts"]) == 1
    assert observed["attempts"][0]["job_id"] == "success"
    assert observed["attempts"][0]["day"] == datetime.now(timezone.utc).date().isoformat()
    assert saved["status"] == "submitted"
    assert saved["confirmation"] == "Synthetic receipt #42"
    assert saved["attempted_at"] and saved["applied_at"]
    assert state["stats"]["submitted"] == 1
    assert any(event["type"] == "submitted" and event["job_id"] == "success" for event in state["events"])


def test_second_immediate_run_and_demo_are_rejected_while_queue_active(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "active")
    entered, release, filled, refused = install_paused_runner(monkeypatch)
    try:
        assert client.post("/api/run", headers=HEADERS).status_code == 200
        # The endpoint must claim running state before its background task starts.
        assert client.post("/api/run", headers=HEADERS).status_code == 409
        assert client.post("/api/demo", headers=HEADERS).status_code == 409
        assert entered.wait(3), "Application did not enter the synthetic runner"
        assert client.get("/api/state").json()["automation"]["running"] is True
    finally:
        release.set()
    await_idle(client)
    assert filled == ["active"]
    assert not refused
    assert not any(job.get("demo") for job in client.app.state.store.jobs())
