"""HTTP workflow tests with synthetic jobs, isolated storage, and no browser/network."""
from __future__ import annotations

import copy
import os
import time
import uuid
from pathlib import Path

# app.py constructs its default app at import time; isolate that side effect too.
os.environ["JOBRADAR_DATA_DIR"] = str(
    Path(__file__).resolve().parent.parent / "data" / "pytest-direct-fetch-import" / uuid.uuid4().hex
)

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.destinations import SIMPLIFY_SOURCE_URL, SUPPORTED_HOSTS, listing_binding, parse_destination, public_detail_url


HEADERS = {"X-JobRadar-Client": "dashboard"}
URL = "https://job-boards.greenhouse.io/example/jobs/1234567009"
PROFILE = {"name": "Alex Example", "email": "alex@example.test", "location": "Boston, MA",
           "approved_work_locations": ["United States"], "experience_months": 12,
           "graduation_month": "2022-06", "work_authorized": True, "needs_sponsorship": False,
           "skills": ["Python", "SQL"]}
RULES = {"company_scope": "any", "locations": ["United States"], "allowed_ats": ["lever", "greenhouse"],
         "max_required_experience_months": 12, "prefer_new_grad": True,
         "enforce_graduation_window": False, "min_score": 60, "daily_limit": 3}
DESCRIPTION = """New graduate software engineering role.
Requirements:
0-1 years of professional software engineering experience.
Graduation between 2026 and 2027.
Experience with Python and SQL.
"""


