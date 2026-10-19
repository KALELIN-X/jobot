"""Optional quotas never replace approval, selection or durable attempt history."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from test_workflow import HEADERS, PROFILE, RULES, add_job, await_idle


def approve(client, fingerprint=None):
    fingerprint = fingerprint or client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    return client.post("/api/approve", headers=HEADERS,
                       json={"acknowledge": True, "expected_fingerprint": fingerprint})


def configure(client, limit):
    assert client.post("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json={**RULES, "daily_limit": limit}).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("resume.txt", b"Built Python backend services and REST APIs.", "text/plain")}).status_code == 200
    assert approve(client).status_code == 200


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_new_install_defaults_to_unlimited_without_approving_or_starting_anything(client):
    state = client.get("/api/state").json()
    assert state["rules"]["daily_limit"] is None
    assert state["queue"]["remaining_today"] is None
    assert state["queue"]["daily_limit_enabled"] is False
    assert state["queue"]["attempts_today"] == 0
    assert not state["approval"]["approved"]
    assert not state["automation"]["enabled"]
    assert not any("limit reached" in problem for problem in state["queue"]["blockers"])


@pytest.mark.parametrize("limit", [None, 1, 25, 500])
def test_daily_cap_is_optional_and_can_be_above_twenty(client, limit):
    response = client.post("/api/rules", headers=HEADERS, json={"daily_limit": limit})
    assert response.status_code == 200
    state = client.get("/api/state").json()
    assert state["rules"]["daily_limit"] == limit
    assert state["queue"]["remaining_today"] == limit
    assert state["queue"]["daily_limit_enabled"] is (limit is not None)


@pytest.mark.parametrize("limit", [0, -1, 501, True, False, 25.5, "25", "unlimited", []])
def test_daily_cap_rejects_invalid_or_coerced_values(client, limit):
    response = client.post("/api/rules", headers=HEADERS, json={"daily_limit": limit})
    assert response.status_code == 422
    assert client.get("/api/state").json()["rules"]["daily_limit"] is None


def test_explicit_existing_cap_and_approval_survive_restart(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        configure(client, 7)
        saved_approval = client.app.state.store.get("approval")
    with TestClient(create_app(tmp_path)) as restarted:
        state = restarted.get("/api/state").json()
        assert state["rules"]["daily_limit"] == state["queue"]["remaining_today"] == 7
        assert state["queue"]["daily_limit_enabled"] is True
        assert state["approval"]["approved"] is True
        assert restarted.app.state.store.get("approval") == saved_approval


def test_switching_to_unlimited_requires_review_of_the_new_rules(client):
    configure(client, 5)
    previous_fingerprint = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    assert client.post("/api/rules", headers=HEADERS, json={**RULES, "daily_limit": None}).status_code == 200
    state = client.get("/api/state").json()
    assert state["rules"]["daily_limit"] is None
    assert not state["approval"]["approved"]
    assert not state["automation"]["enabled"]
    assert approve(client, previous_fingerprint).status_code == 409
    assert approve(client).status_code == 200


@pytest.mark.parametrize("limit,expected_submitted", [(None, 27), (25, 25)])
def test_large_selected_queue_respects_only_the_optional_user_cap(client, monkeypatch, limit, expected_submitted):
    configure(client, limit)
    checked, submitted = [], []

    async def verified(job):
        checked.append(job["id"])
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic official refresh"}}

    async def synthetic_application(job, profile, resume_path, **options):
        await options["before_submit"]()
        submitted.append(job["id"])
        return {"status": "submitted", "message": "Synthetic confirmation; no employer contacted"}

    monkeypatch.setattr("jobradar.refresh.refresh_candidate", verified)
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_application)
    for index in range(27):
        add_job(client, f"selected-{index}")
    add_job(client, "question-hold", score=100)
    client.app.state.store.update_job("question-hold", "queued", pending_questions=[{
        "question": "Preferred office?", "type": "text", "required": True, "options": []}])
    add_job(client, "unselected")
    client.app.state.store.update_job("unselected", "new")
    assert client.get("/api/state").json()["queue"]["ready"] == 27
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = await_idle(client, timeout=15)
    assert len(checked) == len(submitted) == expected_submitted
    assert set(checked) == set(submitted)
    assert state["queue"]["attempts_today"] == expected_submitted
    assert state["queue"]["queued"] == 27 - expected_submitted
    assert state["queue"]["remaining_today"] == (None if limit is None else 0)
    assert state["run"]["status"] == ("completed" if limit is None else "paused")
    store = client.app.state.store
    assert store.job("question-hold")["status"] == "needs_attention"
    assert store.job("question-hold")["attempted_at"] is None
    assert store.job("unselected")["status"] == "new"
    assert store.job("unselected")["attempted_at"] is None
    assert not any("verification budget" in event["message"] for event in state["events"])
    if limit is None:
        # Attempts above the old twenty-per-day ceiling do not prevent a later
        # selected role from being eligible under an approved unlimited rule.
        add_job(client, "later-selected")
        assert client.get("/api/state").json()["queue"]["can_run"] is True
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM destination_attempts").fetchone()[0] == expected_submitted
    day = datetime.now(timezone.utc).date().isoformat()
    assert store.attempt_count(day) == expected_submitted


def test_unlimited_refresh_checks_each_selected_role_once_even_when_none_can_be_verified(client, monkeypatch):
    configure(client, None)
    checked = []

    async def unavailable(job):
        checked.append(job["id"])
        return {**job, "candidate_refresh": {"status": "unavailable", "message": "Synthetic unavailable posting"}}

    async def unexpected_browser(*args, **kwargs):
        pytest.fail("Unverified postings must not open an application browser")

    monkeypatch.setattr("jobradar.refresh.refresh_candidate", unavailable)
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_browser)
    for index in range(23):
        add_job(client, f"unavailable-{index}")
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = await_idle(client, timeout=15)
    assert len(checked) == len(set(checked)) == 23
    assert state["queue"]["attempts_today"] == state["queue"]["queued"] == 0
    assert state["queue"]["remaining_today"] is None
    assert all(job["status"] == "needs_attention" and job["attempted_at"] is None for job in state["jobs"])
    assert state["run"]["status"] == "completed"
