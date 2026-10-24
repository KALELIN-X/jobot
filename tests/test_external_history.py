from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.store import Store, application_key, listing_identity_key


URL = "https://jobs.lever.co/Example/12345678-abcd-1234-abcd-123456789abc/apply"
HEADERS = {"X-JobRadar-Client": "dashboard"}


def job(job_id="first", **changes):
    return {"id": job_id, "status": "new", "company": "Example", "apply_url": URL, "title": "Backend engineer", **changes}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def test_external_history_is_durable_idempotent_and_does_not_use_quota(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    result = store.record_external_application(URL)
    assert result == {"application_key": application_key(job()), "recorded": True, "matched_jobs": 0, "updated_jobs": 0}
    store = Store(path)
    assert store.destination_attempted(job("another-feed"))
    assert store.attempt_count("2026-10-18") == 0
    repeated = store.record_external_application(URL.lower() + "?utm_source=other")
    assert repeated["recorded"] is False
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM destination_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_marks_every_current_match_and_future_imports_without_affecting_other_posts(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job("first"))
    store.save_job(job("second", status="queued", apply_url=URL.lower() + "?ref=feed"))
    store.save_job(job("other", apply_url=URL.replace("12345678-abcd", "87654321-abcd")))
    result = store.record_external_application(URL)
    assert result["matched_jobs"] == result["updated_jobs"] == 2
    assert store.job("first")["status"] == store.job("second")["status"] == "applied_elsewhere"
    assert store.job("other")["status"] == "new"
    assert store.claim_job("first") is None
    store.save_job(job("future", apply_url=URL.replace("/apply", "") + "?source=new-feed"))
    assert store.job("future")["status"] == "applied_elsewhere"
    store.save_job(job("first", title="Refreshed title"))
    assert store.job("first")["status"] == "applied_elsewhere"
    assert store.job("first")["title"] == "Refreshed title"
    assert store.record_external_application(URL)["updated_jobs"] == 0


@pytest.mark.parametrize("status", ["filling", "submitting", "submitted", "uncertain"])
def test_external_history_preserves_active_and_recorded_states(tmp_path, status):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status=status))
    result = store.record_external_application(URL)
    assert result["matched_jobs"] == 1
    assert result["updated_jobs"] == 0
    assert store.job("first")["status"] == status
    store.save_job(job(title="Refreshed title"))
    assert store.job("first")["status"] == status


def test_existing_attempt_evidence_is_preserved_even_if_its_state_was_changed(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status="filling"))
    store.reserve_attempt("first", "2026-10-18", 5)
    store.update_job("first", "failed", error="Check submission outcome")
    result = store.record_external_application(URL)
    assert result["recorded"] is False
    assert result["updated_jobs"] == 0
    saved = store.job("first")
    assert saved["status"] == "failed" and saved["attempted_at"]
    assert saved["error"] == "Check submission outcome"
    assert store.attempt_count("2026-10-18") == 1


def test_history_added_during_filling_blocks_final_reservation(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status="filling"))
    store.record_external_application(URL)
    assert store.job("first")["status"] == "filling"
    with pytest.raises(ValueError, match="another feed record"):
        store.reserve_attempt("first", "2026-10-18", 5)
    assert store.attempt_count("2026-10-18") == 0


def test_concurrent_external_history_records_are_idempotent(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.record_external_application, [URL, URL + "?ref=other"]))
    assert sorted(result["recorded"] for result in results) == [False, True]


@pytest.mark.parametrize("url", ["", "not a URL", URL.replace("https:", "http:"), URL.replace("jobs.lever.co", "evil.test"), URL.replace("jobs.lever.co", "user@jobs.lever.co"), URL.replace("jobs.lever.co", "jobs.lever.co:bad"), URL.replace("12345678-abcd-1234-abcd-123456789abc", "not-a-posting")])
def test_store_rejects_invalid_external_application_urls(tmp_path, url):
    store = Store(tmp_path / "db.sqlite3")
    with pytest.raises(ValueError, match="HTTPS employer"):
        store.record_external_application(url)
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM destination_attempts").fetchone()[0] == 0


def test_api_records_history_and_refuses_requeue(client):
    store = client.app.state.store
    store.save_job(job())
    response = client.post("/api/applied-elsewhere", headers=HEADERS, json={"url": URL})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "application_key": application_key(job()), "recorded": True, "matched_jobs": 1, "updated_jobs": 1}
    assert client.get("/api/state").json()["jobs"][0]["status"] == "applied_elsewhere"
    assert client.post("/api/jobs/first/status", headers=HEADERS, json={"status": "queued"}).status_code == 409
    assert client.post("/api/jobs/first/status", headers=HEADERS, json={"status": "dismissed"}).status_code == 409
    repeated = client.post("/api/applied-elsewhere", headers=HEADERS, json={"url": URL + "?source=repeated"})
    assert repeated.status_code == 200
    assert repeated.json()["recorded"] is False
    assert repeated.json()["updated_jobs"] == 0


