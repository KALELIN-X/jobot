"""Worker visibility with isolated storage and synthetic application callbacks."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Importing app creates a default Store; isolate that side effect as well.
os.environ["JOBRADAR_DATA_DIR"] = str(
    Path(__file__).resolve().parent.parent / "data" / "pytest-live-progress-import" / uuid.uuid4().hex
)

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.destinations import SIMPLIFY_SOURCE_URL, listing_binding, parse_destination, public_detail_url
from jobradar.run_status import RunTracker
from jobradar.store import Store


HEADERS = {"X-JobRadar-Client": "dashboard"}
PUBLIC_JOB = {"id": "synthetic-job", "company": "Example", "title": "New Grad Backend Engineer"}
PROFILE = {"name": "Alex Example", "email": "alex@example.test", "location": "Boston, MA",
           "approved_work_locations": ["United States"], "experience_months": 12,
           "graduation_month": "2022-06", "work_authorized": True, "needs_sponsorship": False,
           "skills": ["Python", "SQL"]}
RULES = {"company_scope": "any", "locations": ["United States"], "allowed_ats": ["lever", "greenhouse"],
         "max_required_experience_months": 12, "prefer_new_grad": True,
         "enforce_graduation_window": False, "min_score": 60, "daily_limit": 3}


@pytest.fixture
def tracker(tmp_path):
    return RunTracker(Store(tmp_path / "progress.sqlite3"))


def test_no_recorded_worker_is_idle_without_claiming_liveness(tracker):
    snapshot = tracker.snapshot()
    assert snapshot["status"] == snapshot["stage"] == "idle"
    assert not snapshot.get("worker_alive", False)


def test_progress_persists_only_observed_stage_counts_and_public_job_identity(tracker):
    tracker.start(total=2)
    tracker.report({"stage": "filling_fields", "field_count": 8, "completed_fields": 3,
                    "pending_question_count": 1, "message": "private answer text",
                    "email": "private@example.test", "answer": "private answer text",
                    "screenshot": "C:/private/contact.png"},
                   {**PUBLIC_JOB, "profile": PROFILE, "email": "private@example.test"})
    snapshot = tracker.snapshot()
    assert snapshot["status"] == "running" and snapshot["stage"] == "filling_fields"
    assert snapshot["active_job"] == PUBLIC_JOB
    assert (snapshot["field_count"], snapshot["completed_fields"], snapshot["pending_question_count"]) == (8, 3, 1)
    assert snapshot["processed"] == snapshot["submitted"] == 0
    assert snapshot["worker_alive"] is True
    saved = tracker.store.get("last_run")
    assert saved["active_job"] == PUBLIC_JOB and saved["stage"] == "filling_fields"
    persisted = json.dumps({"run": saved, "events": tracker.store.events()})
    for private in ("private@example.test", "private answer text", "C:/private/contact.png", PROFILE["email"]):
        assert private not in persisted
    snapshot["active_job"]["company"] = "Changed by observer"
    assert tracker.snapshot()["active_job"]["company"] == "Example"


@pytest.mark.parametrize("bad_count", [-1, 1001, True, "5", None, 3.5])
def test_untrusted_stage_and_malformed_counts_cannot_invent_progress(tracker, bad_count):
    tracker.start(total=1)
    tracker.report({"stage": "invented_success", "submitted": 99}, PUBLIC_JOB)
    assert tracker.snapshot()["stage"] == "starting"
    tracker.report({"stage": "inspecting_form", "field_count": bad_count,
                    "completed_fields": bad_count, "pending_question_count": bad_count}, PUBLIC_JOB)
    snapshot = tracker.snapshot()
    assert snapshot["field_count"] is snapshot["completed_fields"] is None
    assert snapshot["pending_question_count"] == 0 and snapshot["submitted"] == 0


def test_same_stage_count_updates_do_not_flood_events_and_next_job_resets_counts(tracker):
    tracker.start(total=2)
    for count in range(5):
        tracker.report({"stage": "filling_fields", "field_count": 5, "completed_fields": count,
                        "pending_question_count": 2}, PUBLIC_JOB)
    progress = [event for event in tracker.store.events() if event["type"] == "application_progress"]
    assert len(progress) == 1
    assert tracker.snapshot()["completed_fields"] == 4
    tracker.report({"stage": "opening_form"}, {**PUBLIC_JOB, "id": "second-job"})
    snapshot = tracker.snapshot()
    assert snapshot["field_count"] is snapshot["completed_fields"] is None
    assert snapshot["pending_question_count"] == 0


def test_heartbeat_indicates_worker_liveness_without_claiming_form_or_submission_activity(tracker, monkeypatch):
    tracker.start(total=1)
    tracker.report({"stage": "opening_form"}, PUBLIC_JOB)
    observed = tracker.snapshot()
    future = datetime.now(timezone.utc) + timedelta(seconds=25)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future if tz else future.replace(tzinfo=None)

    monkeypatch.setattr("jobradar.run_status.datetime", Clock)
    assert tracker.snapshot()["worker_alive"] is False
    monkeypatch.setattr("jobradar.run_status.now", lambda: future.isoformat())
    tracker.heartbeat()
    snapshot = tracker.snapshot()
    assert snapshot["worker_alive"] is True
    assert snapshot["updated_at"] == observed["updated_at"]
    assert snapshot["stage"] == "opening_form" and snapshot["timeline"] == observed["timeline"]
    assert snapshot["processed"] == snapshot["submitted"] == 0


def test_confirmed_held_failed_and_uncertain_outcomes_count_once_and_survive_restart(tracker):
    tracker.start(total=4)
    for index, status in enumerate(("submitted", "needs_attention", "failed", "uncertain")):
        job = {**PUBLIC_JOB, "id": f"job-{index}"}
        tracker.outcome(job, {"status": status})
        tracker.outcome(job, {"status": status})
    tracker.finish()
    snapshot = tracker.snapshot()
    assert snapshot["status"] == "completed" and snapshot["processed"] == 4
    assert all(snapshot[field] == 1 for field in ("submitted", "needs_attention", "failed", "uncertain"))
    assert snapshot["finished_at"] and not snapshot["worker_alive"]
    tracker.report({"stage": "submitting"}, PUBLIC_JOB)
    assert tracker.snapshot()["stage"] == "completed"
    restored = RunTracker(Store(tracker.store.path))
    assert restored.snapshot()["status"] == "completed"
    assert restored.snapshot()["processed"] == 4
    assert sum(event["type"] == "run_finished" for event in tracker.store.events()) == 1


@pytest.mark.parametrize("stop_requested", [False, True])
def test_restart_marks_unfinished_run_interrupted_without_retrying_or_claiming_confirmation(tracker, stop_requested):
    tracker.start(total=2)
    tracker.report({"stage": "awaiting_confirmation"}, PUBLIC_JOB)
    tracker.outcome(PUBLIC_JOB, {"status": "uncertain"})
    if stop_requested:
        tracker.request_stop()
    restored = RunTracker(Store(tracker.store.path))
    snapshot = restored.snapshot()
    assert snapshot["status"] == snapshot["stage"] == "interrupted"
    assert snapshot["processed"] == snapshot["uncertain"] == 1
    assert snapshot["submitted"] == 0 and snapshot["finished_at"]
    assert not snapshot["worker_alive"]
    RunTracker(Store(tracker.store.path))
    assert sum(event["type"] == "run_interrupted" for event in tracker.store.events()) == 1


def test_stop_is_observable_and_finishes_paused_without_manufactured_outcomes(tracker):
    tracker.start(total=2)
    tracker.report({"stage": "inspecting_form"}, PUBLIC_JOB)
    tracker.request_stop()
    assert tracker.snapshot()["status"] == "stopping"
    assert tracker.snapshot()["stage"] == "stopping"
    tracker.finish()
    assert tracker.snapshot()["status"] == "paused"
    assert tracker.snapshot()["processed"] == tracker.snapshot()["submitted"] == 0


def synthetic_job(identifier="queued-first", *, status="queued", score=95):
    url = f"https://job-boards.greenhouse.io/example/jobs/{int(uuid.uuid5(uuid.NAMESPACE_URL, identifier)) % 10000000000}"
    destination = parse_destination(url)
    job = {**PUBLIC_JOB, "id": identifier, "location": "Boston, MA", "country_code": "US", "work_model": "On Site",
           "source": "SimplifyJobs", "source_url": SIMPLIFY_SOURCE_URL,
           "detail_url": destination["canonical_url"], "apply_url": destination["apply_url"], "ats": "greenhouse",
           "description": "Build Python backend services and SQL databases. 0-1 years of professional experience.",
           "closed": False, "score": score, "status": status,
           "score_detail": {"experience_requirement": {"minimum_months": 0, "maximum_months": 12, "status": "known"},
                            "eligibility": [{"label": "Synthetic condition", "status": "pass", "detail": "Test only"}]}}
    job["destination_provenance"] = {"kind": "simplify_feed", "source_url": SIMPLIFY_SOURCE_URL,
                                     "application_key": destination["application_key"], "listing_binding": listing_binding(job)}
    job["employer_verified"] = {"source_url": public_detail_url(destination),
                                "application_key": destination["application_key"], "posting_id": destination["posting_id"]}
    return job


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def refreshed(job):
        return {**job, "closed": False, "candidate_refresh": {"status": "verified", "message": "Synthetic refresh"}}
    monkeypatch.setattr("jobradar.refresh.refresh_candidate", refreshed)
    with TestClient(create_app(tmp_path)) as instance:
        yield instance


def configure_and_approve(client):
    assert client.post("/api/profile", headers=HEADERS, json=PROFILE).status_code == 200
    assert client.post("/api/rules", headers=HEADERS, json=RULES).status_code == 200
    assert client.post("/api/resume", headers=HEADERS,
                       files={"file": ("synthetic-resume.txt", b"Built Python backend services and SQL databases.", "text/plain")}).status_code == 200
    fingerprint = client.get("/api/state").json()["approval"]["candidate_fingerprint"]
    response = client.post("/api/approve", headers=HEADERS,
                           json={"acknowledge": True, "expected_fingerprint": fingerprint})
    assert response.status_code == 200, response.text


def wait_idle(client):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = client.get("/api/state").json()
        if not snapshot["automation"]["running"]:
            return snapshot
        time.sleep(0.01)
    pytest.fail("Synthetic worker did not stop within the bounded test deadline")


def start_run(client):
    response = client.post("/api/run", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response


def test_api_reports_selected_job_and_observed_counts_while_worker_is_waiting(client, monkeypatch):
    configure_and_approve(client)
    client.app.state.store.save_job(synthetic_job())
    client.app.state.store.save_job(synthetic_job("discovered-higher-score", status="new", score=99))
    entered, release = threading.Event(), threading.Event()
    visited = []

    async def application(job, profile, resume_path, **options):
        visited.append(job["id"])
        await options["on_progress"]({"stage": "opening_form"})
        await options["on_progress"]({"stage": "filling_fields", "field_count": 8, "completed_fields": 3,
                                      "message": "Do not log this private answer", "answer": "private@example.test"})
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            return {"status": "failed", "message": "Synthetic release timed out"}
        await options["on_progress"]({"stage": "needs_attention", "pending_question_count": 1})
        return {"status": "needs_attention", "message": "Synthetic question needs an answer",
                "pending_questions": [{"id": "question-1", "key": "Favorite language?", "question": "Favorite language?",
                                       "type": "text", "required": True, "options": []}]}

    monkeypatch.setattr("jobradar.autofill.run_application", application)
    try:
        start_run(client)
        assert entered.wait(3), "Selected application did not reach its synthetic callback"
        snapshot = client.get("/api/state").json()
        run = snapshot["run"]
        assert snapshot["automation"]["running"] is True
        assert run["status"] == "running" and run["stage"] == "filling_fields"
        assert run["active_job"] == {**PUBLIC_JOB, "id": "queued-first"}
        assert run["total"] == 1 and run["processed"] == run["submitted"] == 0
        assert (run["field_count"], run["completed_fields"]) == (8, 3)
        assert run["worker_alive"] is True
        assert snapshot["queue"]["attempts_today"] == 0
        public_progress = json.dumps({"run": run, "events": snapshot["events"]})
        assert "private@example.test" not in public_progress
        assert "Do not log this private answer" not in public_progress
    finally:
        release.set()
    snapshot = wait_idle(client)
    assert snapshot["run"]["status"] == "completed"
    assert snapshot["run"]["processed"] == snapshot["run"]["needs_attention"] == 1
    assert snapshot["run"]["submitted"] == 0
    assert visited == ["queued-first"]
    assert client.app.state.store.job("discovered-higher-score")["status"] == "new"
    assert client.app.state.store.job("queued-first")["status"] == "needs_attention"
    assert snapshot["answer_memory"]["pending"][0]["job_id"] == "queued-first"
    assert client.post("/api/run", headers=HEADERS).status_code == 409
    assert visited == ["queued-first"], "Held jobs must not silently retry"


def test_api_attempt_and_waiting_stage_do_not_claim_confirmed_submission(client, monkeypatch):
    configure_and_approve(client)
    client.app.state.store.save_job(synthetic_job())
    entered, release = threading.Event(), threading.Event()

    async def application(job, profile, resume_path, **options):
        await options["on_progress"]({"stage": "checking_answers"})
        await options["before_submit"]()
        await options["on_progress"]({"stage": "submitting"})
        await options["on_progress"]({"stage": "awaiting_confirmation"})
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            return {"status": "uncertain", "message": "Synthetic confirmation timed out"}
        await options["on_progress"]({"stage": "finished", "status": "submitted"})
        return {"status": "submitted", "message": "Synthetic employer confirmation observed",
                "confirmation": "Synthetic application received"}

    monkeypatch.setattr("jobradar.autofill.run_application", application)
    try:
        start_run(client)
        assert entered.wait(3)
        snapshot = client.get("/api/state").json()
        assert snapshot["run"]["stage"] == "awaiting_confirmation"
        assert snapshot["run"]["submitted"] == snapshot["run"]["processed"] == 0
        assert snapshot["queue"]["attempts_today"] == 1
        assert client.app.state.store.job("queued-first")["status"] != "submitted"
    finally:
        release.set()
    snapshot = wait_idle(client)
    assert snapshot["run"]["processed"] == snapshot["run"]["submitted"] == 1
    assert snapshot["run"]["status"] == "completed"
    saved = client.app.state.store.job("queued-first")
    assert saved["status"] == "submitted" and saved["attempted_at"]
    assert saved["confirmation"] == "Synthetic application received"
    assert client.app.state.store.destination_attempted(saved)


def test_api_stop_is_visible_and_preserves_remaining_queue_without_submission(client, monkeypatch):
    configure_and_approve(client)
    client.app.state.store.save_job(synthetic_job(score=99))
    client.app.state.store.save_job(synthetic_job("queued-later", score=90))
    entered, release = threading.Event(), threading.Event()
    visited = []

    async def application(job, profile, resume_path, **options):
        visited.append(job["id"])
        await options["on_progress"]({"stage": "checking_answers"})
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            return {"status": "failed", "message": "Synthetic release timed out"}
        try:
            await options["before_submit"]()
        except ValueError:
            await options["on_progress"]({"stage": "needs_attention"})
            return {"status": "needs_attention", "message": "Stopped before any submission request"}
        pytest.fail("Stop must be checked before reserving an application attempt")

    monkeypatch.setattr("jobradar.autofill.run_application", application)
    try:
        start_run(client)
        assert entered.wait(3)
        assert client.post("/api/run/stop").status_code == 403
        assert client.post("/api/run/stop", headers=HEADERS).status_code == 200
        snapshot = client.get("/api/state").json()
        assert snapshot["run"]["status"] == "stopping"
        assert snapshot["run"]["submitted"] == 0
    finally:
        release.set()
    snapshot = wait_idle(client)
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["run"]["processed"] == snapshot["run"]["needs_attention"] == 1
    assert snapshot["run"]["submitted"] == snapshot["queue"]["attempts_today"] == 0
    assert client.app.state.store.job("queued-later")["status"] == "queued"
    assert visited == ["queued-first"]


def test_api_restart_exposes_interruption_and_preserves_confirmed_history(tmp_path, monkeypatch):
    store = Store(tmp_path / "jobradar.sqlite3")
    confirmed = synthetic_job("confirmed-history", status="submitted")
    store.save_job(confirmed)
    tracker = RunTracker(store)
    tracker.start(total=2)
    tracker.outcome(confirmed, {"status": "submitted"})
    tracker.report({"stage": "inspecting_form"}, synthetic_job("interrupted-job"))

    async def forbidden(*args, **kwargs):
        pytest.fail("Recovering progress must never launch an application worker")

    monkeypatch.setattr("jobradar.autofill.run_application", forbidden)
    with TestClient(create_app(tmp_path)) as reopened:
        snapshot = reopened.get("/api/state").json()
        assert snapshot["run"]["status"] == "interrupted"
        assert snapshot["run"]["submitted"] == snapshot["run"]["processed"] == 1
        assert not snapshot["automation"]["running"] and not snapshot["run"]["worker_alive"]
        assert reopened.app.state.store.job("confirmed-history")["status"] == "submitted"
        assert reopened.app.state.store.destination_attempted(confirmed)
        assert any(event["type"] == "run_interrupted" for event in snapshot["events"])


def test_confirming_activity_question_updates_saved_answer_and_visibly_stops_active_approval(client, monkeypatch):
    configure_and_approve(client)
    client.app.state.store.save_job(synthetic_job())
    question = {"id": "office", "key": "Preferred office?", "question": "Preferred office?", "type": "select",
                "required": True, "options": [{"label": "Boston", "value": "bos"}, {"label": "New York", "value": "nyc"}]}
    client.app.state.store.save_job(synthetic_job("held-question", status="needs_attention") | {"pending_questions": [question]})
    entered, release = threading.Event(), threading.Event()

    async def application(job, profile, resume_path, **options):
        await options["on_progress"]({"stage": "checking_answers"})
        entered.set()
        if not await asyncio.to_thread(release.wait, 5):
            return {"status": "failed", "message": "Synthetic release timed out"}
        try:
            await options["before_submit"]()
        except ValueError:
            return {"status": "needs_attention", "message": "Saved answers changed; review approval again"}
        pytest.fail("An answer change must invalidate the previously approved profile before submission")

    monkeypatch.setattr("jobradar.autofill.run_application", application)
    try:
        start_run(client)
        assert entered.wait(3)
        pending = client.get("/api/state").json()["answer_memory"]["pending"]
        assert pending[0]["job_id"] == "held-question" and pending[0]["options"] == question["options"]
        response = client.post("/api/answers", headers=HEADERS,
                               json={"job_id": "held-question", "question": question["question"], "answer": "nyc"})
        assert response.status_code == 200 and response.json()["approval_required"] is True
        snapshot = client.get("/api/state").json()
        assert snapshot["profile"]["answers"][question["question"]] == "New York"
        assert snapshot["run"]["status"] == "stopping"
        assert not snapshot["approval"]["approved"] and not snapshot["automation"]["enabled"]
        assert snapshot["answer_memory"]["pending"] == []
        assert client.app.state.store.job("held-question")["status"] == "needs_attention"
    finally:
        release.set()
    snapshot = wait_idle(client)
    assert snapshot["run"]["status"] == "paused"
    assert snapshot["run"]["submitted"] == snapshot["queue"]["attempts_today"] == 0


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


def test_api_serves_only_the_recorded_job_capture_without_exposing_local_paths(client):
    directory = client.app.state.data_dir / "artifacts" / "capture-job"
    directory.mkdir(parents=True)
    screenshot = directory / "screen.PNG"
    screenshot.write_bytes(PNG)
    client.app.state.store.save_job(synthetic_job("capture-job", status="needs_attention") | {"screenshot": str(screenshot)})
    response = client.get("/api/jobs/capture-job/screenshot")
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    snapshot = client.get("/api/state").json()
    job = next(row for row in snapshot["jobs"] if row["id"] == "capture-job")
    assert job["browser_capture_available"] is True
    assert "screenshot" not in job
    assert str(screenshot) not in json.dumps(snapshot)


@pytest.mark.parametrize("location", ["outside", "other-job", "non-png", "missing", "directory", "relative-traversal"])
def test_api_rejects_unrecorded_outside_or_non_png_capture_paths(client, location):
    data_dir = client.app.state.data_dir
    capture_dir = data_dir / "artifacts" / "capture-job"
    capture_dir.mkdir(parents=True)
    targets = {"outside": data_dir / "private.png",
               "other-job": data_dir / "artifacts" / "other-job" / "screen.png",
               "non-png": capture_dir / "private.txt",
               "missing": capture_dir / "absent.png",
               "directory": capture_dir / "folder.png",
               "relative-traversal": capture_dir / ".." / "other-job" / "screen.png"}
    target = targets[location]
    if location == "directory":
        target.mkdir()
    elif location != "missing":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG)
    client.app.state.store.save_job(synthetic_job("capture-job", status="needs_attention") | {"screenshot": str(target)})
    assert client.get("/api/jobs/capture-job/screenshot").status_code == 404
    snapshot = client.get("/api/state").json()
    assert snapshot["jobs"][0]["browser_capture_available"] is False
    assert "screenshot" not in snapshot["jobs"][0]
    assert client.get("/api/jobs/unknown-id/screenshot").status_code == 404


@pytest.mark.parametrize("identifier,encoded", [("..", "%2E%2E"), (".", "%2E")])
def test_capture_job_identifier_cannot_expand_its_directory_to_artifact_or_data_root(client, identifier, encoded):
    root = client.app.state.data_dir if identifier == ".." else client.app.state.data_dir / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    screenshot = root / "private.png"
    screenshot.write_bytes(PNG)
    client.app.state.store.save_job(synthetic_job(identifier, status="needs_attention") | {"screenshot": str(screenshot)})
    assert client.get(f"/api/jobs/{encoded}/screenshot").status_code == 404
    assert client.get("/api/state").json()["jobs"][0]["browser_capture_available"] is False


def test_capture_symlink_cannot_escape_the_artifacts_root(client):
    data_dir = client.app.state.data_dir
    outside = data_dir / "private"
    outside.mkdir()
    screenshot = outside / "screen.png"
    screenshot.write_bytes(PNG)
    link = data_dir / "artifacts" / "capture-job"
    link.parent.mkdir()
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlink creation is unavailable: {type(error).__name__}")
    client.app.state.store.save_job(synthetic_job("capture-job", status="needs_attention") | {"screenshot": str(link / "screen.png")})
    assert client.get("/api/jobs/capture-job/screenshot").status_code == 404
    assert client.get("/api/state").json()["jobs"][0]["browser_capture_available"] is False
