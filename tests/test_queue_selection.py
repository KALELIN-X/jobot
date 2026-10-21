"""Explicit local selection is separate from guarded application execution."""
import asyncio
import importlib
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


@pytest.fixture
def scheduled_client(tmp_path, monkeypatch):
    """Advance scheduler polling explicitly instead of racing its 5s timer.

    Application selection, approvals, browser boundaries and all other async
    waits remain real. Only the scheduler's polling tick is controlled here.
    """
    ticks = asyncio.Queue()

    class SchedulerClock:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            task = asyncio.current_task()
            if delay == 5 and task and task.get_coro().__qualname__.endswith(".<locals>.scheduler"):
                await ticks.get()
            else:
                await asyncio.sleep(delay)

    # Replace this module's asyncio reference, not asyncio.sleep globally: a
    # global patch could accidentally accelerate worker/network timeouts too.
    monkeypatch.setattr(importlib.import_module("jobradar.app"), "asyncio", SchedulerClock())

    async def refreshed(job):
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic official refresh"}}

    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    with TestClient(create_app(tmp_path)) as test_client:
        def advance():
            assert test_client.portal is not None
            test_client.portal.call(ticks.put_nowait, None)

        yield test_client, advance


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


def test_bulk_loading_uses_score_only_and_keeps_submission_blockers(client):
    configure_and_approve(client)
    discovered(client, "matching")
    discovered(client, "low-fit", score=20)
    discovered(client, "unranked", score=None)
    discovered(client, "dismissed", score=60)
    client.app.state.store.update_job("dismissed", "dismissed")
    discovered(client, "questions")
    client.app.state.store.update_job("questions", "needs_attention", pending_questions=[{
        "question": "Preferred office?", "type": "text", "required": True, "options": []}])
    discovered(client, "unsupported")
    client.app.state.store.update_job("unsupported", "failed", ats="workday", apply_url="https://example.test/application")
    discovered(client, "legacy-hold")
    client.app.state.store.update_job("legacy-hold", "held")
    add_job(client, "already-queued")
    discovered(client, "demo")
    store = client.app.state.store
    store.save_job({**store.job("demo"), "demo": True})
    before = client.get("/api/state").json()
    assert before["queue"]["loadable"] == 5
    assert before["queue"]["matching"] == 1
    assert before["queue"]["ready"] == 1
    response = client.post("/api/queue/matching", headers=HEADERS)
    assert response.status_code == 200
    result = response.json()
    assert {key: result[key] for key in ("added", "queued", "ready", "blocked", "min_score", "remaining_matching", "remaining_loadable")} == {
        "added": 5, "queued": 6, "ready": 4, "blocked": 2, "min_score": 60, "remaining_matching": 0, "remaining_loadable": 0}
    assert "Submission checks still apply" in result["message"]
    repeated = client.post("/api/queue/matching", headers=HEADERS).json()
    assert repeated["added"] == 0
    assert "score threshold" in repeated["message"]
    for identifier in ("matching", "dismissed", "questions", "unsupported", "legacy-hold", "already-queued"):
        assert store.job(identifier)["status"] == "queued"
    for identifier in ("low-fit", "unranked", "demo"):
        assert store.job(identifier)["status"] == "new"
    after = client.get("/api/state").json()
    assert after["queue"]["loadable"] == 0
    assert after["queue"]["ready"] == 4
    assert after["queue"]["blocked"] == 2
    assert after["approval"] == before["approval"]
    assert not after["automation"]["enabled"]
    assert store.job("questions")["pending_questions"]
    assert all(store.job(identifier)["attempted_at"] is None for identifier in ("matching", "questions", "unsupported"))


