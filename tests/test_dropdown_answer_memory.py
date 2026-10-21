"""Confirmed lookup text starts a search; only real choices can fill a select."""
import copy

import pytest
from fastapi.testclient import TestClient

from jobradar.answer_memory import answer_memory_state
from jobradar.app import create_app
from jobradar.autofill import lookup_query_hash, plan_form
from test_greenhouse_forms import PROFILE, employer, run_fixture

HEADERS = {"X-JobRadar-Client": "dashboard"}
QUESTION = "Location (City)"


def city(**changes):
    return {"id": "city", "name": "city", "label": QUESTION, "type": "select", "tag": "input", "combobox": True,
            "visible": True, "required": True, "searchable": True, "lookup_required": True,
            "minimum_query_length": 3, "options": [], **changes}


def held_job(candidate):
    return {"id": "held-city", "company": "Synthetic employer", "title": "Engineer", "status": "needs_attention",
            "pending_questions": [candidate]}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as result:
        yield result


def test_pending_searchable_field_is_distinct_from_an_unsupported_empty_select():
    plan = plan_form([city()], {})
    assert plan.blockers and not plan.actions
    candidate = plan.pending_questions[0]
    assert candidate["type"] == "select" and candidate["options"] == []
    assert candidate["searchable"] is candidate["lookup_required"] is True
    assert candidate["minimum_query_length"] == 3
    assert candidate["reason"] == "lookup_query_required"
    ordinary = plan_form([city(combobox=False, searchable=False, lookup_required=False, tag="select")], {})
    assert "searchable" not in ordinary.pending_questions[0]


def test_confirmed_query_is_provisional_persists_and_revokes_approval_without_running(client, tmp_path):
    store = client.app.state.store
    candidate = plan_form([city()], {}).pending_questions[0]
    store.save_job(held_job(candidate))
    original = copy.deepcopy(store.job("held-city"))
    store.set("approval", {"approved": True})
    store.set("automation", {"enabled": True})
    response = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": "Boston", "job_id": "held-city"})
    assert response.status_code == 200, response.text
    assert response.json()["answer_kind"] == "lookup_query" and response.json()["choice_validation_required"] is True
    assert not store.get("approval")["approved"] and not store.get("automation")["enabled"]
    saved = store.job("held-city")
    # Saving a confirmed answer refreshes eligibility/ranking metadata. It must
    # not change queue membership, replace the observed question, or reserve a
    # submission merely because that refresh updated the row timestamp.
    for key in ("id", "company", "title", "status", "pending_questions", "form_questions", "attempted_at", "confirmation", "applied_at"):
        assert saved.get(key) == original.get(key)
    assert saved["status"] == "needs_attention" and saved["attempted_at"] is None
    assert not store.destination_attempted(saved)
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM destination_attempts").fetchone()[0] == 0
    assert not any(event["type"] in {"run_started", "application_progress"} for event in store.events())
    state = client.get("/api/state").json()
    assert state["profile"]["answers"] == {QUESTION: "Boston"}
    assert state["answer_memory"]["pending"] == []
    saved_item = state["answer_memory"]["saved"][0]
    assert saved_item["question"] == QUESTION and saved_item["answer"] == "Boston" and saved_item["answer_kind"] == "lookup_query"
    assert saved_item["sources"] == [{"job_id": "held-city", "company": "Synthetic employer", "title": "Engineer", "type": "select",
                                      "required": True, "options": [], "searchable": True, "lookup_required": True, "minimum_query_length": 3}]
    # A query in memory cannot fill the select until the browser supplies choices.
    plan = plan_form([city()], state["profile"])
    assert plan.blockers and not plan.actions
    assert plan.pending_questions[0]["lookup_query_hash"] == lookup_query_hash("Boston")
    with TestClient(create_app(tmp_path)) as reopened:
        assert reopened.get("/api/state").json()["profile"]["answers"] == {QUESTION: "Boston"}


@pytest.mark.parametrize("answer", ["Bo", "x" * 241, True, False])
def test_search_query_requires_written_bounded_text_and_explicit_minimum(client, answer):
    client.app.state.store.save_job(held_job(plan_form([city()], {}).pending_questions[0]))
    response = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": answer, "job_id": "held-city"})
    assert response.status_code == 422
    assert client.app.state.store.get("profile", {}).get("answers", {}) == {}


@pytest.mark.parametrize("changes", [
    {"searchable": False}, {"lookup_required": False}, {"searchable": None},
    {"options": [{"label": "Cambridge, Massachusetts, United States", "value": "Cambridge, Massachusetts, United States"}], "lookup_required": False},
])
def test_arbitrary_text_cannot_be_saved_for_nonsearchable_or_populated_selects(client, changes):
    candidate = plan_form([city(**changes)], {}).pending_questions[0]
    client.app.state.store.save_job(held_job(candidate))
    response = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": "Boston", "job_id": "held-city"})
    assert response.status_code == 422


