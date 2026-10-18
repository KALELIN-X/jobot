"""Queue preflight uses isolated storage and never opens an employer browser."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.ingest import SOURCE_URL, destination_provenance


HEADERS = {"X-JobRadar-Client": "dashboard"}
RULES = {"companies": ["Example"], "company_boards": {"Example": "example"}, "daily_limit": 1}
POSTING = "12345678-abcd-1234-abcd-123456789abc"


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def unexpected_browser(*args, **kwargs):
        pytest.fail("A blocked preflight must never open an application browser")

    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_browser)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def configure(client, approve=True):
    assert client.post("/api/profile", headers=HEADERS,
                       json={"name": "Alex Example", "email": "alex@example.test"}).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json=RULES).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("resume.txt", b"Built Python services.", "text/plain")}).status_code == 200
    if approve:
        fingerprint = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
        assert client.post("/api/approve", headers=HEADERS,
                           json={"acknowledge": True, "expected_fingerprint": fingerprint}).status_code == 200


def add_job(client, identifier="ready", **changes):
    job = {
        "id": identifier, "company": "Example", "title": "Software Engineer",
        "location": "Boston, MA", "ats": "lever", "apply_url": f"https://jobs.lever.co/example/{POSTING}/apply",
        "description": "Build Python backend services.", "score": 90,
        "score_detail": {"eligibility": []}, "status": "queued", **changes,
    }
    job.setdefault("source", "Jobright")
    job.setdefault("source_url", SOURCE_URL)
    job.setdefault("detail_url", "https://jobright.ai/jobs/info/synthetic-role-1234")
    job.setdefault("destination_provenance", destination_provenance(job["apply_url"], "jobright_detail", job["detail_url"]))
    client.app.state.store.save_job(job)


def test_pending_roles_are_not_misrepresented_as_submission_ready(client):
    configure(client, approve=False)
    add_job(client)
    add_job(client, "unsupported", ats="workday", apply_url="https://example.test/apply")
    add_job(client, "low-fit", score=30)
    add_job(client, "demo", demo=True)
    add_job(client, "dismissed", status="dismissed")

    state = client.get("/api/state").json()
    queue = state["queue"]
    assert queue["pending"] == 3
    assert queue["ready"] == 1
    assert queue["blocked"] == 2
    assert not queue["can_run"]
    assert "approve" in " ".join(queue["blockers"])
    by_id = {job["id"]: job for job in state["jobs"]}
    assert by_id["ready"]["submission_check"] == {"eligible": True, "blockers": []}
    assert "Lever" in " ".join(by_id["unsupported"]["submission_check"]["blockers"])
    assert "minimum" in " ".join(by_id["low-fit"]["submission_check"]["blockers"])
    assert "submission_check" not in by_id["demo"]
    assert not state["automation"]["enabled"]
    # Polling must not mutate persisted listings or attempt history.
    assert "submission_check" not in client.app.state.store.job("ready")
    assert client.app.state.store.attempt_count(datetime.now(timezone.utc).date().isoformat()) == 0


def test_approved_but_empty_queue_returns_actionable_error_instead_of_started(client):
    configure(client)
    response = client.post("/api/run", headers=HEADERS)
    assert response.status_code == 409
    assert "Fetch jobs" in response.json()["detail"]
    assert not client.get("/api/state").json()["automation"]["running"]


def test_no_eligible_roles_are_held_without_starting_a_fake_run(client):
    configure(client)
    add_job(client, ats="greenhouse", apply_url="https://example.test/apply")
    response = client.post("/api/run", headers=HEADERS)
    assert response.status_code == 409
    assert "No queued roles currently pass" in response.json()["detail"]
    assert client.app.state.store.job("ready")["status"] == "queued"


def test_ready_snapshot_clearly_distinguishes_one_off_run_and_schedule(client):
    configure(client)
    add_job(client)
    state = client.get("/api/state").json()
    assert state["queue"]["can_run"] is True
    assert state["queue"]["blockers"] == []
    assert state["queue"]["remaining_today"] == 1
    assert not state["automation"]["enabled"]


def test_resume_integrity_blocks_run_and_schedule_before_browser_launch(client):
    configure(client)
    add_job(client)
    store = client.app.state.store
    resume = store.get("resume")
    (client.app.state.data_dir / "resumes" / resume["stored_name"]).write_bytes(b"Changed outside the app")
    state = client.get("/api/state").json()
    assert not state["queue"]["can_run"]
    assert any("missing or changed" in problem for problem in state["setup_problems"])
    for endpoint, payload in [("run", None), ("automation", {"enabled": True})]:
        response = client.post(f"/api/{endpoint}", headers=HEADERS, json=payload)
        assert response.status_code == 409
        assert "missing or changed" in response.json()["detail"]
    assert not client.get("/api/state").json()["automation"]["enabled"]


def test_used_quota_is_visible_and_prevents_start(client):
    configure(client)
    add_job(client, "already-attempted", status="filling")
    store = client.app.state.store
    day = datetime.now(timezone.utc).date().isoformat()
    store.reserve_attempt("already-attempted", day, 1)
    store.update_job("already-attempted", "submitted")
    add_job(client, apply_url="https://jobs.lever.co/example/87654321-abcd-1234-abcd-123456789abc/apply")
    queue = client.get("/api/state").json()["queue"]
    assert queue["attempts_today"] == 1
    assert queue["remaining_today"] == 0
    assert queue["ready"] == 1
    assert not queue["can_run"]
    response = client.post("/api/run", headers=HEADERS)
    assert response.status_code == 409
    assert "limit reached" in response.json()["detail"]
    assert store.attempt_count(day) == 1


def test_prior_destination_attempt_is_a_submission_blocker(client):
    configure(client)
    add_job(client, "previous", status="filling")
    store = client.app.state.store
    store.reserve_attempt("previous", "2020-01-01", 1)
    store.update_job("previous", "submitted")
    add_job(client, "duplicate-feed-row")
    state = client.get("/api/state").json()
    assert state["queue"]["ready"] == 0
    duplicate = next(job for job in state["jobs"] if job["id"] == "duplicate-feed-row")
    assert "already recorded as applied" in " ".join(duplicate["submission_check"]["blockers"])
    assert client.post("/api/run", headers=HEADERS).status_code == 409


def test_explicitly_enabled_schedule_can_wait_for_future_jobs(client):
    configure(client)
    assert not client.get("/api/state").json()["queue"]["can_run"]
    response = client.post("/api/automation", headers=HEADERS, json={"enabled": True})
    assert response.status_code == 200
    assert client.get("/api/state").json()["automation"]["enabled"] is True


def test_unchecked_lever_preference_saves_but_cannot_approve_or_run(client):
    configure(client)
    add_job(client)
    response = client.post("/api/rules", headers=HEADERS, json={**RULES, "allowed_ats": []})
    assert response.status_code == 200
    state = client.get("/api/state").json()
    assert state["rules"]["allowed_ats"] == []
    assert not state["approval"]["approved"]
    assert state["queue"]["ready"] == 0
    assert any("Enable Lever" in problem for problem in state["setup_problems"])
    response = client.post("/api/approve", headers=HEADERS, json={
        "acknowledge": True, "expected_fingerprint": state["approval"]["candidate_fingerprint"]})
    assert response.status_code == 400
    assert "Enable Lever" in response.json()["detail"]
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 409


@pytest.mark.parametrize("ats", [["ashby"], ["lever", "workday"], ["lever", "lever"]])
def test_rules_still_reject_unsupported_application_systems(client, ats):
    response = client.post("/api/rules", headers=HEADERS, json={**RULES, "allowed_ats": ats})
    assert response.status_code == 422
