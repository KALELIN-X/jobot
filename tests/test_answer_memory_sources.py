"""Saved dropdown answers retain exact, employer-bound observed controls."""
import copy

import pytest
from fastapi.testclient import TestClient

from jobradar.answer_memory import answer_memory_state
from jobradar.app import create_app
from jobradar.autofill import plan_form

HEADERS = {"X-JobRadar-Client": "dashboard"}
QUESTION = "Preferred office?"


def choice(label, value=None):
    return {"label": label, "value": label if value is None else value}


def candidate(options, **changes):
    return {"question": QUESTION, "type": "select", "required": True, "options": options,
            "searchable": True, "lookup_required": not bool(options), **changes}


def job(identifier, questions, **changes):
    return {"id": identifier, "company": "Synthetic " + identifier, "title": "Engineer", "status": "needs_attention",
            "form_questions": questions, "pending_questions": [], **changes}


def test_saved_answer_has_distinct_observed_sources_without_universal_options_or_mutation():
    profile = {"answers": {QUESTION: "Boston"}}
    jobs = [job("east", [candidate([choice("Boston", "bos"), choice("New York", "nyc")])]),
            job("west", [candidate([choice("Seattle", "sea"), choice("San Francisco", "sf")])]),
            job("synthetic", [candidate([choice("Fake")])], demo=True)]
    before = copy.deepcopy((profile, jobs))
    state = answer_memory_state(profile, jobs)
    saved = state["saved"][0]
    assert saved["question"] == QUESTION and saved["answer"] == "Boston"
    assert "options" not in saved and state["pending"] == []
    assert [source["job_id"] for source in saved["sources"]] == ["east", "west"]
    assert saved["sources"][0]["options"] == [choice("Boston", "bos"), choice("New York", "nyc")]
    assert saved["sources"][1]["options"] == [choice("Seattle", "sea"), choice("San Francisco", "sf")]
    assert (profile, jobs) == before


def test_current_form_metadata_wins_over_stale_pending_copy_and_dedupes_per_job():
    current = candidate([choice("Boston"), choice("New York")])
    listing = job("east", [current, copy.deepcopy(current)], pending_questions=[candidate([])])
    saved = answer_memory_state({"answers": {QUESTION: "Boston"}}, [listing])["saved"][0]
    assert len(saved["sources"]) == 1
    assert saved["sources"][0]["options"] == current["options"]
    assert saved["sources"][0]["lookup_required"] is False
    assert "answer_kind" not in saved
    assert answer_memory_state({"answers": {QUESTION: "Boston"}}, [listing])["pending"] == []


def test_saved_lookup_source_keeps_search_metadata_without_pretending_to_be_an_option():
    listing = job("lookup", [candidate([], minimum_query_length=3)])
    saved = answer_memory_state({"answers": {QUESTION: "Boston"}}, [listing])["saved"][0]
    assert saved["answer_kind"] == "lookup_query"
    assert saved["sources"][0]["searchable"] is saved["sources"][0]["lookup_required"] is True
    assert saved["sources"][0]["minimum_query_length"] == 3 and saved["sources"][0]["options"] == []


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as result:
        yield result


def test_job_bound_save_uses_all_form_questions_even_when_no_longer_pending(client):
    store = client.app.state.store
    store.save_job(job("east", [candidate([choice("Boston", "bos"), choice("New York", "nyc")])]))
    response = client.post("/api/answers", headers=HEADERS, json={"job_id": "east", "question": QUESTION, "answer": "nyc"})
    assert response.status_code == 200 and response.json()["answer"] == "New York"
    assert store.get("profile")["answers"] == {QUESTION: "New York"}
    assert store.job("east")["pending_questions"] == []
    state = client.get("/api/state").json()
    assert state["answer_memory"]["saved"][0]["sources"][0]["job_id"] == "east"


@pytest.mark.parametrize("kind", ["select", "radio"])
def test_choice_confirmation_rejects_reused_value_with_changed_label_without_mutation(client, kind):
    store = client.app.state.store
    store.set("profile", {"answers": {QUESTION: "Boston"}})
    store.set("approval", {"approved": True})
    store.set("automation", {"enabled": True})
    store.save_job(job("east", [candidate([choice("Seattle", "office-1")], type=kind)]))
    before = {key: copy.deepcopy(store.get(key)) for key in ("profile", "approval", "automation")}
    original_job, original_events = copy.deepcopy(store.job("east")), store.events()
    # The stale page displayed Boston for office-1 before a form refresh.
    response = client.post("/api/answers", headers=HEADERS, json={
        "job_id": "east", "question": QUESTION, "answer": "office-1", "choice_label": "Boston"})
    assert response.status_code == 409
    assert {key: store.get(key) for key in before} == before
    assert store.job("east") == original_job and store.events() == original_events
    current = client.post("/api/answers", headers=HEADERS, json={
        "job_id": "east", "question": QUESTION, "answer": "office-1", "choice_label": "Seattle"})
    assert current.status_code == 200, current.text
    assert current.json()["answer"] == "Seattle"
    assert store.get("profile")["answers"][QUESTION] == "Seattle"
    assert not store.get("approval")["approved"] and not store.get("automation")["enabled"]


