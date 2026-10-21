"""Onboarding uses synthetic local candidates; no employers or private DBs."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.approval import fingerprint
from test_workflow import HEADERS, PROFILE, add_job, configure_and_approve


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as value:
        yield value


def save_step(client, step, **values):
    snapshot = client.get("/api/onboarding").json()
    return client.post("/api/onboarding", headers=HEADERS,
                       json={"step": step, "expected_revision": snapshot["revision"], **values})


def test_reading_legacy_profile_is_not_a_migration_or_an_application(client):
    legacy = {"name": "Existing Candidate", "answers": {"Favorite editor?": "Example editor"}}
    client.app.state.store.set("profile", legacy)
    snapshot = client.get("/api/onboarding").json()
    assert snapshot["profile"] == legacy
    assert snapshot["application_profile"]["facts"]["us_citizen"] is None
    assert not snapshot["completion"]["complete"]
    assert client.app.state.store.get("profile") == legacy
    assert client.app.state.store.jobs() == []
    assert [step["id"] for step in snapshot["catalog"]] == [
        "identity", "work_authorization", "education", "experience", "preferences", "review"]


def test_step_merge_preserves_old_answers_and_explicit_no(client):
    client.post("/api/profile", headers=HEADERS, json={**PROFILE, "answers": {"Favorite editor?": "Example"}})
    result = save_step(client, "work_authorization", facts={"sponsorship_now": False, "sponsorship_future": None})
    assert result.status_code == 200, result.text
    saved = result.json()["profile"]
    assert saved["name"] == PROFILE["name"]
    assert saved["answers"] == {"Favorite editor?": "Example"}
    assert saved["application_profile"]["facts"]["sponsorship_now"] is False
    assert saved["application_profile"]["facts"]["sponsorship_future"] is None
    assert saved["application_profile"]["confirmed_at"]["facts.sponsorship_now"]
    assert result.json()["approval_required"]


def test_stale_tab_cannot_overwrite_newer_details(client):
    old = client.get("/api/onboarding").json()
    assert save_step(client, "identity", profile_patch={"name": "Fresh name"}).status_code == 200
    before = deepcopy(client.app.state.store.get("profile"))
    stale = client.post("/api/onboarding", headers=HEADERS, json={
        "step": "identity", "expected_revision": old["revision"], "profile_patch": {"name": "Stale name"}})
    assert stale.status_code == 409
    assert client.app.state.store.get("profile") == before


@pytest.mark.parametrize("body", [
    {"step": "identity", "facts": {"us_citizen": True}},
    {"step": "identity", "profile_patch": {"answers": {"unknown": "yes"}}},
    {"step": "identity", "employment": []},
    {"step": "work_authorization", "profile_patch": {"work_authorized": "yes"}},
    {"step": "work_authorization", "facts": {"sponsorship_now": "false"}},
    {"step": "preferences", "facts": {"domestic_travel_percent": True}},
    {"step": "identity", "complete": True},
])
def test_invalid_step_or_fact_never_changes_profile(client, body):
    before = client.app.state.store.get("profile")
    result = save_step(client, **body)
    assert result.status_code == 422, result.text
    assert client.app.state.store.get("profile") == before


def test_education_record_status_and_gpa_stay_separate(client):
    records = [{"id": "completed", "institution": "Example College", "degree": "BS", "status": "completed",
                "start_month": "2019-09", "end_month": "2023-05", "gpa": 3.2, "gpa_scale": 4.0},
               {"id": "incoming", "institution": "Example University", "degree": "MS", "status": "incoming",
                "start_month": "2027-01", "end_month": "2028-05"}]
    result = save_step(client, "education", education=records)
    assert result.status_code == 200, result.text
    saved = result.json()["application_profile"]["education"]
    assert saved[0]["gpa"] == 3.2 and saved[1]["gpa"] is None
    before = deepcopy(client.app.state.store.get("profile"))
    records[1].update(gpa=4.0, gpa_scale=4.0)
    assert save_step(client, "education", education=records).status_code == 422
    assert client.app.state.store.get("profile") == before


def test_walkthrough_metadata_and_noop_edits_do_not_revoke_approval(client):
    configure_and_approve(client)
    client.app.state.store.set("automation", {"enabled": True})
    before = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    result = save_step(client, "identity", profile_patch={"name": PROFILE["name"]})
    assert result.status_code == 200
    assert not result.json()["changed"]
    after = client.get("/api/state").json()
    assert after["approval"]["approved"]
    assert after["approval"]["candidate_fingerprint"] == before
    assert after["automation"]["enabled"]
    assert "identity" in after["onboarding"]["completed_steps"]


def test_material_fact_change_revokes_approval_without_touching_attempts(client):
    configure_and_approve(client)
    add_job(client, "already-done")
    store = client.app.state.store
    store.claim_job("already-done")
    store.reserve_attempt("already-done", "2026-10-20", None)
    store.update_job("already-done", "submitted", confirmation={"message": "Synthetic receipt"})
    original = store.job("already-done")
    result = save_step(client, "preferences", facts={"willing_to_relocate": False})
    assert result.status_code == 200
    assert result.json()["approval_required"]
    assert not client.get("/api/state").json()["approval"]["approved"]
    assert store.job("already-done")["status"] == "submitted"
    assert store.job("already-done")["attempted_at"] == original["attempted_at"]
    assert store.job("already-done")["confirmation"] == original["confirmation"]
    assert store.destination_attempted(original)


def test_legacy_profile_editor_cannot_erase_structured_records(client):
    record = {"id": "past", "company": "Example Labs", "title": "Intern", "kind": "internship",
              "start_month": "2024-07", "end_month": "2024-09", "current": False}
    assert save_step(client, "experience", employment=[record]).status_code == 200
    response = client.post("/api/profile", headers=HEADERS, json={"name": "Updated name", "email": "a@example.test"})
    assert response.status_code == 200
    assert client.get("/api/onboarding").json()["application_profile"]["employment"][0]["start_month"] == "2024-07"


def test_finishing_requires_reviewed_essentials_not_invented_optional_facts(client):
    assert save_step(client, "review", complete=True).status_code == 422
    assert save_step(client, "identity", profile_patch={"name": "Alex Example", "first_name": "Alex",
                                                      "last_name": "Example", "email": "alex@example.test"}).status_code == 200
    client.post("/api/resume", headers=HEADERS, files={"file": ("resume.txt", b"Synthetic local resume", "text/plain")})
    result = save_step(client, "review", complete=True)
    assert result.status_code == 200
    assert result.json()["completion"]["complete"]
    assert result.json()["application_profile"]["facts"]["us_citizen"] is None
    assert not client.get("/api/state").json()["approval"]["approved"]


def test_profile_editor_rejects_stale_structured_write_and_preserves_new_records(client):
    old = client.get("/api/state").json()
    record = {"id": "past", "company": "Example Labs", "title": "Intern", "kind": "internship",
              "start_month": "2024-07", "end_month": "2024-09", "current": False}
    assert save_step(client, "experience", employment=[record]).status_code == 200
    saved = deepcopy(client.app.state.store.get("profile"))
    response = client.post("/api/profile", headers=HEADERS, json={
        **old["profile"], "expected_revision": old["profile_revision"], "name": "Stale editor"})
    assert response.status_code == 409
    assert client.app.state.store.get("profile") == saved
    fresh = client.get("/api/state").json()
    response = client.post("/api/profile", headers=HEADERS, json={
        **fresh["profile"], "expected_revision": fresh["profile_revision"], "name": "Fresh editor"})
    assert response.status_code == 200, response.text
    assert "expected_revision" not in client.app.state.store.get("profile")
    assert client.app.state.store.get("profile")["application_profile"]["employment"] == saved["application_profile"]["employment"]


def test_full_structured_profile_write_requires_a_revision(client):
    before = deepcopy(client.app.state.store.get("profile"))
    response = client.post("/api/profile", headers=HEADERS, json={"application_profile": {}})
    assert response.status_code == 409
    assert client.app.state.store.get("profile") == before


def test_confirmation_metadata_does_not_change_material_fingerprint():
    before = {"name": "Example"}
    after = {**before, "application_profile": {"completed_steps": ["identity"],
                                             "confirmed_at": {"name": "2026-10-20T12:00:00+00:00"}}}
    assert fingerprint(before, {}, {}) == fingerprint(after, {}, {})