def test_failed_lookup_remains_pending_and_cannot_be_hidden_by_the_same_saved_query(client):
    profile = {"answers": {QUESTION: "Boston"}}
    candidate = plan_form([city()], profile).pending_questions[0]
    assert candidate["reason"] == "lookup_no_exact_choice"
    assert "Boston" not in str(candidate)
    job = held_job(candidate)
    state = answer_memory_state(profile, [job])
    assert state["pending"][0]["question"] == QUESTION
    client.app.state.store.set("profile", profile)
    client.app.state.store.save_job(job)
    unchanged = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": " Boston ", "job_id": "held-city"})
    assert unchanged.status_code == 422 and "exact available choice" in unchanged.json()["detail"]
    changed = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": "Boston, Massachusetts", "job_id": "held-city"})
    assert changed.status_code == 200
    assert not client.get("/api/state").json()["answer_memory"]["pending"]


def test_real_returned_choices_require_exact_choice_after_lookup_and_never_choose_first(client):
    options = [{"label": "Boston, Massachusetts, United States", "value": "Boston, Massachusetts, United States"},
               {"label": "Boston, England, United Kingdom", "value": "Boston, England, United Kingdom"}]
    queried = city(options=options, lookup_required=False)
    profile = {"answers": {QUESTION: "Boston"}}
    plan = plan_form([queried], profile)
    assert plan.blockers and not plan.actions
    candidate = plan.pending_questions[0]
    assert candidate["lookup_required"] is False and candidate["options"] == options
    client.app.state.store.set("profile", profile)
    client.app.state.store.save_job(held_job(candidate))
    assert client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": "Boston", "job_id": "held-city"}).status_code == 422
    selected = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": options[0]["label"], "job_id": "held-city"})
    assert selected.status_code == 200 and "answer_kind" not in selected.json()
    confirmed = client.get("/api/state").json()["profile"]
    filled = plan_form([queried], confirmed)
    assert not filled.blockers and [(action.kind, action.value) for action in filled.actions] == [("combobox", options[0]["label"])]


def test_optional_multi_select_remains_manual_even_with_saved_query_or_known_choices():
    item = city(required=False, multiple=True, options=[{"label": "Boston", "value": "Boston"}])
    plan = plan_form([item], {"answers": {QUESTION: "Boston"}})
    assert not plan.blockers
    assert not plan.actions and not plan.pending_questions


def test_known_country_and_yes_no_choices_are_ordinary_exact_answers(client):
    for index, (question, answer, options) in enumerate([
        ("Country", "United States +1", [{"label": "United States +1", "value": "United States +1"}]),
        ("Are you authorized to work in the United States?", True, [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]),
    ]):
        item = city(label=question, options=options, lookup_required=False)
        candidate = plan_form([item], {}).pending_questions[0]
        job = {**held_job(candidate), "id": f"held-choice-{index}"}
        client.app.state.store.save_job(job)
        response = client.post("/api/answers", headers=HEADERS, json={"question": question, "answer": answer, "job_id": job["id"]})
        assert response.status_code == 200 and "answer_kind" not in response.json()


def test_real_browser_query_discovers_choices_then_only_confirmed_exact_choice_submits(employer, client, tmp_path):
    initial, reservations, receipts, _ = run_fixture(employer, tmp_path, "search", PROFILE)
    assert initial["status"] == "needs_attention" and not reservations and not receipts
    candidate = next(question for question in initial["pending_questions"] if question["question"] == QUESTION)
    assert candidate["searchable"] is candidate["lookup_required"] is True and candidate["options"] == []
    client.app.state.store.save_job(held_job(candidate))
    saved = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": "Boston", "job_id": "held-city"})
    assert saved.status_code == 200 and saved.json()["choice_validation_required"] is True
    lookup_profile = {**PROFILE, "answers": {**PROFILE["answers"], QUESTION: "Boston"}}
    discovered, reservations, receipts, _ = run_fixture(employer, tmp_path, "search", lookup_profile)
    assert discovered["status"] == "needs_attention" and not reservations and not receipts
    real_choice = next(question for question in discovered["pending_questions"] if question["question"] == QUESTION)
    assert real_choice["lookup_required"] is False
    assert real_choice["options"] == [{"label": "Boston, Massachusetts, United States", "value": "Boston, Massachusetts, United States"}]
    client.app.state.store.update_job("held-city", "needs_attention", pending_questions=[real_choice])
    confirmed = client.post("/api/answers", headers=HEADERS, json={"question": QUESTION, "answer": real_choice["options"][0]["label"], "job_id": "held-city"})
    assert confirmed.status_code == 200 and "answer_kind" not in confirmed.json()
    approved_profile = {**PROFILE, "answers": {**PROFILE["answers"], QUESTION: confirmed.json()["answer"]}}
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "search", approved_profile)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"] and len(receipts) == 1
    assert receipts[0]["city"] == b"Boston, Massachusetts, United States"
