import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobradar.store import Store, application_key


URL = "https://jobs.lever.co/Example/12345678-abcd-1234-abcd-123456789abc/apply"


def job(job_id="first", **changes):
    return {"id": job_id, "status": "new", "apply_url": URL, "title": "Original title", **changes}


def test_feed_refresh_preserves_review_questions_and_confirmation_but_not_old_destinations(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(destination_provenance={"old": "source"}))
    question = {"question": "Preferred editor?", "type": "text"}
    store.update_job("first", "needs_attention", pending_questions=[question], form_questions=[question],
                     questions_refreshed_at="2026-10-18T12:00:00+00:00", confirmation="Recorded receipt", error="Review needed")
    store.save_job(job(title="Fresh title", apply_url=None, destination_provenance=None))
    saved = store.job("first")
    assert saved["title"] == "Fresh title"
    assert saved["status"] == "needs_attention"
    assert saved["pending_questions"] == [question]
    assert saved["form_questions"] == [question]
    assert saved["questions_refreshed_at"] == "2026-10-18T12:00:00+00:00"
    assert saved["confirmation"] == "Recorded receipt"
    assert saved["error"] == "Review needed"
    assert saved["apply_url"] is None
    assert saved["destination_provenance"] is None


def test_feed_cannot_replace_explicitly_refreshed_question_metadata_with_stale_payload(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job())
    store.update_job("first", "needs_attention", form_questions=[], pending_questions=[],
                     questions_refreshed_at="2026-10-18T12:30:00+00:00")
    store.save_job(job(title="Fresh source title", form_questions=[{"question": "Stale source question"}],
                       questions_refreshed_at="2020-01-01T00:00:00+00:00"))
    refreshed = store.job("first")
    assert refreshed["title"] == "Fresh source title"
    assert refreshed["form_questions"] == refreshed["pending_questions"] == []
    assert refreshed["questions_refreshed_at"] == "2026-10-18T12:30:00+00:00"


@pytest.mark.parametrize("variant", [
    URL,
    URL.replace("jobs.lever.co", "JOBS.LEVER.CO"),
    URL.replace("Example", "example").replace("abcd", "ABCD"),
    URL + "/?utm_source=another-feed",
    URL.replace("/apply", ""),
    URL.replace("jobs.lever.co", "jobs.lever.co:443"),
])
def test_canonical_key_collapses_equivalent_lever_urls(variant):
    assert application_key(job(apply_url=variant)) == application_key(job())


def test_canonical_key_keeps_tenant_board_and_posting_distinct():
    expected = application_key(job())
    assert application_key(job(apply_url=URL.replace("jobs.lever.co", "jobs.eu.lever.co"))) != expected
    assert application_key(job(apply_url=URL.replace("Example", "Another"))) != expected
    assert application_key(job(apply_url=URL.replace("12345678-abcd", "87654321-abcd"))) != expected
    assert application_key(job(demo=True)) is None


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"),
    URL.replace("jobs.lever.co", "jobs.lever.co.evil.test"),
    URL.replace("jobs.lever.co", "user@jobs.lever.co"),
    URL.replace("jobs.lever.co", "jobs.lever.co:bad"),
    URL.replace("12345678-abcd-1234-abcd-123456789abc", "unknown"),
])
def test_canonical_key_rejects_untrusted_or_unrecognized_urls(url):
    assert application_key(job(apply_url=url)) is None


@pytest.mark.parametrize("limit", [5, None])
def test_same_destination_cannot_reserve_under_a_different_feed_id(tmp_path, limit):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status="filling"))
    store.save_job(job("second", status="filling", apply_url=URL.lower() + "?source=reposted"))
    assert store.attempt_count("2026-10-18") == 0
    assert not store.destination_attempted(job("second"))
    store.reserve_attempt("first", "2026-10-18", limit)
    assert store.attempt_count("2026-10-18") == 1
    assert store.attempt_count("2026-10-19") == 0
    assert store.destination_attempted(job("second"))
    assert not store.destination_attempted(job("demo", demo=True))
    assert not store.destination_attempted(job("different", apply_url=URL.replace("Example", "Other")))
    with pytest.raises(ValueError, match="another feed record"):
        store.reserve_attempt("second", "2026-10-19", limit)
    assert store.job("second")["attempted_at"] is None
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


@pytest.mark.parametrize("limit", [5, None])
def test_destination_dedupe_is_atomic_across_workers(tmp_path, limit):
    store = Store(tmp_path / "db.sqlite3")
    for candidate in (job("one", status="filling"), job("two", status="filling")):
        store.save_job(candidate)

    def reserve(job_id):
        try:
            store.reserve_attempt(job_id, "2026-10-18", limit)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["one", "two"])) == [False, True]


def test_daily_limit_is_atomic_for_distinct_destinations(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job("one", status="filling"))
    store.save_job(job("two", status="filling", apply_url=URL.replace("12345678-abcd", "87654321-abcd")))

    def reserve(job_id):
        try:
            store.reserve_attempt(job_id, "2026-10-18", 1)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["one", "two"])) == [False, True]


def test_legacy_attempts_are_preserved_and_backfilled_even_with_duplicates(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE attempts (job_id TEXT PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        for job_id in ("old-one", "old-two"):
            connection.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job_id, json.dumps(job(job_id)), "uncertain", "2026-10-17", "2026-10-17"))
            connection.execute("INSERT INTO attempts VALUES(?,?,?)", (job_id, "2026-10-17", "2026-10-17"))
    store = Store(path)
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM destination_attempts").fetchone()[0] == 1
    store.save_job(job("reposted", status="filling"))
    with pytest.raises(ValueError, match="another feed record"):
        store.reserve_attempt("reposted", "2026-10-18", 5)
    # Reopening an already migrated database is idempotent.
    assert Store(path).job("old-one")["status"] == "uncertain"


def test_claim_uses_current_row_and_does_not_resurrect_a_dismissal(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job())
    old_snapshot = store.jobs()[0]
    store.update_job("first", "dismissed")
    assert old_snapshot["status"] == "new"
    assert store.claim_job(old_snapshot["id"]) is None
    assert store.job("first")["status"] == "dismissed"


def test_claim_returns_fresh_payload_and_only_one_worker_can_claim(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job())
    store.save_job(job(title="Updated title", status="queued"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.claim_job, ["first", "first"]))
    successful = [result for result in results if result]
    assert len(successful) == 1
    assert successful[0]["title"] == "Updated title"
    assert successful[0]["status"] == "filling"
    assert store.job("first")["status"] == "filling"


@pytest.mark.parametrize("status", ["dismissed", "submitted", "uncertain", "submitting", "filling", "failed", "needs_attention"])
def test_claim_refuses_ineligible_statuses(tmp_path, status):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status=status))
    assert store.claim_job("first") is None
    assert store.claim_job("missing") is None


def test_claim_refuses_a_requeued_row_that_has_an_attempt(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(job(status="filling"))
    store.reserve_attempt("first", "2026-10-18", 5)
    store.update_job("first", "queued")
    assert store.claim_job("first") is None
