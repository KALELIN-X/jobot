"""Question endpoints read mocked public fields and preserve private state/history."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.models import Profile, Rules
from test_destinations import fetched

HEADERS = {"X-JobRadar-Client": "dashboard"}
CITY = "Location (City)"
COUNTRY = "Country"
CANONICAL_CITY = "Boston, Massachusetts, United States"


def field(label, *, options=None, **changes):
    return {"id": label, "label": label, "name": label, "type": "select", "tag": "input", "combobox": True,
            "visible": True, "required": True, "searchable": True, "lookup_required": not bool(options),
            "options": options or [], **changes}


def option(label):
    return {"label": label, "value": label}


def recorded(label, options=None, **changes):
    return {"question": label, "key": label, "type": "select", "required": True, "options": options or [],
            "searchable": True, "lookup_required": not bool(options), **changes}


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def unexpected_application(*args, **kwargs):
        pytest.fail("Question inspection must never launch the application worker")
    monkeypatch.setattr("jobradar.autofill.run_application", unexpected_application)
    app = create_app(tmp_path)
    store = app.state.store
    store.set("profile", Profile(name="Synthetic Candidate", email="candidate@example.test", answers={COUNTRY: "United States +1"}).model_dump())
    store.set("rules", Rules().model_dump())
    store.set("approval", {"approved": True, "fingerprint": "synthetic saved approval"})
    store.set("automation", {"enabled": False})
    store.set("resume", {"loaded": False, "filename": "synthetic.txt"})
    listing = {**fetched(), "status": "needs_attention", "form_questions": [recorded(COUNTRY, [option("United States +1")]), recorded(CITY)],
               "pending_questions": [recorded(CITY)], "questions_refreshed_at": "2020-01-01T00:00:00+00:00"}
    store.save_job(listing)
    with TestClient(app) as result:
        yield result


def endpoint(suffix, job_id="synthetic-feed-row"):
    return f"/api/jobs/{job_id}/questions/{suffix}"


def private_snapshot(store):
    values = {key: copy.deepcopy(store.get(key)) for key in ("profile", "rules", "approval", "resume", "automation", "stop_requested")}
    with store.connection() as connection:
        values["attempts"] = [tuple(row) for row in connection.execute("SELECT * FROM attempts ORDER BY job_id")]
        values["destinations"] = [tuple(row) for row in connection.execute("SELECT * FROM destination_attempts ORDER BY application_key")]
    return values


def test_whole_form_refresh_saves_all_observed_choices_and_only_unresolved_questions(client, monkeypatch):
    store = client.app.state.store
    historical = {**fetched(), "id": "previous-submission", "status": "filling",
                  "apply_url": fetched()["apply_url"].replace("1234567009", "1234567010")}
    store.save_job(historical)
    store.reserve_attempt("previous-submission", "2026-10-17", None)
    store.update_job("previous-submission", "submitted", confirmation="Synthetic historical receipt")
    historical_before = store.job("previous-submission")
    before = private_snapshot(store)
    assert before["attempts"] and before["destinations"]
    prior = store.job("synthetic-feed-row")
    calls = []
    async def inspect(job, **kwargs):
        calls.append((job["id"], kwargs))
        return {"status": "inspected", "fields": [field(COUNTRY, options=[option("United States +1"), option("Canada +1")]),
                                                  field(CITY, minimum_query_length=3),
                                                  field("Preferred editor?", type="text", combobox=False, required=False)]}
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", inspect)
    response = client.post(endpoint("refresh"), headers=HEADERS)
    assert response.status_code == 200, response.text
    assert calls == [("synthetic-feed-row", {"question": None, "query": None})]
    assert response.json()["questions"] == 3 and response.json()["pending"] == 2
    updated = store.job("synthetic-feed-row")
    assert {question["question"] for question in updated["form_questions"]} == {COUNTRY, CITY, "Preferred editor?"}
    assert {question["question"] for question in updated["pending_questions"]} == {CITY, "Preferred editor?"}
    assert updated["questions_refreshed_at"] != prior["questions_refreshed_at"]
    assert updated["status"] == prior["status"] and updated["attempted_at"] is None
    assert private_snapshot(store) == before
    assert store.job("previous-submission") == historical_before
    assert store.events()[0]["type"] == "questions"
    assert "candidate@example.test" not in json.dumps(store.events())


def test_targeted_lookup_merges_only_requested_question_and_does_not_save_query_as_answer(client, monkeypatch):
    store = client.app.state.store
    old_country = recorded(COUNTRY, [option("United States +1"), option("Canada +1")])
    old_other = recorded("Preferred shift?", [option("Day"), option("Night")])
    store.update_job("synthetic-feed-row", "needs_attention", form_questions=[old_country, old_other, recorded(CITY)],
                     pending_questions=[old_other, recorded(CITY)])
    before = private_snapshot(store)
    query = "Explicit locality search"
    async def inspect(job, **kwargs):
        assert kwargs == {"question": CITY, "query": query}
        return {"status": "inspected", "fields": [field(COUNTRY), field(CITY, options=[option(CANONICAL_CITY)], minimum_query_length=3)]}
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", inspect)
    response = client.post(endpoint("options"), headers=HEADERS, json={"question": CITY, "query": query})
    assert response.status_code == 200 and response.json()["options"] == [option(CANONICAL_CITY)]
    assert response.json()["lookup_required"] is False
    updated = store.job("synthetic-feed-row")
    assert next(item for item in updated["form_questions"] if item["question"] == COUNTRY) == old_country
    assert next(item for item in updated["form_questions"] if item["question"] == "Preferred shift?") == old_other
    assert next(item for item in updated["pending_questions"] if item["question"] == "Preferred shift?") == old_other
    assert {item["question"] for item in updated["pending_questions"]} == {CITY, "Preferred shift?"}
    assert CITY not in store.get("profile")["answers"]
    assert query not in json.dumps(store.events())
    assert private_snapshot(store) == before


@pytest.mark.parametrize("targeted", [False, True])
def test_inspection_failure_does_not_overwrite_recorded_metadata_or_private_state(client, monkeypatch, targeted):
    store = client.app.state.store
    prior, private = store.job("synthetic-feed-row"), private_snapshot(store)
    async def unavailable(*args, **kwargs):
        return {"status": "needs_attention", "message": "Public form unavailable.", "fields": []}
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", unavailable)
    response = client.post(endpoint("options" if targeted else "refresh"), headers=HEADERS,
                           **({"json": {"question": CITY, "query": "Boston"}} if targeted else {}))
    assert response.status_code == 422
    assert store.job("synthetic-feed-row") == prior and private_snapshot(store) == private
    assert store.events() == []


def test_targeted_question_missing_from_current_form_does_not_clear_other_questions(client, monkeypatch):
    store = client.app.state.store
    prior = store.job("synthetic-feed-row")
    async def no_city(*args, **kwargs):
        return {"status": "inspected", "fields": [field(COUNTRY, options=[option("United States +1")])]}
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", no_city)
    response = client.post(endpoint("options"), headers=HEADERS, json={"question": CITY, "query": "Boston"})
    assert response.status_code == 422 and store.job("synthetic-feed-row") == prior


@pytest.mark.parametrize("status", ["active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"])
def test_active_or_protected_status_refuses_lookup_without_browser(client, monkeypatch, status):
    store = client.app.state.store
    store.update_job("synthetic-feed-row", status)
    prior = store.job("synthetic-feed-row")
    async def forbidden(*args, **kwargs):
        pytest.fail("A protected application must not launch form inspection")
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", forbidden)
    assert client.post(endpoint("refresh"), headers=HEADERS).status_code == 409
    assert client.post(endpoint("options"), headers=HEADERS, json={"question": CITY, "query": "Boston"}).status_code == 409
    assert store.job("synthetic-feed-row") == prior


def test_attempted_failed_application_refuses_lookup_and_preserves_ledger(client, monkeypatch):
    store = client.app.state.store
    store.update_job("synthetic-feed-row", "filling")
    store.reserve_attempt("synthetic-feed-row", "2026-10-18", None)
    store.update_job("synthetic-feed-row", "failed")
    prior, private = store.job("synthetic-feed-row"), private_snapshot(store)
    async def forbidden(*args, **kwargs):
        pytest.fail("An attempted application must not launch form inspection")
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", forbidden)
    assert client.post(endpoint("refresh"), headers=HEADERS).status_code == 409
    assert store.job("synthetic-feed-row") == prior and private_snapshot(store) == private


@pytest.mark.parametrize("changes", [{"closed": True}, {"destination_provenance": None}, {"demo": True}])
def test_closed_unverified_or_synthetic_record_refuses_lookup(client, monkeypatch, changes):
    store = client.app.state.store
    store.update_job("synthetic-feed-row", "needs_attention", **changes)
    async def forbidden(*args, **kwargs):
        pytest.fail("An unavailable destination must not launch form inspection")
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", forbidden)
    assert client.post(endpoint("refresh"), headers=HEADERS).status_code == 409
    assert client.post(endpoint("refresh", "not-present"), headers=HEADERS).status_code == 404


@pytest.mark.parametrize("change", ["active", "attempted", "closed", "unverified", "destination"])
def test_changed_application_during_inspection_does_not_receive_stale_question_update(client, monkeypatch, change):
    store = client.app.state.store
    prior = store.job("synthetic-feed-row")
    async def changed(*args, **kwargs):
        if change == "attempted":
            store.update_job("synthetic-feed-row", "filling")
            store.reserve_attempt("synthetic-feed-row", "2026-10-18", None)
            store.update_job("synthetic-feed-row", "failed")
        else:
            kwargs = {"closed": True} if change == "closed" else {"destination_provenance": None} if change == "unverified" else {"apply_url": prior["apply_url"].replace("1234567009", "1234567010")} if change == "destination" else {}
            store.update_job("synthetic-feed-row", "filling" if change == "active" else "needs_attention", **kwargs)
        return {"status": "inspected", "fields": [field(CITY, options=[option(CANONICAL_CITY)])]}
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", changed)
    response = client.post(endpoint("refresh"), headers=HEADERS)
    assert response.status_code == 409
    after = store.job("synthetic-feed-row")
    assert after["form_questions"] == prior["form_questions"] and after["pending_questions"] == prior["pending_questions"]
    assert after["questions_refreshed_at"] == prior["questions_refreshed_at"]


@pytest.mark.parametrize("body", [{}, {"question": " "}, {"question": CITY, "query": "x" * 241}, {"question": "q" * 501},
                                  {"question": CITY, "query": "Bos\u0000ton"}, {"question": CITY, "query": None},
                                  {"question": CITY, "query": ["Boston"]}, {"question": CITY, "unexpected": True}])
def test_lookup_model_rejects_invalid_body_before_browser(client, monkeypatch, body):
    before = client.app.state.store.job("synthetic-feed-row")
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid request bodies must not open a browser")
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", forbidden)
    assert client.post(endpoint("options"), headers=HEADERS, json=body).status_code == 422
    assert client.app.state.store.job("synthetic-feed-row") == before


def test_question_routes_require_same_origin_dashboard_header(client, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Untrusted requests must not open a browser")
    monkeypatch.setattr("jobradar.question_lookup.inspect_questions", forbidden)
    assert client.post(endpoint("refresh")).status_code == 403
    assert client.post(endpoint("options"), headers={**HEADERS, "Origin": "https://untrusted.example"}, json={"question": CITY, "query": "Boston"}).status_code == 403
