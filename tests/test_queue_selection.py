"""Explicit local selection is separate from guarded application execution."""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from test_workflow import HEADERS, PROFILE, add_job, await_idle, configure_and_approve


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic official refresh"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def discovered(client, identifier, score=95):
    add_job(client, identifier, score)
    client.app.state.store.update_job(identifier, "new")


def select(client, identifier):
    return client.post(f"/api/jobs/{identifier}/status", headers=HEADERS, json={"status": "queued"})


def test_discovered_matching_role_stays_unselected_until_the_owner_queues_it(client, monkeypatch):
    configure_and_approve(client)
    discovered(client, "discovered")
    async def unexpected(*args, **kwargs):
        pytest.fail("Discovery alone must never open an application")
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected)
    state = client.get("/api/state").json()
    assert {key: state["queue"][key] for key in ("discovered", "matching", "queued", "ready", "blocked")} == {
        "discovered": 1, "matching": 1, "queued": 0, "ready": 0, "blocked": 0}
    role = state["jobs"][0]
    assert role["queue_actions"] == {"can_add": True, "can_remove": False, "reason": ""}
    assert role["submission_check"]["matches_rules"] is True
    assert role["submission_check"]["eligible"] is False
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert select(client, "discovered").status_code == 200
    state = client.get("/api/state").json()
    assert state["queue"]["queued"] == state["queue"]["ready"] == 1
    assert state["queue"]["matching"] == state["queue"]["discovered"] == 0
    assert state["approval"]["approved"] is True
    assert not state["automation"]["enabled"]


def test_queue_selection_is_reversible_and_can_keep_held_roles_for_review(client):
    configure_and_approve(client)
    discovered(client, "low-fit", score=20)
    assert select(client, "low-fit").status_code == 200
    state = client.get("/api/state").json()
    assert state["queue"]["queued"] == state["queue"]["blocked"] == 1
    assert state["queue"]["ready"] == 0
    assert state["jobs"][0]["queue_actions"]["can_remove"] is True
    assert client.post("/api/jobs/low-fit/status", headers=HEADERS, json={"status": "new"}).status_code == 200
    state = client.get("/api/state").json()
    assert state["queue"]["queued"] == state["queue"]["matching"] == 0
    assert state["queue"]["discovered"] == 1
    assert client.app.state.store.job("low-fit")["status"] == "new"
    assert client.post("/api/jobs/low-fit/status", headers=HEADERS, json={"status": "new"}).status_code == 409


def test_bulk_queue_adds_only_safe_matching_discoveries_and_is_idempotent(client):
    configure_and_approve(client)
    discovered(client, "matching")
    discovered(client, "low-fit", score=20)
    discovered(client, "dismissed")
    client.app.state.store.update_job("dismissed", "dismissed")
    discovered(client, "questions")
    client.app.state.store.update_job("questions", "needs_attention")
    discovered(client, "demo")
    store = client.app.state.store
    store.save_job({**store.job("demo"), "demo": True})
    response = client.post("/api/queue/matching", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["added"] == response.json()["queued"] == 1
    assert response.json()["remaining_matching"] == 0
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0
    assert client.app.state.store.job("matching")["status"] == "queued"
    assert client.app.state.store.job("low-fit")["status"] == "new"
    assert client.app.state.store.job("dismissed")["status"] == "dismissed"
    assert client.app.state.store.job("questions")["status"] == "needs_attention"
    assert client.app.state.store.job("demo")["status"] == "new"


def test_prior_attempt_blocks_individual_and_bulk_queue_selection(client):
    configure_and_approve(client)
    add_job(client, "previous")
    store = client.app.state.store
    store.update_job("previous", "filling")
    store.reserve_attempt("previous", "2020-01-01", 5)
    store.update_job("previous", "submitted")
    previous = store.job("previous")
    store.save_job({**previous, "id": "duplicate", "status": "new"})
    assert select(client, "previous").status_code == 409
    assert select(client, "duplicate").status_code == 409
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0
    duplicate = next(job for job in client.get("/api/state").json()["jobs"] if job["id"] == "duplicate")
    assert duplicate["queue_actions"]["can_add"] is False
    assert "already recorded" in duplicate["queue_actions"]["reason"]


def test_local_queue_selection_needs_no_submission_approval(client):
    discovered(client, "review-first")
    assert select(client, "review-first").status_code == 200
    state = client.get("/api/state").json()
    assert not state["approval"]["approved"]
    assert state["queue"]["queued"] == 1
    assert client.post("/api/run", headers=HEADERS).status_code == 409


def test_removing_selected_role_during_refresh_prevents_browser_open(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "selected")
    discovered(client, "unselected")
    entered, release = threading.Event(), threading.Event()
    refreshed = []
    async def paused_refresh(job):
        refreshed.append(job["id"])
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic official refresh"}}
    async def unexpected_browser(*args, **kwargs):
        pytest.fail("Removed and unselected roles cannot enter the browser")
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", paused_refresh)
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_browser)
    try:
        assert client.post("/api/run", headers=HEADERS).status_code == 200
        assert entered.wait(3)
        assert client.post("/api/jobs/selected/status", headers=HEADERS, json={"status": "new"}).status_code == 200
    finally:
        release.set()
    await_idle(client)
    assert refreshed == ["selected"]
    assert client.app.state.store.job("selected")["status"] == "new"
    assert client.app.state.store.job("selected")["attempted_at"] is None


