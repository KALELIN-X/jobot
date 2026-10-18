import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jobradar.app import create_app, ordered_queue
from jobradar.models import Rules
from jobradar.ingest import SOURCE_URL, destination_provenance


HEADERS = {"X-JobRadar-Client": "dashboard"}
BASE_RULES = {"companies": ["Example"], "company_boards": {"Example": "example"}}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_job_preferences_are_optional_until_user_sets_them():
    rules = Rules()
    assert rules.max_required_experience_months is None
    assert rules.prefer_new_grad is False
    assert Rules(max_required_experience_months=0).max_required_experience_months == 0
    assert Rules(max_required_experience_months=120).max_required_experience_months == 120


@pytest.mark.parametrize("months", [-1, 121, 1.5, True, "12", "one year"])
def test_maximum_required_experience_rejects_invalid_values(months):
    with pytest.raises(ValidationError):
        Rules(max_required_experience_months=months)


@pytest.mark.parametrize("preference", ["true", "yes", 1, None])
def test_new_grad_preference_requires_a_boolean(preference):
    with pytest.raises(ValidationError):
        Rules(prefer_new_grad=preference)


@pytest.mark.parametrize("prefer_new_grad,expected", [(False, ["experienced", "new-grad", "unknown"]), (True, ["new-grad", "experienced", "unknown"])])
def test_queue_preference_changes_priority_without_changing_fit_scores(prefer_new_grad, expected):
    jobs = [
        {"id": "new-grad", "score": 70, "score_detail": {"is_new_grad": True}},
        {"id": "experienced", "score": 95, "score_detail": {"is_new_grad": False}},
        {"id": "unknown", "score": None, "score_detail": {}},
    ]
    ordered = ordered_queue(jobs, {"prefer_new_grad": prefer_new_grad})
    assert [job["id"] for job in ordered] == expected
    assert [job["score"] for job in jobs] == [70, 95, None]


def configure_and_approve(client, **preferences):
    assert client.post("/api/profile", headers=HEADERS, json={"name": "Alex Example", "email": "alex@example.test"}).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json={**BASE_RULES, **preferences}).status_code == 200
    assert client.post("/api/resume", headers=HEADERS, files={"file": ("resume.txt", b"Built Python APIs", "text/plain")}).status_code == 200
    fingerprint = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    assert client.post("/api/approve", headers=HEADERS, json={"acknowledge": True, "expected_fingerprint": fingerprint}).status_code == 200


def test_api_persists_adjustable_preferences_and_revokes_prior_approval(client):
    configure_and_approve(client)
    assert client.post("/api/automation", headers=HEADERS, json={"enabled": True}).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json={**BASE_RULES, "max_required_experience_months": 12, "prefer_new_grad": True}).status_code == 200
    state = client.get("/api/state").json()
    assert state["rules"]["max_required_experience_months"] == 12
    assert state["rules"]["prefer_new_grad"] is True
    assert state["approval"]["approved"] is False
    assert state["automation"]["enabled"] is False
    assert client.app.state.store.get("rules")["max_required_experience_months"] == 12
    assert client.post("/api/rules", headers=HEADERS, json={**BASE_RULES, "max_required_experience_months": None, "prefer_new_grad": False}).status_code == 200
    state = client.get("/api/state").json()
    assert state["rules"]["max_required_experience_months"] is None
    assert state["rules"]["prefer_new_grad"] is False


@pytest.mark.parametrize("prefer_new_grad,expected", [(False, ["experienced", "new-grad"]), (True, ["new-grad", "experienced"])])
def test_running_queue_uses_preference_then_unchanged_fit_score(client, monkeypatch, prefer_new_grad, expected):
    configure_and_approve(client, prefer_new_grad=prefer_new_grad)
    for index, (identifier, score, is_new_grad) in enumerate([("new-grad", 70, True), ("experienced", 95, False)]):
        apply_url = f"https://jobs.lever.co/example/12345678-abcd-1234-abcd-{index:012x}/apply"
        detail_url = f"https://jobright.ai/jobs/info/synthetic-{identifier}"
        client.app.state.store.save_job({
            "id": identifier, "company": "Example", "title": "Backend Engineer", "location": "Boston",
            "ats": "lever", "apply_url": apply_url,
            "source": "Jobright", "source_url": SOURCE_URL, "detail_url": detail_url,
            "destination_provenance": destination_provenance(apply_url, "jobright_detail", detail_url),
            "description": "Build Python backend APIs.", "score": score,
            "score_detail": {"is_new_grad": is_new_grad, "eligibility": []}, "status": "queued",
        })
    observed = []

    async def fake_application(job, profile, resume_path, **options):
        observed.append((job["id"], job["score"]))
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic test confirmation"}

    monkeypatch.setattr("jobradar.autofill.run_application", fake_application)
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    deadline = time.monotonic() + 5
    while client.get("/api/state").json()["automation"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [identifier for identifier, _ in observed] == expected
    assert dict(observed) == {"new-grad": 70, "experienced": 95}
