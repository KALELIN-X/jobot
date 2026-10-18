import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobradar.store import Store, application_key, listing_identity_key

URL = "https://job-boards.greenhouse.io/example/jobs/1234567009"


def job(identifier="first", **changes):
    return {"id": identifier, "company": "Example", "title": "Junior Backend Engineer", "apply_url": URL, "ats": "greenhouse", "status": "new", **changes}


def test_repeated_posting_across_feed_ids_host_variants_and_days_is_not_retried(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.save_job(job(status="filling"))
    store.reserve_attempt("first", "2026-10-18", 5)
    store.update_job("first", "uncertain")
    store = Store(path)
    duplicate = job("different-feed-id", status="filling", apply_url=URL.replace("job-boards.greenhouse.io", "boards.eu.greenhouse.io") + "?gh_src=another")
    store.save_job(duplicate)
    assert store.destination_attempted(duplicate)
    with pytest.raises(ValueError, match="another feed record"):
        store.reserve_attempt("different-feed-id", "2026-10-19", 5)
    assert store.job("first")["status"] == "uncertain"
    assert store.attempt_count("2026-10-18") == 1
    assert store.attempt_count("2026-10-19") == 0


def test_prior_manual_greenhouse_attempt_is_backfilled_without_changing_evidence(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,attempted_at TEXT,updated_at TEXT NOT NULL); CREATE TABLE attempts (job_id TEXT PRIMARY KEY,day TEXT NOT NULL,created_at TEXT NOT NULL);")
        payload = job(confirmation="Synthetic receipt", applied_at="2026-10-17T12:00:00+00:00")
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", ("first", json.dumps(payload), "submitted", "2026-10-17T12:00:00+00:00", "2026-10-17T12:00:00+00:00"))
        connection.execute("INSERT INTO attempts VALUES (?,?,?)", ("first", "2026-10-17", "2026-10-17T12:00:00+00:00"))
    store = Store(path)
    saved = store.job("first")
    assert saved["status"] == "submitted" and saved["confirmation"] == "Synthetic receipt"
    assert store.destination_attempted(job("other-source"))
    with store.connection() as connection:
        assert connection.execute("SELECT application_key FROM destination_attempts").fetchone()[0] == application_key(job())
    assert Store(path).job("first")["attempted_at"] == saved["attempted_at"]


def test_external_company_title_holds_are_unchanged_by_new_destination_support(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    original = job(apply_url=None)
    old_identity = listing_identity_key(original)
    store.save_job(original)
    store.record_external_application(job_id="first")
    store.save_job(job("new-source"))
    assert listing_identity_key(original) == old_identity
    assert store.job("new-source")["status"] == "applied_elsewhere"
    assert store.claim_job("new-source") is None


def test_manually_recorded_submitted_row_without_attempt_still_blocks_company_alias(tmp_path):
    path = tmp_path / "legacy-manual.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE jobs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,attempted_at TEXT,updated_at TEXT NOT NULL); CREATE TABLE attempts (job_id TEXT PRIMARY KEY,day TEXT NOT NULL,created_at TEXT NOT NULL);")
        payload = job(company="Example Robotics AI", confirmation="Synthetic guided confirmation")
        connection.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", ("first", json.dumps(payload), "submitted", None, "2026-10-17T12:00:00+00:00"))
    store = Store(path)
    alias = job("different-source", company="Example Robotics", apply_url=URL.replace("job-boards.greenhouse.io", "boards.greenhouse.io"))
    assert store.destination_attempted(alias)
    store.save_job(alias)
    assert store.claim_job(alias["id"]) is None
    assert store.job("first")["status"] == "submitted"
    assert store.job("first")["attempted_at"] is None
    assert store.job("first")["confirmation"] == "Synthetic guided confirmation"
    assert store.attempt_count("2026-10-18") == 0
    assert Store(path).destination_attempted(alias)


def test_history_url_record_is_supported_and_blocks_claim_before_filling(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.record_external_application(URL)
    store.save_job(job())
    store.update_job("first", "queued")
    assert store.claim_job("first") is None
    assert store.attempt_count("2026-10-18") == 0


def test_duplicate_reservation_is_atomic_across_greenhouse_aliases(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.save_job(job("one", status="filling"))
    store.save_job(job("two", status="filling", apply_url=URL.replace("job-boards.greenhouse.io", "boards.greenhouse.io")))
    def reserve(identifier):
        try:
            store.reserve_attempt(identifier, "2026-10-18", 5)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(reserve, ["one", "two"])) == [False, True]
