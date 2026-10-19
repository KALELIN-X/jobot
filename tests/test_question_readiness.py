"""Known form questions block readiness until exact saved answers fit them."""
import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from test_workflow import HEADERS, PROFILE, add_job, await_idle, configure_and_approve


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "candidate_refresh": {"status": "verified", "message": "Synthetic official refresh"}}
    async def unexpected_browser(*args, **kwargs):
        pytest.fail("Known unanswered questions must block the browser")
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_browser)
    with TestClient(create_app(tmp_path)) as client:
        yield client


def question(*, required=True, kind="text", options=None):
    return {"question": "Preferred office?", "type": kind, "required": required, "options": options or []}


def role(client):
    return next(job for job in client.get("/api/state").json()["jobs"] if job["id"] == "questions")


def test_required_unanswered_question_blocks_selected_role_without_changing_its_queue(client):
    configure_and_approve(client)
    add_job(client, "questions")
    client.app.state.store.update_job("questions", "queued", pending_questions=[question()])
    state = client.get("/api/state").json()
    saved = role(client)
    assert saved["unanswered_question_count"] == 1
    assert saved["conflicting_answer_count"] == 0
    assert not saved["submission_check"]["eligible"]
    assert "required application question" in " ".join(saved["submission_check"]["blockers"])
    assert state["queue"]["queued"] == state["queue"]["blocked"] == 1
    assert state["queue"]["ready"] == 0
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert client.app.state.store.job("questions")["status"] == "queued"
    assert client.app.state.store.job("questions")["attempted_at"] is None


def test_confirmed_exact_answer_clears_question_but_does_not_requeue_or_approve(client):
    configure_and_approve(client)
    add_job(client, "questions")
    client.app.state.store.update_job("questions", "needs_attention", pending_questions=[question()])
    response = client.post("/api/answers", headers=HEADERS,
                           json={"job_id": "questions", "question": "Preferred office?", "answer": "Boston"})
    assert response.status_code == 200
    state = client.get("/api/state").json()
    saved = role(client)
    assert saved["unanswered_question_count"] == 0
    assert saved["submission_check"]["matches_rules"] is True
    assert not saved["submission_check"]["eligible"]
    assert saved["status"] == "needs_attention"
    assert state["queue"]["queued"] == 0
    assert not state["approval"]["approved"]
    assert not state["automation"]["enabled"]
    assert state["profile"]["answers"] == {"Preferred office?": "Boston"}


@pytest.mark.parametrize("kind", ["select", "radio"])
def test_recorded_choices_require_an_exact_current_saved_option(client, kind):
    configure_and_approve(client)
    add_job(client, "questions")
    options = [{"label": "Boston", "value": "bos"}, {"label": "New York", "value": "nyc"}]
    client.app.state.store.update_job("questions", "queued", pending_questions=[question(kind=kind, options=options)])
    # An answer for differently cased wording must not resolve this question.
    assert client.post("/api/profile", headers=HEADERS,
                       json={**PROFILE, "answers": {"preferred office?": "Boston"}}).status_code == 200
    assert role(client)["unanswered_question_count"] == 1
    assert client.post("/api/answers", headers=HEADERS,
                       json={"job_id": "questions", "question": "Preferred office?", "answer": "Boston"}).status_code == 200
    assert role(client)["unanswered_question_count"] == 0
    assert role(client)["submission_check"]["eligible"] is True
    # A different employer option list makes the previous exact answer unusable.
    client.app.state.store.update_job("questions", "queued", pending_questions=[question(kind=kind, options=options[1:])])
    assert role(client)["unanswered_question_count"] == 1
    assert role(client)["submission_check"]["eligible"] is False


def test_unanswered_optional_questions_remain_optional(client):
    configure_and_approve(client)
    add_job(client, "questions")
    client.app.state.store.update_job("questions", "queued", pending_questions=[question(required=False)])
    saved = role(client)
    assert saved["unanswered_question_count"] == saved["conflicting_answer_count"] == 0
    assert saved["submission_check"]["eligible"] is True
    assert client.get("/api/state").json()["queue"]["can_run"] is True


def test_optional_provided_answer_conflict_blocks_until_corrected_or_forgotten(client):
    configure_and_approve(client)
    add_job(client, "questions")
    options = [{"label": "Boston", "value": "bos"}]
    client.app.state.store.update_job("questions", "queued", pending_questions=[question(required=False, kind="select", options=options)])
    assert client.post("/api/profile", headers=HEADERS,
                       json={**PROFILE, "answers": {"Preferred office?": "Unavailable office"}}).status_code == 200
    saved = role(client)
    assert saved["unanswered_question_count"] == 0
    assert saved["conflicting_answer_count"] == 1
    assert saved["submission_check"]["eligible"] is False
    assert client.request("DELETE", "/api/answers", headers=HEADERS,
                          json={"question": "Preferred office?"}).status_code == 200
    saved = role(client)
    assert saved["conflicting_answer_count"] == 0
    assert saved["submission_check"]["eligible"] is True


def test_one_ready_role_does_not_allow_worker_to_open_another_known_question_hold(client, monkeypatch):
    configure_and_approve(client)
    add_job(client, "ready")
    add_job(client, "questions", score=99)
    client.app.state.store.update_job("questions", "queued", pending_questions=[question()])
    opened = []
    async def synthetic_browser(job, profile, resume_path, **options):
        opened.append(job["id"])
        await options["before_submit"]()
        return {"status": "submitted", "message": "Synthetic confirmation"}
    monkeypatch.setattr("jobradar.autofill.run_application", synthetic_browser)
    assert client.get("/api/state").json()["queue"]["ready"] == 1
    assert client.post("/api/run", headers=HEADERS).status_code == 200
    await_idle(client)
    assert opened == ["ready"]
    assert client.app.state.store.job("questions")["status"] == "needs_attention"
    assert client.app.state.store.job("questions")["attempted_at"] is None


def test_score_loading_can_queue_question_holds_without_making_them_submission_ready(client):
    configure_and_approve(client)
    add_job(client, "questions")
    client.app.state.store.update_job("questions", "new", pending_questions=[question()])
    state = client.get("/api/state").json()
    assert state["queue"]["discovered"] == 1
    assert state["queue"]["matching"] == 0
    assert state["queue"]["loadable"] == 1
    result = client.post("/api/queue/matching", headers=HEADERS).json()
    assert result["added"] == result["queued"] == result["blocked"] == 1
    assert result["ready"] == 0
    assert client.app.state.store.job("questions")["status"] == "queued"
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert client.app.state.store.job("questions")["attempted_at"] is None