@pytest.mark.parametrize("metadata,answer,label", [
    (candidate([], type="text"), "Boston", "Boston"),
    (candidate([], type="checkbox"), True, "Yes"),
    (candidate([]), "Boston", "Boston"),
    (candidate([choice("Boston")], multiple=True), "Boston", "Boston"),
    (candidate([{**choice("Boston"), "disabled": True}]), "Boston", "Boston"),
    (candidate([choice("Boston")]), "Boston", " Boston"),
])
def test_choice_label_requires_current_single_enabled_exact_option(client, metadata, answer, label):
    client.app.state.store.save_job(job("east", [metadata]))
    response = client.post("/api/answers", headers=HEADERS, json={
        "job_id": "east", "question": QUESTION, "answer": answer, "choice_label": label})
    assert response.status_code == 409
    assert not client.app.state.store.get("profile", {}).get("answers")


def test_choice_confirmation_requires_job_bound_metadata(client):
    response = client.post("/api/answers", headers=HEADERS, json={
        "question": QUESTION, "answer": "Boston", "choice_label": "Boston"})
    assert response.status_code == 409
    assert not client.app.state.store.get("profile", {}).get("answers")


@pytest.mark.parametrize("label", ["", "x" * 501, True, 123])
def test_choice_label_model_is_a_bounded_strict_string(client, label):
    response = client.post("/api/answers", headers=HEADERS, json={
        "job_id": "east", "question": QUESTION, "answer": "Boston", "choice_label": label})
    assert response.status_code == 422


def test_form_question_is_authoritative_and_other_employer_options_do_not_validate(client):
    store = client.app.state.store
    store.save_job(job("east", [candidate([choice("Boston"), choice("New York")])], pending_questions=[candidate([choice("Seattle")])]))
    store.save_job(job("west", [candidate([choice("Seattle")])]))
    wrong = client.post("/api/answers", headers=HEADERS, json={"job_id": "east", "question": QUESTION, "answer": "Seattle"})
    assert wrong.status_code == 422
    right = client.post("/api/answers", headers=HEADERS, json={"job_id": "west", "question": QUESTION, "answer": "Seattle"})
    assert right.status_code == 200
    assert len(client.get("/api/state").json()["answer_memory"]["saved"][0]["sources"]) == 2


def test_identical_form_question_copies_are_deduplicated_for_job_bound_save(client):
    current = candidate([choice("Boston")])
    client.app.state.store.save_job(job("east", [current, copy.deepcopy(current)], pending_questions=[copy.deepcopy(current)]))
    result = client.post("/api/answers", headers=HEADERS, json={"job_id": "east", "question": QUESTION, "answer": "Boston"})
    assert result.status_code == 200


def test_conflicting_current_control_types_require_manual_review(client):
    client.app.state.store.set("profile", {"answers": {QUESTION: "Boston"}})
    client.app.state.store.save_job(job("east", [candidate([choice("Boston")]), candidate([], type="text", searchable=False, lookup_required=False)]))
    response = client.post("/api/answers", headers=HEADERS, json={"job_id": "east", "question": QUESTION, "answer": "Boston"})
    assert response.status_code == 409
    source = client.get("/api/state").json()["answer_memory"]["saved"][0]["sources"][0]
    assert source["conflicting_controls"] is True and source["options"] == [] and source["type"] == "unsupported"


def test_planner_preserves_same_label_controls_with_different_choices_or_types(client):
    first = {"id": "first", "label": QUESTION, "type": "select", "tag": "select", "required": True,
             "options": [choice("Boston")]}
    second = {**first, "id": "second", "options": [choice("Seattle")]}
    written = {**first, "id": "third", "type": "text", "tag": "input", "options": []}
    # A repeated identical control is the only safe duplicate to collapse.
    identical = {**first, "id": "fourth"}
    plan = plan_form([first, second, written, identical], {})
    assert len(plan.pending_questions) == 3
    assert len({item["id"] for item in plan.pending_questions}) == 3
    assert [(item["type"], item["options"]) for item in plan.pending_questions] == [
        ("select", [choice("Boston")]), ("select", [choice("Seattle")]), ("text", [])]
    client.app.state.store.save_job(job("ambiguous", plan.pending_questions, pending_questions=plan.pending_questions))
    pending = client.get("/api/state").json()["answer_memory"]["pending"]
    assert len(pending) == 1 and pending[0]["conflicting_controls"] is True and pending[0]["type"] == "unsupported"
    response = client.post("/api/answers", headers=HEADERS,
                           json={"job_id": "ambiguous", "question": QUESTION, "answer": "Boston"})
    assert response.status_code == 409
    assert not client.app.state.store.get("profile", {}).get("answers")