def test_manual_run_never_picks_up_unselected_or_later_added_roles(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "selected", score=99)
    discovered(client, "later", score=90)
    entered, release = threading.Event(), threading.Event()
    observed = []
    async def paused_browser(job, profile, resume_path, **options):
        observed.append(job["id"])
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic confirmation"}
    monkeypatch.setattr("jobradar.autofill.run_application", paused_browser)
    try:
        assert client.post("/api/run", headers=HEADERS).status_code == 200
        assert entered.wait(3)
        assert select(client, "later").status_code == 200
    finally:
        release.set()
    await_idle(client)
    assert observed == ["selected"]
    assert client.app.state.store.job("later")["status"] == "queued"
    assert client.app.state.store.job("later")["attempted_at"] is None


def test_enabled_approved_schedule_selects_matching_roles_after_fetch(client, monkeypatch):
    configure_and_approve(client)
    discovered(client, "scheduled")
    saved = client.app.state.store.job("scheduled")
    reached = threading.Event()
    async def fake_fetch(limit):
        assert limit == 100
        return [saved]
    async def no_board_requests(jobs, boards):
        return jobs
    async def synthetic_browser(job, profile, resume_path, **options):
        assert job["status"] == "filling"
        await options["before_submit"]()
        reached.set()
        return {"status": "submitted", "message": "Synthetic scheduled confirmation"}
    monkeypatch.setattr("jobradar.ingest.fetch_jobs", fake_fetch)
    monkeypatch.setattr("jobradar.ingest.enrich_lever_jobs", no_board_requests)
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_browser)
    try:
        assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 200
        assert reached.wait(8), "Approved scheduled fetch never selected the matching discovered role"
        await_idle(client)
    finally:
        client.post("/api/automation", headers=HEADERS, json={"enabled": False})
    assert client.app.state.store.job("scheduled")["status"] == "submitted"


def test_configuration_change_during_scheduled_fetch_prevents_auto_selection(client, monkeypatch):
    configure_and_approve(client)
    discovered(client, "scheduled")
    saved = client.app.state.store.job("scheduled")
    entered, release = threading.Event(), threading.Event()
    async def fake_fetch(limit):
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return [saved]
    async def no_board_requests(jobs, boards):
        return jobs
    async def unexpected_browser(*args, **kwargs):
        pytest.fail("Revoked scheduling approval cannot launch a browser")
    monkeypatch.setattr("jobradar.ingest.fetch_jobs", fake_fetch)
    monkeypatch.setattr("jobradar.ingest.enrich_lever_jobs", no_board_requests)
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_browser)
    try:
        assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 200
        assert entered.wait(8)
        assert client.post("/api/profile", headers=HEADERS, json={**PROFILE, "email": "changed@example.test"}).status_code == 200
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and client.get("/api/state").json()["automation"]["fetching"]:
        time.sleep(0.01)
    state = client.get("/api/state").json()
    assert not state["approval"]["approved"]
    assert not state["automation"]["enabled"]
    assert client.app.state.store.job("scheduled")["status"] == "new"
    assert client.app.state.store.job("scheduled")["attempted_at"] is None