@pytest.mark.parametrize("status", ["new", "dismissed", "needs_attention", "failed", "held", "blocked"])
def test_individual_selection_allows_unranked_unsupported_and_held_roles(client, status):
    discovered(client, "manual", score=None)
    store = client.app.state.store
    store.update_job("manual", status, ats="workday", apply_url="https://example.test/application")
    assert select(client, "manual").status_code == 200
    state = client.get("/api/state").json()
    assert state["queue"]["queued"] == state["queue"]["blocked"] == 1
    assert state["queue"]["ready"] == 0
    assert store.job("manual")["attempted_at"] is None


def test_already_queued_record_returns_clear_reason_without_rewriting_review_notes(client):
    add_job(client, "selected")
    store = client.app.state.store
    store.update_job("selected", "queued", error="Keep this review note")
    before = store.job("selected")
    role = client.get("/api/state").json()["jobs"][0]
    assert role["queue_actions"] == {
        "can_add": False, "can_remove": True, "reason": "Already in your application queue."}
    response = select(client, "selected")
    assert response.status_code == 200
    assert response.json()["already_queued"] is True
    assert response.json()["message"] == "Already in your application queue."
    assert store.job("selected") == before


def test_bulk_loading_honors_the_saved_score_threshold_without_requiring_submission_approval(client):
    assert client.post("/api/rules", headers=HEADERS, json={"min_score": 85}).status_code == 200
    for identifier, score in [("below", 84.9), ("threshold", 85), ("above", 95), ("unknown", None)]:
        discovered(client, identifier, score)
    before = client.get("/api/state").json()
    assert before["queue"]["loadable"] == 2
    assert not before["approval"]["approved"]
    result = client.post("/api/queue/matching", headers=HEADERS).json()
    assert result["added"] == result["queued"] == 2
    assert result["min_score"] == 85
    assert client.app.state.store.job("below")["status"] == "new"
    assert client.app.state.store.job("unknown")["status"] == "new"
    assert not client.get("/api/state").json()["approval"]["approved"]
    assert client.post("/api/run", headers=HEADERS).status_code == 409


@pytest.mark.parametrize("status", ["active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"])
def test_active_or_recorded_statuses_are_excluded_from_individual_and_bulk_loading(client, status):
    discovered(client, "protected")
    client.app.state.store.update_job("protected", status)
    role = client.get("/api/state").json()["jobs"][0]
    assert role["queue_actions"]["can_add"] is False
    assert role["queue_actions"]["reason"]
    assert select(client, "protected").status_code == 409
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0
    assert client.app.state.store.job("protected")["status"] == status


@pytest.mark.parametrize("prior_status", ["submitted", "failed"])
def test_prior_attempt_blocks_individual_and_bulk_queue_selection(client, prior_status):
    configure_and_approve(client)
    add_job(client, "previous")
    store = client.app.state.store
    store.update_job("previous", "filling")
    store.reserve_attempt("previous", "2020-01-01", 5)
    store.update_job("previous", prior_status)
    previous = store.job("previous")
    store.save_job({**previous, "id": "duplicate", "status": "new"})
    assert select(client, "previous").status_code == 409
    assert select(client, "duplicate").status_code == 409
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0
    duplicate = next(job for job in client.get("/api/state").json()["jobs"] if job["id"] == "duplicate")
    assert duplicate["queue_actions"]["can_add"] is False
    assert "already recorded" in duplicate["queue_actions"]["reason"]


@pytest.mark.parametrize("existing_status", ["queued", "active", "applying", "filling", "submitting"])
def test_individual_and_bulk_selection_reject_alias_of_an_already_selected_posting(client, existing_status):
    add_job(client, "selected")
    discovered(client, "alias")
    store = client.app.state.store
    store.update_job("selected", existing_status, ats="greenhouse",
                     apply_url="https://boards.greenhouse.io/example/jobs/12345")
    store.update_job("alias", "new", ats="greenhouse",
                     apply_url="https://job-boards.eu.greenhouse.io/EXAMPLE/jobs/12345?utm_source=other-feed")
    role = next(job for job in client.get("/api/state").json()["jobs"] if job["id"] == "alias")
    assert role["queue_actions"]["can_add"] is False
    assert "already queued or active" in role["queue_actions"]["reason"]
    response = select(client, "alias")
    assert response.status_code == 409
    assert "already queued or active" in response.json()["detail"]
    assert client.get("/api/state").json()["queue"]["loadable"] == 0
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0
    assert store.job("alias")["status"] == "new"


