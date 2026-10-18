from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .destinations import application_key_from_url


def application_key(job):
    """Stable supported-ATS identity, preserving all existing Lever ledger keys."""
    if job.get("demo"):
        return None
    raw = job.get("apply_url") or job.get("application_url") or job.get("url")
    return application_key_from_url(raw)


def now():
    return datetime.now(timezone.utc).isoformat()


def listing_identity_key(job):
    """A conservative employer/title hold, never proof of an exact posting."""
    if job.get("demo"):
        return None
    parts = []
    for field in ("company", "title"):
        raw = job.get(field)
        if not isinstance(raw, str) or not raw.strip():
            return None
        parts.append(" ".join(unicodedata.normalize("NFKC", raw).casefold().split()))
    return "company-title:" + hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, attempted_at TEXT, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS attempts (job_id TEXT PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS destination_attempts (application_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS external_listing_history (identity_key TEXT PRIMARY KEY, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, type TEXT NOT NULL, message TEXT NOT NULL, job_id TEXT);
              CREATE TABLE IF NOT EXISTS mock_receipts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL);
            """)
            # Keep the original attempts table intact for existing installations.
            # A separate unique destination ledger also accommodates legacy data
            # that already contains duplicate feed records for one ATS posting.
            connection.execute("BEGIN IMMEDIATE")
            old_attempts = connection.execute("""
                SELECT jobs.id, jobs.payload,
                       COALESCE(attempts.created_at, jobs.attempted_at, jobs.updated_at) AS created_at
                FROM jobs LEFT JOIN attempts ON attempts.job_id=jobs.id
                WHERE attempts.job_id IS NOT NULL OR jobs.attempted_at IS NOT NULL
                      OR jobs.status IN ('submitted', 'uncertain')
                ORDER BY created_at, jobs.id
            """).fetchall()
            for row in old_attempts:
                try:
                    key = application_key(json.loads(row["payload"]))
                except (ValueError, TypeError):
                    continue
                if key:
                    connection.execute(
                        "INSERT OR IGNORE INTO destination_attempts VALUES(?,?,?)",
                        (key, row["id"], row["created_at"]),
                    )

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, key, default=None):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value)))

    def event(self, kind, message, job_id=None):
        with self.connection() as connection:
            connection.execute("INSERT INTO events(created_at,type,message,job_id) VALUES (?,?,?,?)", (now(), kind, message[:1500], job_id))

    def events(self):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]

    def job(self, job_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    @staticmethod
    def _job(row):
        result = json.loads(row["payload"])
        return {**result, "id": row["id"], "status": row["status"], "attempted_at": row["attempted_at"], "updated_at": row["updated_at"]}

    def jobs(self):
        with self.connection() as connection:
            return [self._job(row) for row in connection.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 1000")]

    def save_job(self, job):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT status,attempted_at,payload FROM jobs WHERE id=?", (job["id"],)).fetchone()
            status = existing["status"] if existing else job.get("status", "new")
            attempted = existing["attempted_at"] if existing else None
            if existing:
                previous = json.loads(existing["payload"])
                # Discovery refreshes source fields, but cannot erase a worker's
                # questions or recorded delivery evidence while the owner reviews it.
                runtime_fields = {key: previous[key] for key in ("pending_questions", "confirmation", "message", "error", "applied_at", "screenshot")
                                  if key in previous}
                job = {**job, **runtime_fields}
            key = application_key(job)
            prior = connection.execute("SELECT job_id FROM destination_attempts WHERE application_key=?", (key,)).fetchone() if key else None
            externally_recorded = (prior and prior["job_id"].startswith("external:")) or self._identity_recorded(connection, job)
            if externally_recorded and not attempted and status not in {"filling", "submitting", "submitted", "uncertain"}:
                status = "applied_elsewhere"
            connection.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)", (job["id"], json.dumps(job), status, attempted, now()))

    @staticmethod
    def _identity_recorded(connection, job):
        key = listing_identity_key(job)
        return bool(key and connection.execute("SELECT 1 FROM external_listing_history WHERE identity_key=?", (key,)).fetchone())

    def record_external_application(self, url=None, job_id=None):
        """Record a user-reported application without consuming the local quota.

        The destination ledger remains authoritative even when no matching feed
        row exists. A saved listing also creates a broader employer/title hold
        for possible reposts. Active or attempted states retain their evidence.
        """
        key = application_key({"apply_url": url}) if url is not None else None
        if url is not None and key is None:
            raise ValueError("Enter an HTTPS Lever or Greenhouse posting URL with a valid posting ID.")
        if url is None and job_id is None:
            raise ValueError("Provide a saved job ID or an HTTPS Lever or Greenhouse posting URL.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            identity_key = None
            if job_id is not None:
                selected = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
                if selected is None:
                    raise LookupError("Job not found.")
                selected_job = json.loads(selected["payload"])
                identity_key = listing_identity_key(selected_job)
                if identity_key is None:
                    raise ValueError("The saved listing needs an employer and title; synthetic demos cannot be recorded as application history.")
                selected_key = application_key(selected_job)
                if url is not None and selected_key != key:
                    raise ValueError("The provided URL does not match the supported employer destination on the selected listing.")
                key = selected_key
            recorded = False
            if key is not None:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO destination_attempts VALUES(?,?,?)",
                    (key, "external:" + key, now()),
                )
                recorded = cursor.rowcount == 1
            if identity_key is not None:
                cursor = connection.execute("INSERT OR IGNORE INTO external_listing_history VALUES(?,?)", (identity_key, now()))
                recorded = recorded or cursor.rowcount == 1
            matched = updated = 0
            for row in connection.execute("SELECT * FROM jobs").fetchall():
                candidate = json.loads(row["payload"])
                destination_matches = key is not None and application_key(candidate) == key
                identity_matches = identity_key is not None and listing_identity_key(candidate) == identity_key
                if not destination_matches and not identity_matches:
                    continue
                matched += 1
                if row["attempted_at"] or row["status"] in {"filling", "submitting", "submitted", "uncertain", "applied_elsewhere"}:
                    continue
                connection.execute("UPDATE jobs SET status='applied_elsewhere',updated_at=? WHERE id=?", (now(), row["id"]))
                updated += 1
        result = {"recorded": recorded, "matched_jobs": matched, "updated_jobs": updated}
        if key is not None:
            result["application_key"] = key
        if identity_key is not None:
            result.update(identity_key=identity_key, review_scope="company_title")
        return result

    def update_job(self, job_id, status, **fields):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Application not found.")
            payload = {**json.loads(row[0]), **fields}
            connection.execute("UPDATE jobs SET payload=?,status=?,updated_at=? WHERE id=?", (json.dumps(payload), status, now(), job_id))

    def claim_job(self, job_id):
        """Claim the current eligible row, never an older queue snapshot."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["attempted_at"] or row["status"] not in {"new", "queued"}:
                return None
            current_job = json.loads(row["payload"])
            key = application_key(current_job)
            if ((key and connection.execute("SELECT 1 FROM destination_attempts WHERE application_key=?", (key,)).fetchone())
                    or self._identity_recorded(connection, current_job)):
                return None
            updated_at = now()
            connection.execute(
                "UPDATE jobs SET status='filling',updated_at=? WHERE id=?",
                (updated_at, job_id),
            )
            return {**self._job(row), "status": "filling", "updated_at": updated_at}

    def attempt_count(self, day):
        """Read the durable per-day attempt count before opening another form."""
        with self.connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM attempts WHERE day=?", (day,)).fetchone()[0]

    def destination_attempted(self, job):
        """Skip already-attempted supported destinations before filling/uploading."""
        with self.connection() as connection:
            key = application_key(job)
            return bool((key and connection.execute("SELECT 1 FROM destination_attempts WHERE application_key=?", (key,)).fetchone()) or self._identity_recorded(connection, job))

    def reserve_attempt(self, job_id, day, limit):
        # Transaction protects against two workers exceeding the quota or submitting twice.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload,attempted_at,status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["attempted_at"] or row["status"] != "filling":
                raise ValueError("This application already has an attempt or is no longer active.")
            count = connection.execute("SELECT COUNT(*) FROM attempts WHERE day=?", (day,)).fetchone()[0]
            if count >= limit:
                raise ValueError("Daily submission-attempt limit reached (UTC day).")
            job = json.loads(row["payload"])
            if self._identity_recorded(connection, job):
                raise ValueError("An application with this employer and title was reported elsewhere. This may be a repost; review it before any new application.")
            key = application_key(job)
            if key and connection.execute("SELECT 1 FROM destination_attempts WHERE application_key=?", (key,)).fetchone():
                raise ValueError("This employer posting already has a submission attempt under another feed record.")
            connection.execute("INSERT INTO attempts VALUES(?,?,?)", (job_id, day, now()))
            if key:
                connection.execute("INSERT INTO destination_attempts VALUES(?,?,?)", (key, job_id, now()))
            connection.execute("UPDATE jobs SET attempted_at=?,status='submitting' WHERE id=?", (now(), job_id))

    def receipt(self, name, email):
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO mock_receipts(created_at,name,email) VALUES(?,?,?)", (now(), name, email))
            return cursor.lastrowid

    def recover(self):
        with self.connection() as connection:
            connection.execute("UPDATE jobs SET status=CASE WHEN attempted_at IS NULL THEN 'needs_attention' ELSE 'uncertain' END WHERE status IN ('filling','submitting')")