@pytest.mark.parametrize("body", [{}, {"url": None}, {"url": 123}, {"url": "https://example.test/job"}, {"url": URL, "unapproved_extra": "extra"}, {"url": "x" * 2001}, {"job_id": " "}, {"job_id": 123}, {"job_id": "x" * 201}, [], None])
def test_api_rejects_malformed_external_history_bodies(client, body):
    assert client.post("/api/applied-elsewhere", headers=HEADERS, json=body).status_code == 422


def test_external_history_api_keeps_local_origin_and_header_guard(client):
    assert client.post("/api/applied-elsewhere", json={"url": URL}).status_code == 403
    for origin in ["https://evil.test", "null"]:
        assert client.post("/api/applied-elsewhere", headers={**HEADERS, "Origin": origin}, json={"url": URL}).status_code == 403


def test_saved_listing_without_application_url_blocks_company_title_reposts_durably(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    original = job(company="Example Robotics", title="Junior Backend Developer", apply_url=None)
    store.save_job(original)
    result = store.record_external_application(job_id="first")
    assert result == {"recorded": True, "matched_jobs": 1, "updated_jobs": 1, "identity_key": listing_identity_key(original), "review_scope": "company_title"}
    assert store.job("first")["status"] == "applied_elsewhere"
    store = Store(path)
    repost = job("new-feed-id", company=" example robotics ", title="Junior  Backend Developer", apply_url=URL.replace("12345678-abcd", "87654321-abcd"), location="Another city")
    store.save_job(repost)
    assert store.job("new-feed-id")["status"] == "applied_elsewhere"
    assert store.destination_attempted(repost)
    assert store.attempt_count("2026-10-18") == 0
    assert store.record_external_application(job_id="new-feed-id")["recorded"] is True  # Newly observed exact URL is also remembered.
    assert store.record_external_application(job_id="new-feed-id")["recorded"] is False
    for candidate in [job("another-employer", company="Other", title=original["title"]), job("another-title", company="Example Robotics", title="Engineering Manager")]:
        store.save_job(candidate)
        assert store.job(candidate["id"])["status"] == "new"


def test_identity_hold_refuses_claim_even_if_status_is_manually_changed(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(apply_url=None))
    store.record_external_application(job_id="first")
    store.update_job("first", "queued")
    assert store.claim_job("first") is None


@pytest.mark.parametrize("status", ["filling", "submitting", "submitted", "uncertain"])
def test_identity_history_preserves_active_evidence_and_blocks_final_reservation(tmp_path, status):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status=status, apply_url=None))
    result = store.record_external_application(job_id="first")
    assert result["updated_jobs"] == 0
    assert store.job("first")["status"] == status
    if status == "filling":
        with pytest.raises(ValueError, match="employer and title"):
            store.reserve_attempt("first", "2026-10-18", 5)
    assert store.attempt_count("2026-10-18") == 0


def test_identity_keys_normalize_case_spacing_and_compatibility_characters_only():
    assert listing_identity_key(job(company="Ｅｘａｍｐｌｅ", title="Backend\tEngineer")) == listing_identity_key(job())
    assert listing_identity_key(job(title="Backend engineer II")) != listing_identity_key(job())
    assert listing_identity_key(job(demo=True)) is None


def test_api_can_record_saved_job_without_a_lever_url(client):
    store = client.app.state.store
    store.save_job(job(apply_url=None))
    response = client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "first"})
    assert response.status_code == 200
    assert response.json()["review_scope"] == "company_title"
    assert response.json()["identity_key"] == listing_identity_key(job())
    assert "application_key" not in response.json()
    assert store.job("first")["status"] == "applied_elsewhere"
    assert "possible reposts" in store.events()[0]["message"]


def test_api_requires_matching_saved_destination_when_both_identifiers_are_provided(client):
    store = client.app.state.store
    store.save_job(job())
    mismatch = client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "first", "url": URL.replace("12345678-abcd", "87654321-abcd")})
    assert mismatch.status_code == 400
    assert not store.destination_attempted(job())
    valid = client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "first", "url": URL.lower() + "?ref=tracking"})
    assert valid.status_code == 200
    assert valid.json()["application_key"] == application_key(job())
    assert valid.json()["identity_key"] == listing_identity_key(job())
    store.save_job(job("missing-url", apply_url=None))
    assert client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "missing-url", "url": URL}).status_code == 400


@pytest.mark.parametrize("changes", [{"demo": True}, {"company": ""}, {"title": ""}])
def test_api_refuses_synthetic_or_unidentifiable_listing_history(client, changes):
    client.app.state.store.save_job(job(**changes))
    assert client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "first"}).status_code == 400


def test_api_reports_missing_listing_without_creating_any_history(client):
    assert client.post("/api/applied-elsewhere", headers=HEADERS, json={"job_id": "missing"}).status_code == 404
    assert not client.app.state.store.destination_attempted(job())