def public_job(identifier="direct-row", url=URL, company="Example", **changes):
    destination = parse_destination(url)
    job = {"id": identifier, "company": company, "title": "New Grad Backend Engineer",
           "location": "Boston, MA", "country_code": "US", "work_model": "On Site",
           "source": "SimplifyJobs", "source_url": SIMPLIFY_SOURCE_URL, "source_new_grad": True,
           "detail_url": destination["canonical_url"], "apply_url": destination["apply_url"],
           "ats": destination["ats"], "description": DESCRIPTION, "description_source": "Official public posting",
           "closed": False, "status": "new", **changes}
    job["destination_provenance"] = {"kind": "simplify_feed", "source_url": SIMPLIFY_SOURCE_URL,
                                     "application_key": destination["application_key"], "listing_binding": listing_binding(job)}
    job["employer_verified"] = {"source_url": public_detail_url(destination),
                                "application_key": destination["application_key"], "posting_id": destination["posting_id"]}
    return job


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "closed":False, "candidate_refresh":{"status":"verified","message":"Synthetic official refresh"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    async def forbidden_browser(*args, **kwargs):
        pytest.fail("This integration test must not launch an employer browser")
    monkeypatch.setattr("jobradar.autofill.run_application", forbidden_browser)
    with TestClient(create_app(tmp_path)) as instance:
        yield instance


def configure(client):
    assert client.post("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json=RULES).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("synthetic-resume.txt", b"Alex Example built Python services and SQL databases.", "text/plain")}).status_code == 200


def mock_public_fetch(monkeypatch, rows):
    calls = []
    async def fetch_jobs(limit):
        calls.append(limit)
        return copy.deepcopy(rows)
    async def enrich(jobs, company_boards):
        return jobs
    monkeypatch.setattr("jobradar.ingest.fetch_jobs", fetch_jobs)
    monkeypatch.setattr("jobradar.ingest.enrich_lever_jobs", enrich)
    return calls


def approve(client, acknowledge=True):
    reviewed = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    return client.post("/api/approve", headers=HEADERS,
                       json={"acknowledge": acknowledge, "expected_fingerprint": reviewed})


def wait_idle(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if not state["automation"]["running"]:
            return state
        time.sleep(0.01)
    pytest.fail("Synthetic queue did not return to idle")


def test_fetch_save_rank_exposes_genuine_ready_greenhouse_and_discovery_counts(client, monkeypatch):
    configure(client)
    calls = mock_public_fetch(monkeypatch, [public_job()])
    response = client.post("/api/fetch", headers=HEADERS)
    assert response.status_code == 200
    summary = response.json()
    assert calls == [100]
    assert {key: summary[key] for key in ("count", "verified_destinations", "direct_links", "ready")} == {
        "count": 1, "verified_destinations": 1, "direct_links": 1, "ready": 1}
    assert summary["sources"] == ["SimplifyJobs"]
    state = client.get("/api/state").json()
    assert state["discovery"] == summary
    assert state["last_fetch"]
    assert state["queue"]["ready"] == 1
    assert not state["queue"]["can_run"]  # Fetched eligibility does not grant approval.
    saved = state["jobs"][0]
    assert saved["score"] >= 60
    assert saved["score_detail"]["experience_requirement"]["minimum_months"] == 0
    assert saved["score_detail"]["is_new_grad"] is True
    assert next(check for check in saved["score_detail"]["eligibility"] if check["label"] == "Graduation date")["status"] == "waived"
    assert saved["submission_check"] == {"eligible": True, "blockers": []}
    assert client.app.state.store.job("direct-row")["score"] == saved["score"]
    assert "submission_check" not in client.app.state.store.job("direct-row")


def test_review_approval_does_not_dispatch_until_explicit_run(client, monkeypatch):
    configure(client)
    mock_public_fetch(monkeypatch, [public_job()])
    assert client.post("/api/fetch", headers=HEADERS).status_code == 200
    observed = []
    async def synthetic_worker(job, profile, resume_path, **options):
        observed.append(job["id"])
        assert resume_path.is_file()
        assert options["allowed_hosts"] == set(SUPPORTED_HOSTS)
        assert options["submit"] is True
        return {"status": "needs_attention", "message": "Synthetic test stopped before any browser or submission."}
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_worker)
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert approve(client, acknowledge=False).status_code == 400
    assert observed == []
    assert approve(client).status_code == 200
    state = client.get("/api/state").json()
    assert state["queue"]["can_run"] is True
    assert not state["automation"]["enabled"]
    assert observed == []
    run = client.post("/api/run", headers=HEADERS)
    assert run.status_code == 200 and run.json()["ready"] == 1
    state = wait_idle(client)
    assert observed == ["direct-row"]
    assert state["jobs"][0]["status"] == "needs_attention"
    assert state["jobs"][0]["attempted_at"] is None
    assert not state["automation"]["enabled"]


@pytest.mark.parametrize("refresh_status,description", [
    ("unavailable", DESCRIPTION),
    ("closed", DESCRIPTION),
    ("verified", "Required qualifications:\n2+ years of professional experience.\nPython and SQL."),
])
def test_current_official_requirements_are_rechecked_before_browser_filling(client, monkeypatch, refresh_status, description):
    configure(client)
    mock_public_fetch(monkeypatch, [public_job()])
    assert client.post("/api/fetch", headers=HEADERS).json()["ready"] == 1
    assert approve(client).status_code == 200
    refreshed = []
    async def recheck(job):
        refreshed.append(job["id"])
        return {**job, "description":description,
                "candidate_refresh":{"status":refresh_status,"message":"Synthetic current posting is held"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", recheck)
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = wait_idle(client)
    saved = client.app.state.store.job("direct-row")
    assert refreshed == ["direct-row"]
    assert saved["status"] == "needs_attention"
    assert saved["attempted_at"] is None
    assert state["queue"]["attempts_today"] == 0
    if refresh_status == "verified":
        assert saved["score_detail"]["experience_requirement"]["minimum_months"] == 24
        assert "24 months" in saved["error"]


def test_approval_change_during_public_recheck_prevents_filling(client, monkeypatch):
    configure(client)
    mock_public_fetch(monkeypatch, [public_job()])
    client.post("/api/fetch", headers=HEADERS)
    assert approve(client).status_code == 200
    async def changed_configuration(job):
        client.app.state.store.set("profile", {**PROFILE,"email":"updated@example.test"})
        return {**job,"candidate_refresh":{"status":"verified"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", changed_configuration)
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = wait_idle(client)
    assert not state["approval"]["approved"]
    assert client.app.state.store.job("direct-row")["status"] == "new"
    assert state["queue"]["attempts_today"] == 0


def test_failed_posting_refreshes_have_a_per_run_network_budget(client, monkeypatch):
    configure(client)
    rows = [public_job(f"row-{i}", url=f"https://job-boards.greenhouse.io/example/jobs/{1234000+i}") for i in range(6)]
    mock_public_fetch(monkeypatch, rows)
    assert client.post("/api/fetch", headers=HEADERS).json()["ready"] == 6
    assert approve(client).status_code == 200
    checked = []
    async def unavailable(job):
        checked.append(job["id"])
        return {**job,"candidate_refresh":{"status":"unavailable","message":"Synthetic unavailable posting"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", unavailable)
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = wait_idle(client)
    assert len(checked) == RULES["daily_limit"] == 3
    assert sum(job["status"] == "new" for job in state["jobs"]) == 3
    assert state["queue"]["attempts_today"] == 0
    assert any("verification budget" in event["message"] for event in state["events"])


def test_external_greenhouse_history_blocks_another_feed_id_and_host_after_fetch(client, monkeypatch):
    configure(client)
    history = client.post("/api/applied-elsewhere", headers=HEADERS, json={"url": URL})
    assert history.status_code == 200
    assert history.json()["matched_jobs"] == 0
    alias_url = URL.replace("job-boards.greenhouse.io", "boards.eu.greenhouse.io")
    mock_public_fetch(monkeypatch, [public_job("different-feed", url=alias_url, company="Example Incorporated")])
    summary = client.post("/api/fetch", headers=HEADERS).json()
    assert summary["verified_destinations"] == 1
    assert summary["ready"] == 0
    state = client.get("/api/state").json()
    assert state["jobs"][0]["status"] == "applied_elsewhere"
    assert not state["jobs"][0]["submission_check"]["eligible"]
    assert state["queue"]["ready"] == 0
    assert state["queue"]["attempts_today"] == 0
    assert approve(client).status_code == 200
    assert client.post("/api/run", headers=HEADERS).status_code == 409


def test_fetch_preserves_confirmed_delivery_evidence_and_blocks_cross_source_duplicate(client, monkeypatch):
    configure(client)
    first = public_job("confirmed")
    mock_public_fetch(monkeypatch, [first])
    assert client.post("/api/fetch", headers=HEADERS).status_code == 200
    store = client.app.state.store
    assert store.claim_job("confirmed") is not None
    store.reserve_attempt("confirmed", "2020-01-01", 3)
    store.update_job("confirmed", "submitted", confirmation="Synthetic employer receipt", applied_at="2020-01-01T12:00:00+00:00")
    attempted_at = store.job("confirmed")["attempted_at"]
    refresh = public_job("confirmed", title="New Grad Backend Engineer, Platform")
    duplicate = public_job("other-feed-id", url=URL.replace("job-boards.greenhouse.io", "boards.greenhouse.io"), company="Example AI")
    mock_public_fetch(monkeypatch, [refresh, duplicate])
    summary = client.post("/api/fetch", headers=HEADERS).json()
    assert summary["count"] == summary["verified_destinations"] == 2
    assert summary["ready"] == 0
    saved = store.job("confirmed")
    assert saved["status"] == "submitted"
    assert saved["confirmation"] == "Synthetic employer receipt"
    assert saved["applied_at"] == "2020-01-01T12:00:00+00:00"
    assert saved["attempted_at"] == attempted_at
    state = client.get("/api/state").json()
    assert state["stats"]["submitted"] == 1
    assert state["queue"]["ready"] == 0
    assert state["queue"]["attempts_today"] == 0
    other = next(job for job in state["jobs"] if job["id"] == "other-feed-id")
    assert not other["submission_check"]["eligible"]
    assert "already recorded" in " ".join(other["submission_check"]["blockers"])
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


@pytest.mark.parametrize("missing", ["apply_url", "employer_verified", "destination_provenance"])
def test_missing_or_unverified_destination_does_not_count_as_verified_or_ready(client, monkeypatch, missing):
    configure(client)
    row = public_job()
    row[missing] = None
    mock_public_fetch(monkeypatch, [row])
    response = client.post("/api/fetch", headers=HEADERS)
    assert response.status_code == 200
    summary = response.json()
    assert summary["count"] == 1
    assert summary["verified_destinations"] == summary["ready"] == 0
    assert summary["direct_links"] == (0 if missing == "apply_url" else 1)
    state = client.get("/api/state").json()
    assert state["queue"]["ready"] == 0
    assert state["queue"]["blocked"] == 1
    assert not state["jobs"][0]["submission_check"]["eligible"]
    assert state["discovery"] == summary
