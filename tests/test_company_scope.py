"""Employer scope and source-bound destinations, with no external traffic."""
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.approval import job_problems
from jobradar.ingest import SOURCE_URL, canonical_lever_application_url, destination_provenance, verified_destination
from jobradar.models import Rules
from jobradar.ranking import rank_job


HEADERS = {"X-JobRadar-Client": "dashboard"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "closed":False, "candidate_refresh":{"status":"verified","message":"Synthetic official refresh"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def configure(client, **rules):
    assert client.post("/api/profile", headers=HEADERS,
                       json={"name":"Alex Example", "email":"alex@example.test", "approved_work_locations":["Boston, MA"], "work_authorized":True, "needs_sponsorship":False}).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("resume.txt", b"Built Python services.", "text/plain")}).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json={"company_scope": "any", **rules}).status_code == 200


def approve(client):
    return client.post("/api/approve", headers=HEADERS, json={
        "acknowledge": True,
        "expected_fingerprint": client.get("/api/state").json()["approval"]["candidate_fingerprint"],
    })


def fetched_job(identifier, company="Different Employer", **changes):
    url = f"https://jobs.lever.co/{identifier}/{uuid.uuid5(uuid.NAMESPACE_URL, identifier)}/apply"
    detail = f"https://jobright.ai/jobs/info/synthetic-{identifier}"
    return {"id": identifier, "company": company, "title": "Backend Engineer", "location": "Boston, MA",
            "description": "Build Python backend services.", "ats": "lever", "closed": False,
            "source": "Jobright", "source_url": SOURCE_URL, "detail_url": detail,
            "apply_url": url, "destination_provenance": destination_provenance(url, "jobright_detail", detail),
            "status": "new", "score": 90, "score_detail": {"eligibility": []}, **changes}


def await_idle(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get("/api/state").json()
        if not state["automation"]["running"]:
            return state
        time.sleep(0.01)
    pytest.fail("Synthetic queue did not finish")


def test_new_defaults_any_and_legacy_named_restrictions_are_preserved():
    assert Rules().company_scope == "any"
    assert Rules(companies=["Example"]).company_scope == "selected"
    assert Rules(company_scope="any", companies=["Example"]).company_scope == "any"


@pytest.mark.parametrize("saved,expected", [({}, "any"), ({"companies": []}, "any"),
                                            ({"companies": ["Example"]}, "selected")])
def test_legacy_scope_is_normalized_in_state_without_rewriting_saved_data(client, saved, expected):
    client.app.state.store.set("rules", saved)
    assert client.get("/api/state").json()["rules"]["company_scope"] == expected
    assert client.app.state.store.get("rules") == saved


def test_any_company_approval_needs_no_employer_names_or_board_mapping(client):
    configure(client)
    state = client.get("/api/state").json()
    assert state["setup_problems"] == []
    assert state["rules"]["companies"] == []
    assert state["rules"]["company_boards"] == {}
    assert approve(client).status_code == 200
    assert not client.get("/api/state").json()["automation"]["enabled"]


def test_selected_scope_requires_only_a_nonempty_optional_restriction(client):
    configure(client, company_scope="selected")
    response = approve(client)
    assert response.status_code == 400
    assert "selected-company mode" in response.json()["detail"]
    assert client.post("/api/rules", headers=HEADERS,
                       json={"company_scope": "selected", "companies": ["Example"]}).status_code == 200
    assert approve(client).status_code == 200


def test_any_scope_can_run_newly_fetched_roles_from_multiple_employers(client, monkeypatch):
    configure(client)
    assert approve(client).status_code == 200
    observed = []

    async def synthetic_application(job, profile, resume_path, **options):
        observed.append(job["company"])
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic confirmation"}

    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_application)
    for identifier, company in [("first-board", "First Company"), ("second-board", "Second Company")]:
        client.app.state.store.save_job(fetched_job(identifier, company))
    assert client.get("/api/state").json()["queue"]["matching"] == 2
    assert client.get("/api/state").json()["queue"]["ready"] == 0
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 2
    assert client.get("/api/state").json()["queue"]["ready"] == 2
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    state = await_idle(client)
    assert set(observed) == {"First Company", "Second Company"}
    assert state["stats"]["submitted"] == 2
    assert not state["automation"]["enabled"]