def test_bulk_loading_deduplicates_canonical_postings_without_merging_distinct_roles(client):
    for identifier, score, url in [
        ("preferred", 99, "https://boards.greenhouse.io/example/jobs/12345"),
        ("alias", 90, "https://job-boards.greenhouse.io/EXAMPLE/jobs/12345?source=second-feed"),
        ("distinct", 80, "https://boards.greenhouse.io/example/jobs/67890"),
    ]:
        discovered(client, identifier, score)
        client.app.state.store.update_job(identifier, "new", ats="greenhouse", apply_url=url)
    # All three have the same employer/title; only exact destination identity
    # merges feed aliases. No application-history hold has been recorded.
    assert client.get("/api/state").json()["queue"]["loadable"] == 2
    result = client.post("/api/queue/matching", headers=HEADERS).json()
    assert result["added"] == result["queued"] == 2
    assert result["remaining_loadable"] == 0
    assert client.app.state.store.job("preferred")["status"] == "queued"
    assert client.app.state.store.job("distinct")["status"] == "queued"
    assert client.app.state.store.job("alias")["status"] == "new"
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 0


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


def test_enabled_approved_schedule_selects_matching_roles_after_fetch(scheduled_client, monkeypatch):
    client, advance_scheduler = scheduled_client
    configure_and_approve(client)
    discovered(client, "scheduled")
    saved = client.app.state.store.job("scheduled")
    discovered(client, "scheduled-alias")
    client.app.state.store.update_job("scheduled-alias", "new", apply_url=saved["apply_url"],
                                      destination_provenance=saved["destination_provenance"], detail_url=saved["detail_url"])
    discovered(client, "questions")
    client.app.state.store.update_job("questions", "new", pending_questions=[{
        "question": "Preferred office?", "type": "text", "required": True, "options": []}])
    discovered(client, "dismissed")
    client.app.state.store.update_job("dismissed", "dismissed")
    discovered(client, "unsupported")
    client.app.state.store.update_job("unsupported", "new", ats="workday", apply_url="https://example.test/application")
    reached = threading.Event()
    async def fake_fetch(limit):
        assert limit == 100
        return [saved]
    async def no_board_requests(jobs, boards):
        return jobs
    async def synthetic_browser(job, profile, resume_path, **options):
        assert job["id"] in {"scheduled", "scheduled-alias"}
        assert job["status"] == "filling"
        await options["before_submit"]()
        reached.set()
        return {"status": "submitted", "message": "Synthetic scheduled confirmation"}
    monkeypatch.setattr("jobradar.ingest.fetch_jobs", fake_fetch)
    monkeypatch.setattr("jobradar.ingest.enrich_lever_jobs", no_board_requests)
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_browser)
    try:
        assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 200
        advance_scheduler()
        assert reached.wait(8), "Approved scheduled fetch never selected the matching discovered role"
        await_idle(client)
    finally:
        client.post("/api/automation", headers=HEADERS, json={"enabled": False})
    statuses = [client.app.state.store.job(identifier)["status"] for identifier in ("scheduled", "scheduled-alias")]
    assert sorted(statuses) == ["new", "submitted"]
    assert client.app.state.store.job("questions")["status"] == "new"
    assert client.app.state.store.job("dismissed")["status"] == "dismissed"
    assert client.app.state.store.job("unsupported")["status"] == "new"


def test_configuration_change_during_scheduled_fetch_prevents_auto_selection(scheduled_client, monkeypatch):
    client, advance_scheduler = scheduled_client
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
        advance_scheduler()
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
