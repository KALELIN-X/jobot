"""One explicit fact confirmation can approve a safe, unattempted queue entry."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from test_workflow import HEADERS, add_job, await_idle, configure_and_approve


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic local refresh"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    with TestClient(create_app(tmp_path)) as value:
        yield value


def prepare(client):
    configure_and_approve(client)
    add_job(client, "question-held")
    question = {"question": "Preferred office?", "type": "text", "required": True, "options": []}
    client.app.state.store.update_job("question-held", "needs_attention", pending_questions=[question], form_questions=[question])
    return {"job_id": "question-held", "question": "Preferred office?", "answer": "Boston", "scope": "employer",
            "approve_and_continue": True,
            "expected_fingerprint": client.get("/api/state").json()["approval"]["candidate_fingerprint"]}


def test_one_confirmation_prepares_but_does_not_secretly_start_a_run(client):
    body = prepare(client)
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["continue_ready"]
    assert not response.json()["approval_required"]
    state = client.get("/api/state").json()
    assert state["approval"]["approved"]
    assert not state["automation"]["enabled"]
    assert not state["automation"]["running"]
    assert client.app.state.store.job("question-held")["status"] == "queued"
    assert client.app.state.store.job("question-held")["attempted_at"] is None


def test_normal_run_endpoint_still_reserves_one_attempt_and_checks_confirmation(client, monkeypatch):
    body = prepare(client)
    continuation = client.post("/api/answers", headers=HEADERS, json=body).json()
    assert continuation["continue_ready"]
    opened = []
    async def synthetic_application(job, profile, resume_path, **options):
        opened.append(job["id"])
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic local confirmation", "confirmation": "Synthetic receipt"}
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_application)
    assert client.post(f"/api/run?expected_stop_generation={continuation['stop_generation']}", headers=HEADERS).status_code == 200
    await_idle(client)
    assert opened == ["question-held"]
    assert client.app.state.store.job("question-held")["status"] == "submitted"
    assert client.app.state.store.destination_attempted(client.app.state.store.job("question-held"))
    assert client.post("/api/answers", headers=HEADERS, json={**body,
        "expected_fingerprint": client.get("/api/state").json()["approval"]["candidate_fingerprint"]}).status_code == 409
    assert opened == ["question-held"]


@pytest.mark.parametrize("endpoint,stop_body", [("/api/run/stop", None), ("/api/automation", {"enabled": False})])
def test_stop_during_answer_save_cannot_be_overridden_by_continuation(client, monkeypatch, endpoint, stop_body):
    body = prepare(client)
    from jobradar.ranking import rank_job
    cancelled = False

    def cancel_during_rerank(*args, **kwargs):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            assert client.post(endpoint, headers=HEADERS, json=stop_body).status_code == 200
        return rank_job(*args, **kwargs)

    monkeypatch.setattr("jobradar.ranking.rank_job", cancel_during_rerank)
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 409
    assert client.app.state.store.get("stop_requested") is True
    assert not client.get("/api/state").json()["approval"]["approved"]
    assert client.app.state.store.job(body["job_id"])["status"] == "needs_attention"
    assert client.app.state.store.job(body["job_id"])["attempted_at"] is None


@pytest.mark.parametrize("endpoint,stop_body", [("/api/run/stop", None), ("/api/automation", {"enabled": False})])
def test_stop_after_confirmation_prevents_delayed_frontend_run(client, endpoint, stop_body):
    continuation = client.post("/api/answers", headers=HEADERS, json=prepare(client)).json()
    assert continuation["continue_ready"]
    assert client.post(endpoint, headers=HEADERS, json=stop_body).status_code == 200
    result = client.post(f"/api/run?expected_stop_generation={continuation['stop_generation']}", headers=HEADERS)
    assert result.status_code == 409
    assert client.app.state.store.get("stop_requested") is True
    assert client.app.state.store.job("question-held")["attempted_at"] is None


def test_changed_settings_refuse_continue_before_saving_an_answer(client):
    body = prepare(client)
    client.post("/api/profile", headers=HEADERS, json={"name": "Changed candidate"})
    before = deepcopy(client.app.state.store.get("profile"))
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 409
    assert client.app.state.store.get("profile") == before
    assert client.app.state.store.job("question-held")["attempted_at"] is None


def test_live_polled_fingerprint_is_not_a_substitute_for_reviewing_changed_rules(client):
    body = prepare(client)
    assert client.post("/api/rules", headers=HEADERS, json={"company_scope": "any", "min_score": 50}).status_code == 200
    # Simulate a stale tab polling the new settings without a human approving
    # them. A current hash alone must never silently expand employer scope.
    body["expected_fingerprint"] = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    before = deepcopy(client.app.state.store.get("profile"))
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 409
    assert client.app.state.store.get("profile") == before
    assert not client.get("/api/state").json()["approval"]["approved"]


def test_other_required_fact_holds_role_and_never_approves_or_retries(client):
    body = prepare(client)
    job = client.app.state.store.job("question-held")
    extra = {"question": "Favorite editor?", "type": "text", "required": True, "options": []}
    client.app.state.store.update_job(job["id"], "needs_attention",
                                      pending_questions=[*job["pending_questions"], extra],
                                      form_questions=[*job["form_questions"], extra])
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 200, response.text
    assert not response.json()["continue_ready"]
    assert not client.get("/api/state").json()["approval"]["approved"]
    assert client.app.state.store.job(job["id"])["status"] == "needs_attention"
    assert client.app.state.store.job(job["id"])["attempted_at"] is None


@pytest.mark.parametrize("status", ["submitted", "uncertain", "submitting", "applied_elsewhere", "filling"])
def test_protected_application_state_is_never_requeued_by_answering(client, status):
    body = prepare(client)
    client.app.state.store.update_job(body["job_id"], status)
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 409
    assert client.app.state.store.job(body["job_id"])["status"] == status


def test_history_under_another_feed_id_prevents_continue(client):
    body = prepare(client)
    job = client.app.state.store.job(body["job_id"])
    client.app.state.store.record_external_application(url=job["apply_url"])
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 409
    assert client.app.state.store.destination_attempted(job)
