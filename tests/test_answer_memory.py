from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.answer_memory import answer_memory_state
from jobradar.autofill import plan_form
from jobradar.models import Profile


HEADERS = {"X-JobRadar-Client": "dashboard"}


def field(question, **changes):
    return {"id": "question", "label": question, "name": "custom", "type": "text",
            "tag": "input", "visible": True, "required": True, **changes}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_unknown_questions_are_reviewable_without_learning_prefilled_values():
    plan = plan_form([field("Favorite language? *", value="A site's unverified default")], {})
    assert plan.blockers
    assert not plan.actions
    assert plan.pending_questions == [{
        "id": plan.pending_questions[0]["id"], "key": "Favorite language?", "question": "Favorite language?",
        "type": "text", "required": True, "options": [], "reason": "missing_answer",
    }]
    assert "unverified" not in str(plan.pending_questions)


def test_optional_unfamiliar_question_is_captured_without_blocking_the_form():
    plan = plan_form([field("Favorite language?", required=False)], {})
    assert not plan.blockers
    assert plan.pending_questions[0]["required"] is False


@pytest.mark.parametrize("question,kind", [
    ("I agree to the terms", "checkbox"), ("Privacy agreement", "text"),
    ("Password", "password"), ("Email verification code", "text"),
    ("Social Security Number", "text"), ("CAPTCHA", "text"),
])
def test_challenges_and_legal_fields_never_enter_answer_candidates(question, kind):
    assert not plan_form([field(question, type=kind)], {}).pending_questions


def test_question_choices_are_visible_and_not_filled_by_guessing():
    plan = plan_form([field("Preferred office?", tag="select", type="select", options=[
        {"label": "Select", "value": ""}, {"label": "Boston", "value": "bos"},
        {"label": "New York", "value": "nyc"}, {"label": "Closed", "value": "closed", "disabled": True},
    ])], {})
    assert plan.blockers and not plan.actions
    assert plan.pending_questions[0]["options"] == [{"label": "Boston", "value": "bos"}, {"label": "New York", "value": "nyc"}]


def test_choice_labels_preserve_literal_asterisks_and_unavailable_answers_stay_pending():
    plan = plan_form([field("Preferred edition? *", type="select", tag="select", options=[
        {"label": "Pro*", "value": "pro"}, {"label": "Basic", "value": "basic"},
    ])], {"answers": {"Preferred edition?": "Unavailable edition"}})
    assert plan.blockers and not plan.actions
    assert plan.pending_questions[0]["options"][0]["label"] == "Pro*"
    state = answer_memory_state({"answers": {"Preferred edition?": "Unavailable edition"}}, [
        {"id": "held", "pending_questions": plan.pending_questions},
    ])
    assert state["pending"][0]["question"] == "Preferred edition?"


def test_save_answer_persists_and_reuses_only_the_exact_question(client, tmp_path):
    response = client.post("/api/answers", headers=HEADERS, json={"question": "  Favorite   language? *  ", "answer": " Python "})
    assert response.status_code == 200, response.text
    assert response.json()["approval_required"]
    profile = client.get("/api/state").json()["profile"]
    assert profile["answers"] == {"Favorite language?": "Python"}
    assert plan_form([field("Favorite language? ✱")], profile).actions[0].value == "Python"
    assert plan_form([field("Favorite programming language?")], profile).blockers
    assert plan_form([field("favorite language?")], profile).blockers
    # It survives a separate app/store instance rather than living in browser state.
    with TestClient(create_app(tmp_path)) as reopened:
        saved = reopened.get("/api/state").json()["answer_memory"]["saved"]
    assert saved == [{"question": "Favorite language?", "answer": "Python"}]


def test_new_answer_revokes_approval_and_stops_automation_but_never_requeues(client):
    store = client.app.state.store
    store.set("approval", {"approved": True, "fingerprint": "old approval"})
    store.set("automation", {"enabled": True})
    store.save_job({"id": "held", "company": "Example", "title": "Engineer", "status": "needs_attention"})
    result = client.post("/api/answers", headers=HEADERS, json={"question": "Favorite language?", "answer": "Python"})
    assert result.status_code == 200
    assert not store.get("approval")["approved"]
    assert not store.get("automation")["enabled"]
    assert store.get("stop_requested") is True
    assert store.job("held")["status"] == "needs_attention"
    assert not store.job("held")["attempted_at"]