def test_selected_scope_still_skips_other_employers(client, monkeypatch):
    configure(client, company_scope="selected", companies=["First Company"])
    assert approve(client).status_code == 200
    for identifier, company in [("first-board", "First Company"), ("second-board", "Second Company")]:
        client.app.state.store.save_job(fetched_job(identifier, company))
    observed = []

    async def synthetic_application(job, profile, resume_path, **options):
        observed.append(job["company"])
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic confirmation"}

    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_application)
    assert client.post("/api/queue/matching", headers=HEADERS).json()["added"] == 2
    assert client.get("/api/state").json()["queue"]["ready"] == 1
    assert client.get("/api/state").json()["queue"]["blocked"] == 1
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    await_idle(client)
    assert observed == ["First Company"]
    assert client.app.state.store.job("second-board")["attempted_at"] is None


@pytest.mark.parametrize("change", [
    {"destination_provenance": None}, {"source": "pasted URL"}, {"source_url": "https://evil.test/feed"},
    {"detail_url": "https://jobright.ai/jobs/info/different-role"},
    {"apply_url": "https://jobs.lever.co/different/12345678-abcd-1234-abcd-123456789abc/apply"},
    {"apply_url": "https://jobs.lever.co/fetched/invalid/apply"},
    {"apply_url": "https://jobs.lever.co.evil.test/fetched/12345678-abcd-1234-abcd-123456789abc/apply"},
])
def test_any_company_scope_does_not_trust_arbitrary_or_changed_destinations(change):
    job = fetched_job("fetched")
    assert verified_destination(job)
    job.update(change)
    assert not verified_destination(job)
    assert job_problems(job, Rules().model_dump())


def test_supported_posting_urls_are_canonicalized_but_bad_paths_never_are():
    url = fetched_job("fetched")["apply_url"]
    assert canonical_lever_application_url(url.removesuffix("/apply") + "?utm_source=test") == url
    for invalid in [url + "#other", url.replace("/apply", "/apply/extra"), url.replace("https:", "http:")]:
        assert canonical_lever_application_url(invalid) is None


def test_any_scope_ignores_only_stale_company_metadata_not_other_filters():
    job = fetched_job("fetched", score_detail={"eligibility": [
        {"label": "Approved company", "status": "fail", "detail": "Old company restriction"},
    ]})
    assert job_problems(job, Rules(company_scope="any", companies=["Old Employer"]).model_dump()) == []
    job["score_detail"]["eligibility"].append({"label": "Graduation", "status": "fail", "detail": "Wrong window"})
    assert "Graduation" in " ".join(job_problems(job, Rules().model_dump()))
    assert job_problems(fetched_job("fetched", score=20), Rules().model_dump())


def test_any_scope_omits_company_ranking_gate_but_preserves_fit_score():
    job = fetched_job("fetched")
    profile = {"experience_months": 12, "graduation_date": "2026-05-15"}
    any_result = rank_job(job, profile, "Python", {"company_scope": "any", "companies": ["Old Employer"]})
    selected = rank_job(job, profile, "Python", {"company_scope": "selected", "companies": ["Old Employer"]})
    assert not any(item["label"] == "Approved company" for item in any_result["eligibility"])
    assert any(item["label"] == "Approved company" and item["status"] == "fail" for item in selected["eligibility"])
    assert any_result["score"] == selected["score"]


@pytest.mark.parametrize("scope", ["any", "selected"])
def test_old_enrichment_board_mapping_does_not_block_a_genuine_published_destination(scope):
    job = fetched_job("current-board", company="Example")
    rules = Rules(company_scope=scope, companies=["Example"], company_boards={"Example": "outdated-board"})
    assert verified_destination(job)
    assert job_problems(job, rules.model_dump()) == []


def test_scope_changes_need_a_new_exact_approval(client):
    configure(client, company_scope="selected", companies=["Example"])
    assert approve(client).status_code == 200
    reviewed = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    assert client.post("/api/rules", headers=HEADERS, json={"company_scope": "any"}).status_code == 200
    state = client.get("/api/state").json()
    assert not state["approval"]["approved"]
    assert reviewed != state["approval"]["candidate_fingerprint"]
    assert client.post("/api/approve", headers=HEADERS,
                       json={"acknowledge": True, "expected_fingerprint": reviewed}).status_code == 409


def test_any_company_scope_preserves_external_application_duplicate_hold(client):
    configure(client)
    assert approve(client).status_code == 200
    job = fetched_job("fetched")
    client.app.state.store.save_job(job)
    assert client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": job["id"]}).status_code == 200
    client.app.state.store.save_job({**job, "id": "reposted-feed-id"})
    state = client.get("/api/state").json()
    assert state["queue"]["ready"] == 0
    assert all(row["status"] == "applied_elsewhere" for row in state["jobs"])
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert client.app.state.store.attempt_count(datetime.now(timezone.utc).date().isoformat()) == 0