def test_saved_answer_can_resolve_candidate_with_an_exact_choice(client):
    store = client.app.state.store
    plan = plan_form([field("Preferred office?", type="select", tag="select", options=[
        {"label": "Boston", "value": "bos"}, {"label": "New York", "value": "nyc"},
    ])], {})
    store.save_job({"id": "held", "company": "Example", "title": "Engineer", "status": "needs_attention", "pending_questions": plan.pending_questions})
    memory = client.get("/api/state").json()["answer_memory"]
    assert memory["pending"][0]["job_id"] == "held"
    assert not memory["saved"]
    assert client.post("/api/answers", headers=HEADERS, json={"question": "Preferred office?", "answer": "New", "job_id": "held"}).status_code == 422
    saved = client.post("/api/answers", headers=HEADERS, json={"question": "Preferred office?", "answer": "nyc", "job_id": "held"})
    assert saved.status_code == 200
    assert saved.json()["answer"] == "New York"
    profile = client.get("/api/state").json()["profile"]
    assert not client.get("/api/state").json()["answer_memory"]["pending"]
    future = plan_form([field("Preferred office?", type="select", tag="select", options=[{"label": "New York", "value": "new-key"}])], profile)
    assert future.actions[0].value == "new-key"


def test_boolean_answers_preserve_type_for_checkboxes(client):
    result = client.post("/api/answers", headers=HEADERS, json={"question": "Open to occasional travel?", "answer": False})
    assert result.status_code == 200
    profile = Profile.model_validate(client.get("/api/state").json()["profile"]).model_dump()
    assert profile["answers"]["Open to occasional travel?"] is False
    plan = plan_form([field("Open to occasional travel?", type="checkbox", required=False, checked=True)], profile)
    assert plan.actions[0].value is False


def test_required_false_checkbox_still_needs_attention():
    candidate = plan_form([field("Open to travel?", type="checkbox")], {}).pending_questions
    assert answer_memory_state({"answers": {"Open to travel?": False}}, [{"id": "held", "pending_questions": candidate}])["pending"]


@pytest.mark.parametrize("payload", [
    {"question": "   ", "answer": "Python"}, {"question": "Question", "answer": "   "},
    {"question": "Question", "answer": ["Python"]}, {"question": "Question", "answer": 42},
    {"question": "Question", "answer": "x" * 4001}, {"question": "Q" * 501, "answer": "a"},
    {"question": "I consent to the terms", "answer": True},
    {"question": "Verification code", "answer": "123456"},
    {"question": "Password", "answer": "secret"},
    {"question": "Question", "answer": "answer", "unexpected": "value"},
])
def test_memory_rejects_invalid_or_unsafe_entries(client, payload):
    assert client.post("/api/answers", headers=HEADERS, json=payload).status_code == 422
    assert client.app.state.store.get("profile", {}).get("answers", {}) == {}


def test_job_scoped_review_requires_the_recorded_question(client):
    store = client.app.state.store
    store.save_job({"id": "held", "status": "needs_attention", "pending_questions": []})
    assert client.post("/api/answers", headers=HEADERS, json={"question": "Unknown?", "answer": "No", "job_id": "held"}).status_code == 409
    assert client.post("/api/answers", headers=HEADERS, json={"question": "Unknown?", "answer": "No", "job_id": "missing"}).status_code == 404


def test_forgetting_answer_stops_future_reuse(client):
    client.post("/api/answers", headers=HEADERS, json={"question": "Favorite language?", "answer": "Python"})
    assert client.request("DELETE", "/api/answers", headers=HEADERS, json={"question": "Favorite language?"}).status_code == 200
    profile = client.get("/api/state").json()["profile"]
    assert plan_form([field("Favorite language?")], profile).blockers


def test_concurrent_saves_keep_each_answer(client):
    def save(index):
        return client.post("/api/answers", headers=HEADERS, json={"question": f"Exact question {index}?", "answer": f"Answer {index}"}).status_code

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert list(workers.map(save, range(8))) == [200] * 8
    answers = client.get("/api/state").json()["profile"]["answers"]
    assert answers == {f"Exact question {index}?": f"Answer {index}" for index in range(8)}


def test_unchanged_save_is_idempotent(client):
    body = {"question": "Favorite language?", "answer": "Python"}
    assert client.post("/api/answers", headers=HEADERS, json=body).json()["changed"]
    store = client.app.state.store
    store.set("approval", {"approved": True, "fingerprint": "unchanged marker"})
    store.set("automation", {"enabled": True})
    response = client.post("/api/answers", headers=HEADERS, json=body)
    assert response.status_code == 200
    assert not response.json()["changed"]
    assert store.get("approval")["approved"]
    assert store.get("automation")["enabled"]


def test_memory_mutations_require_local_header_and_origin(client):
    body = {"question": "Favorite language?", "answer": "Python"}
    assert client.post("/api/answers", json=body).status_code == 403
    assert client.post("/api/answers", headers={**HEADERS, "Origin": "https://elsewhere.test"}, json=body).status_code == 403
