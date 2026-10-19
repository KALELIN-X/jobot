from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .answer_memory import answer_memory_state, register_answer_memory_routes
from .approval import fingerprint, job_problems, resume_intact, setup_problems, valid_approval
from .destinations import SUPPORTED_HOSTS, verified_destination
from .dashboard import application_events
from .models import ApprovalRequest, AutomationRequest, ExternalApplicationRequest, JobStatusRequest, Profile, QuestionLookupRequest, Rules, company_scope
from .job_facts import job_facts
from .store import Store, application_key, now
from .run_status import RunTracker

ROOT = Path(__file__).resolve().parent.parent
MAX_RESUME_BYTES = 5 * 1024 * 1024


def ordered_queue(jobs, rules):
    """Prefer explicitly identified new-grad listings without altering fit scores."""
    prefer_new_grad = rules.get("prefer_new_grad", False)
    return sorted(jobs, key=lambda job: (
        bool(prefer_new_grad and (job.get("score_detail") or {}).get("is_new_grad") is True),
        job["score"] if job.get("score") is not None else -1,
    ), reverse=True)


def create_app(data_dir: Path | None = None):
    data_dir = data_dir or Path(os.environ.get("JOBRADAR_DATA_DIR", str(ROOT / "data")))
    store = Store(data_dir / "jobradar.sqlite3")
    store.recover()
    tracker = RunTracker(store)
    state = {"running": False, "fetching": False, "task": None, "demo_running": False, "mock_tokens": set()}
    queue_lock = asyncio.Lock()
    question_lock = asyncio.Lock()

    def configuration():
        rules = Rules.model_validate(store.get("rules", {})).model_dump()
        return (store.get("profile", Profile().model_dump()), rules, store.get("resume", {}), store.get("approval", {}))

    def approved():
        return valid_approval(*configuration())

    def worker_busy():
        return state["running"] or state["demo_running"] or bool(state["task"] and not state["task"].done())

    def configuration_problems(profile, rules, resume):
        problems = setup_problems(profile, rules, resume)
        if resume.get("loaded") and not resume_intact(resume, data_dir):
            problems.append("The uploaded resume file is missing or changed. Upload it again before approval.")
        return problems

    def unanswered_question_counts(job, profile=None):
        if not job.get("pending_questions"):
            return 0, 0
        if profile is None:
            profile = store.get("profile", Profile().model_dump())
        pending = answer_memory_state(profile, [job])["pending"]
        answers = profile.get("answers") or {}
        required = sum(question.get("required") is True for question in pending)
        conflicting = sum(question.get("required") is not True and question["question"] in answers for question in pending)
        return required, conflicting

    def matching_problems(job, rules, profile=None):
        problems = job_problems(job, rules)
        if job["status"] == "applied_elsewhere" or store.destination_attempted(job):
            problems.append("This posting or a listing with the same employer and title is already recorded as applied. Automatic retry is disabled.")
        required, conflicting = unanswered_question_counts(job, profile)
        if required:
            problems.append(f"{required} required application question(s) still need your confirmed answer. Answer them in Activity before running this role.")
        if conflicting:
            problems.append(f"{conflicting} saved answer(s) do not match this form's recorded choices or input type. Review them in Activity.")
        return problems

    def selected_destinations(jobs):
        selected = {}
        for job in jobs:
            if not job.get("demo") and (job["status"] in {"queued", "active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"}
                                        or job.get("attempted_at")):
                key = application_key(job)
                if key:
                    selected.setdefault(key, set()).add(job["id"])
        return selected

    def queue_actions(job, selected=None):
        selected = selected if selected is not None else selected_destinations(store.jobs())
        active_or_attempted = job["status"] in {"active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"} or bool(job.get("attempted_at"))
        prior_destination = store.destination_attempted(job)
        duplicate_selected = bool(selected.get(application_key(job), set()) - {job["id"]})
        can_add = (not job.get("demo") and not active_or_attempted and not prior_destination and not duplicate_selected
                   and job["status"] != "queued")
        can_remove = not job.get("demo") and not active_or_attempted and job["status"] == "queued"
        reason = ("Synthetic demo records are separate from the application queue." if job.get("demo") else
                  "An active or attempted application cannot be queued again. Verify its recorded outcome first." if active_or_attempted else
                  "This employer posting or matching application is already recorded as applied; it cannot be queued again." if prior_destination else
                  "Already in your application queue." if job["status"] == "queued" else
                  "Another record for this employer posting is already queued or active. Use the existing queue entry." if duplicate_selected else "")
        return {"can_add": can_add, "can_remove": can_remove, "reason": reason}

    def score_qualified(job, rules):
        score = job.get("score")
        return (isinstance(score, (int, float)) and not isinstance(score, bool)
                and math.isfinite(score) and rules["min_score"] <= score <= 100)

    def loadable_jobs(jobs, rules, *, scheduled=False, profile=None):
        selected = selected_destinations(jobs)
        candidates = []
        for job in jobs:
            if not queue_actions(job, selected)["can_add"] or not score_qualified(job, rules):
                continue
            if scheduled and (job["status"] != "new" or matching_problems(job, rules, profile)):
                continue
            candidates.append(job)
        available = []
        reserved = set(selected)
        for job in ordered_queue(candidates, rules):
            key = application_key(job)
            if key and key in reserved:
                continue
            available.append(job)
            if key:
                reserved.add(key)
        return available

    def submission_check(job, rules, profile=None):
        problems = matching_problems(job, rules, profile)
        matches_rules = not problems
        if job["status"] == "new":
            problems.append("Add this role to your queue before running it.")
        elif job["status"] != "queued":
            problems.append("Only selected queued roles can run. Resolve any review notes before adding this role to the queue.")
        return {"eligible": not problems, "matches_rules": matches_rules, "blockers": problems}

    def add_matching_jobs(*, scheduled=False):
        profile, rules, resume, approval = configuration()
        scheduling_allowed = (not scheduled or (store.get("automation", {}).get("enabled")
                              and valid_approval(profile, rules, resume, approval)
                              and not configuration_problems(profile, rules, resume)))
        # Manual loading is a shortlist action. Scheduling still needs fresh
        # discoveries that pass every approved submission rule.
        candidates = loadable_jobs(store.jobs(), rules, scheduled=scheduled, profile=profile) if scheduling_allowed else []
        added = 0
        for job in ordered_queue(candidates, rules):
            store.update_job(job["id"], "queued", error=None)
            added += 1
        jobs = store.jobs()
        selected = selected_destinations(jobs)
        queued = [job for job in jobs if job["status"] == "queued" and not job.get("demo")]
        ready = sum(not matching_problems(job, rules, profile) for job in queued)
        result = {"added": added, "queued": len(queued), "ready": ready, "blocked": len(queued) - ready,
                  "min_score": rules["min_score"],
                  "remaining_loadable": len(loadable_jobs(jobs, rules)),
                  "remaining_matching": sum(job["status"] == "new" and queue_actions(job, selected)["can_add"] and score_qualified(job, rules)
                                            and not matching_problems(job, rules, profile) for job in jobs)}
        if added:
            message = (f"Scheduled automation selected {added} eligible discovered role(s) for the application queue." if scheduled else
                       f"Loaded {added} role(s) scoring at least {rules['min_score']}/100 into the application queue. Submission checks still apply.")
            store.event("queue", message)
        return result

    def queue_readiness(jobs, profile, rules, resume, approval):
        real_jobs = [job for job in jobs if not job.get("demo")]
        discovered = [job for job in real_jobs if job["status"] == "new"]
        matching = sum(job["submission_check"]["matches_rules"] and job["queue_actions"]["can_add"] for job in discovered)
        loadable = len(loadable_jobs(real_jobs, rules))
        pending = [job for job in real_jobs if job["status"] == "queued"]
        ready = sum(job["submission_check"]["eligible"] for job in pending)
        day = datetime.now(timezone.utc).date().isoformat()
        attempts = store.attempt_count(day)
        daily_limit = rules["daily_limit"]
        remaining = None if daily_limit is None else max(0, daily_limit - attempts)
        problems = configuration_problems(profile, rules, resume)
        if not valid_approval(profile, rules, resume, approval):
            problems.append("Review and approve your saved profile, resume and application rules.")
        if remaining == 0:
            problems.append("Daily submission-attempt limit reached (UTC day). Your remaining queue is saved.")
        if not pending:
            problems.append("Your application queue is empty. Add a role to the queue or choose Load jobs to queue before running.")
        elif not ready:
            problems.append("No queued roles currently pass your saved rules. Check each role's submission blockers before running.")
        return {"discovered": len(discovered), "matching": matching, "loadable": loadable, "discovery_blocked": len(discovered) - matching,
                "queued": len(pending), "pending": len(pending), "ready": ready, "blocked": len(pending) - ready,
                "attempts_today": attempts, "remaining_today": remaining, "daily_limit_enabled": daily_limit is not None,
                "can_run": not problems and not worker_busy(), "blockers": problems}

    def invalidate():
        store.set("approval", {"approved": False, "approved_at": None})
        store.set("automation", {"enabled": False})
        store.set("stop_requested", True)
        tracker.request_stop()

    def rank_all():
        from .ranking import rank_job
        profile, rules, resume, _ = configuration()
        for job in store.jobs():
            if job.get("demo"):
                continue
            detail = rank_job(job, profile, resume.get("text", ""), rules)
            job.update(score=detail.get("score"), score_detail=detail)
            store.save_job(job)

    async def fetch():
        from .ingest import enrich_lever_jobs, fetch_jobs
        if state["fetching"]:
            return {"message": "A fetch is already running."}
        state["fetching"] = True
        try:
            store.event("fetch", "Checking public new-grad software listings and official employer postings.")
            jobs = await fetch_jobs(limit=100)
            _, rules, _, _ = configuration()
            jobs = await enrich_lever_jobs(jobs, rules.get("company_boards", {}))
            for job in jobs:
                store.save_job(job)
            rank_all()
            _, rules, _, _ = configuration()
            count = sum(verified_destination(job) for job in jobs)
            saved_jobs = [job for job in store.jobs() if not job.get("demo")]
            matching = sum(not matching_problems(job, rules) for job in saved_jobs if job["status"] == "new")
            ready = sum(submission_check(job, rules)["eligible"] for job in saved_jobs if job["status"] == "queued")
            message = f"Fetched {len(jobs)} listings; {count} verified Lever or Greenhouse destinations. {matching} discovered roles match your rules; {ready} selected queued roles are ready."
            summary = {"count": len(jobs), "verified_destinations": count,
                       "direct_links": sum(bool(job.get("apply_url")) for job in jobs),
                       "matching": matching, "ready": ready, "message": message,
                       "sources": sorted({job.get("source", "Jobright") for job in jobs})}
            store.event("fetch", message + " A missing feed row is not treated as a closed job.")
            store.set("last_fetch", now())
            store.set("last_fetch_summary", summary)
            return summary
        finally:
            state["fetching"] = False

    async def run_queue():
        from .answer_memory import _recorded_candidates
        from .autofill import _label, run_application
        from .ranking import rank_job
        from .refresh import refresh_candidate
        if queue_lock.locked():
            return
        async with queue_lock:
            state["running"] = True
            finish_status = "completed"
            try:
                _, queue_rules, _, _ = configuration()
                refresh_day = datetime.now(timezone.utc).date().isoformat()
                selected = [job for job in store.jobs() if not job.get("demo") and job["status"] == "queued"]
                refresh_budget = (len(selected) if queue_rules["daily_limit"] is None else
                                  max(0, queue_rules["daily_limit"] - store.attempt_count(refresh_day)))
                checked_candidates = 0
                tracker.start(total=len(selected))
                for candidate in ordered_queue(selected, queue_rules):
                    # Reload after the prior browser run: a user may have dismissed
                    # this listing or a feed refresh may have changed its details.
                    job = store.job(candidate["id"])
                    if not job:
                        continue
                    if job.get("demo") or job["status"] != "queued":
                        continue
                    profile, rules, resume, approval = configuration()
                    if not valid_approval(profile, rules, resume, approval) or not resume_intact(resume, data_dir):
                        store.event("paused", "Queue paused: the profile, resume or rules need approval.")
                        finish_status = "paused"
                        break
                    day = datetime.now(timezone.utc).date().isoformat()
                    if rules["daily_limit"] is not None and store.attempt_count(day) >= rules["daily_limit"]:
                        store.event("paused", "Daily submission-attempt limit reached (UTC day). The remaining queue is saved.")
                        finish_status = "paused"
                        break
                    if company_scope(rules) == "selected" and job.get("company", "").casefold() not in {company.casefold() for company in rules["companies"]}:
                        continue
                    reasons = matching_problems(job, rules, profile)
                    if reasons:
                        store.update_job(job["id"], "needs_attention", error=" ".join(reasons))
                        tracker.report({"stage":"needs_attention"}, job)
                        tracker.outcome(job, {"status":"needs_attention"})
                        continue
                    if store.destination_attempted(job):
                        store.update_job(job["id"], "needs_attention", error="This posting or a listing with the same employer and title is already recorded as applied. Review its recorded result; automatic retry is disabled.")
                        tracker.report({"stage":"needs_attention"}, job)
                        tracker.outcome(job, {"status":"needs_attention"})
                        continue
                    if store.get("stop_requested", False):
                        finish_status = "paused"
                        break
                    # Saved rows can outlive a bounded discovery refresh. Recheck
                    # only candidates already passing the stored rules, then rank
                    # their current official description before sharing any data.
                    if checked_candidates >= refresh_budget:
                        store.event("paused", "This run reached its bounded posting-verification budget. Remaining roles are saved for a later run.")
                        finish_status = "paused"
                        break
                    checked_candidates += 1
                    tracker.report({"stage": "verifying_posting"}, job)
                    refreshed = await refresh_candidate(job)
                    profile, rules, resume, approval = configuration()
                    if store.get("stop_requested", False) or not valid_approval(profile, rules, resume, approval) or not resume_intact(resume, data_dir):
                        store.event("paused", "Queue paused during posting verification; review the current configuration before restarting.")
                        finish_status = "paused"
                        break
                    latest = store.job(job["id"])
                    if not latest or latest["status"] != "queued":
                        continue
                    detail = rank_job(refreshed, profile, resume.get("text", ""), rules)
                    refreshed.update(score=detail.get("score"), score_detail=detail)
                    store.save_job(refreshed)
                    job = store.job(job["id"])
                    verification = refreshed.get("candidate_refresh", {})
                    if verification.get("status") != "verified":
                        store.update_job(job["id"], "needs_attention", error=verification.get("message", "The current employer posting could not be verified. Refresh and review this role."))
                        tracker.report({"stage":"needs_attention"}, job)
                        tracker.outcome(job, {"status":"needs_attention"})
                        continue
                    reasons = matching_problems(job, rules, profile)
                    if store.destination_attempted(job):
                        reasons.append("This employer posting is already recorded as applied; automatic retry is disabled.")
                    if reasons:
                        store.update_job(job["id"], "needs_attention", error=" ".join(reasons))
                        tracker.report({"stage":"needs_attention"}, job)
                        tracker.outcome(job, {"status":"needs_attention"})
                        continue
                    job = store.claim_job(job["id"])
                    if not job:
                        continue
                    expected = fingerprint(profile, rules, resume)
                    expected_job = (job.get("apply_url"), job.get("title"), job.get("description"))

                    async def before_submit(job_id=job["id"], expected_fingerprint=expected, expected_job_data=expected_job):
                        latest_profile, latest_rules, latest_resume, latest_approval = configuration()
                        if store.get("stop_requested", False):
                            raise ValueError("Queue was stopped before submission.")
                        if not valid_approval(latest_profile, latest_rules, latest_resume, latest_approval) or fingerprint(latest_profile, latest_rules, latest_resume) != expected_fingerprint:
                            raise ValueError("Configuration changed; approve it before submitting.")
                        if not resume_intact(latest_resume, data_dir):
                            raise ValueError("Resume file changed; upload and approve it again.")
                        fresh = store.job(job_id)
                        if (fresh.get("apply_url"), fresh.get("title"), fresh.get("description")) != expected_job_data:
                            raise ValueError("Job details changed during filling; recheck this application.")
                        remaining = matching_problems(fresh, latest_rules, latest_profile)
                        if remaining:
                            raise ValueError(" ".join(remaining))
                        store.reserve_attempt(job_id, datetime.now(timezone.utc).date().isoformat(), latest_rules["daily_limit"])

                    async def on_progress(event, active_job=job):
                        tracker.report(event, active_job)

                    result = await run_application(job, profile, data_dir / "resumes" / resume["stored_name"], allowed_hosts=set(SUPPORTED_HOSTS), artifacts_dir=data_dir / "artifacts" / job["id"], submit=True, before_submit=before_submit, on_progress=on_progress)
                    result_status = result.get("status", "needs_attention")
                    if result_status not in {"submitted", "needs_attention", "uncertain", "failed"}:
                        result_status = "needs_attention"
                    pending_questions = result.get("pending_questions", [])
                    latest = store.job(job["id"])
                    observed = latest.get("form_questions", latest.get("pending_questions", [])) or []
                    changed = {_label(item.get("question")) for item in pending_questions}
                    # Actual worker observations supersede an earlier inspection.
                    # Pending questions are a partial snapshot without stable
                    # control IDs: preserve existing ambiguous controls rather
                    # than guessing which same-label control was replaced.
                    conflicting = {question for question in changed if len(_recorded_candidates(latest, question)) > 1}
                    observed = [item for item in observed if _label(item.get("question")) not in changed
                                or _label(item.get("question")) in conflicting] + pending_questions
                    store.update_job(job["id"], result_status, message=result.get("message"), error=None if result_status == "submitted" else result.get("message"), confirmation=result.get("confirmation"), applied_at=now() if result_status == "submitted" else None, pending_questions=pending_questions, form_questions=observed,
                                     **({"questions_refreshed_at": now()} if pending_questions else {}), screenshot=result.get("screenshot"))
                    tracker.report({"stage": "finished" if result_status == "submitted" else result_status,
                                    "pending_question_count": len(result.get("pending_questions", []))}, job)
                    tracker.outcome(job, {"status": result_status})
                    store.event(result_status, f"{job['company']} — {job['title']}: {result.get('message', result_status)}", job["id"])
            except Exception as error:
                # Don't print profile contents or requests into logs.
                store.event("error", f"Queue stopped: {type(error).__name__}. Check Activity and the application state before retrying.")
                store.recover()
                finish_status = "failed"
            finally:
                if store.get("stop_requested", False) or not approved():
                    finish_status = "paused"
                tracker.finish(status=finish_status)
                state["running"] = False


    async def heartbeat():
        while True:
            tracker.heartbeat()
            await asyncio.sleep(2)

    async def scheduler():
        next_run = 0.0
        while True:
            await asyncio.sleep(5)
            if not store.get("automation", {}).get("enabled") or not approved() or worker_busy():
                continue
            clock = asyncio.get_running_loop().time()
            if clock < next_run:
                continue
            _, rules, _, _ = configuration()
            next_run = clock + rules["interval_minutes"] * 60
            try:
                await fetch()
                if not worker_busy() and store.get("automation", {}).get("enabled") and approved():
                    add_matching_jobs(scheduled=True)
                    _, rules, _, _ = configuration()
                    if any(submission_check(job, rules)["eligible"] for job in store.jobs() if not job.get("demo") and job["status"] == "queued"):
                        await run_queue()
            except Exception as error:
                store.event("error", f"Scheduled fetch failed ({type(error).__name__}). Existing submissions will not be retried.")

    @asynccontextmanager
    async def lifespan(app):
        # Resume only an explicitly enabled, still-approved schedule. A normal
        # shutdown's temporary stop flag must not silently stall that schedule.
        store.set("stop_requested", not (store.get("automation", {}).get("enabled", False) and approved()))
        scheduler_task = asyncio.create_task(scheduler())
        heartbeat_task = asyncio.create_task(heartbeat())
        yield
        store.set("stop_requested", True)
        scheduler_task.cancel()
        heartbeat_task.cancel()
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        tasks = [scheduler_task, heartbeat_task] + ([state["task"]] if state["task"] else [])
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Jobot", lifespan=lifespan, docs_url="/api/docs")
    app.state.store = store
    app.state.data_dir = data_dir
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    register_answer_memory_routes(app, store, invalidate)

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            # Intercepted mock browser navigations use an opaque Origin. Only the
            # synthetic sink accepts it, and that sink requires a one-use form token.
            mock_origin = origin == "null" and request.url.path == "/mock/thanks"
            if origin and origin != expected and not mock_origin:
                return JSONResponse({"detail": "Only requests from this local app are accepted."}, status_code=403)
            if request.url.path.startswith("/api/") and request.headers.get("x-jobradar-client") != "dashboard":
                return JSONResponse({"detail": "Missing local dashboard request header."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/state")
    def get_state():
        profile, rules, resume, approval = configuration()
        run_snapshot = tracker.snapshot()
        if run_snapshot.get("mode") == "demo":
            run_snapshot = {"status": "idle", "stage": "idle", "message": "No application run recorded yet", "worker_alive": False}
        stored_jobs = store.jobs()
        demo_ids = {job["id"] for job in stored_jobs if job.get("demo")}
        jobs = [job for job in stored_jobs if not job.get("demo")]
        selected = selected_destinations(jobs)
        real_jobs = [job for job in jobs if not job.get("demo")]
        for job in real_jobs:
            job["submission_check"] = submission_check(job, rules, profile)
            job["queue_actions"] = queue_actions(job, selected)
            job["unanswered_question_count"], job["conflicting_answer_count"] = unanswered_question_counts(job, profile)
        for job in jobs:
            job["browser_capture_available"] = capture_path(job) is not None
            job.pop("screenshot", None)
            job["facts"] = job_facts(job)
            # Preserve source text in SQLite for ranking and the pre-submit
            # refresh, without resending full postings on every status poll.
            for field in ("description", "requirements", "required_skills", "preferred_skills", "published_pay", "pay_description"):
                job.pop(field, None)
        return {
            "profile": profile, "rules": rules,
            "resume": {"loaded": resume.get("loaded", False), "filename": resume.get("filename")},
            "approval": {"approved": valid_approval(profile, rules, resume, approval), "approved_at": approval.get("approved_at"),
                         "candidate_fingerprint": fingerprint(profile, rules, resume)},
            "jobs": jobs, "events": application_events(store.events(), demo_ids), "answer_memory": answer_memory_state(profile, jobs), "run": run_snapshot,
            "stats": {"total": len(real_jobs), "discovered": sum(job["status"] == "new" for job in real_jobs),
                      "queued": sum(job["status"] == "queued" for job in real_jobs),
                      "submitted": sum(job["status"] == "submitted" for job in real_jobs),
                      "needs_attention": sum(job["status"] in ("needs_attention", "uncertain", "failed") for job in real_jobs)},
            "queue": queue_readiness(jobs, profile, rules, resume, approval),
            "automation": {"enabled": store.get("automation", {}).get("enabled", False), "running": worker_busy(), "fetching": state["fetching"]},
            "last_fetch": store.get("last_fetch"), "discovery": store.get("last_fetch_summary", {}),
            "setup_problems": configuration_problems(profile, rules, resume),
        }

    def capture_path(job):
        try:
            if not job.get("id") or job["id"] in {".", ".."} or Path(job["id"]).name != job["id"]:
                return None
            supplied = job.get("screenshot")
            target = Path(supplied).resolve() if isinstance(supplied, str) else None
            directory = (data_dir / "artifacts" / job["id"]).resolve()
            artifact_root = (data_dir / "artifacts").resolve()
            if directory == artifact_root or not directory.is_relative_to(artifact_root):
                return None
            if target and target.is_relative_to(directory) and target.suffix.lower() == ".png" and target.is_file():
                return target
        except (ValueError, OSError, TypeError):
            pass
        return None

    @app.get("/api/jobs/{job_id}/screenshot")
    def browser_capture(job_id: str):
        job = store.job(job_id)
        target = capture_path(job) if job else None
        if target is None:
            raise HTTPException(404, "No recorded browser capture is available for this application.")
        return FileResponse(target, media_type="image/png")

    async def refresh_form_questions(job_id, *, question=None, query=None):
        from .autofill import _label, plan_form
        from .question_lookup import inspect_questions

        job = store.job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found.")
        if (job.get("demo") or job.get("attempted_at") or store.destination_attempted(job)
                or job["status"] in {"active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"}):
            raise HTTPException(409, "Only an unattempted application can refresh its form questions.")
        if not verified_destination(job) or job.get("closed") is True:
            raise HTTPException(409, "Refresh this job's verified employer link before reading its form questions.")
        if worker_busy() or state["fetching"] or question_lock.locked():
            raise HTTPException(409, "Wait for the current application, fetch, or question lookup to finish.")
        async with question_lock:
            inspected = await inspect_questions(job, question=question, query=query or None)
            if inspected.get("status") != "inspected":
                raise HTTPException(422, inspected.get("message") or "The employer's current choices could not be read.")
            fresh = store.job(job_id)
            if (fresh is None or fresh.get("apply_url") != job.get("apply_url") or fresh.get("attempted_at")
                    or worker_busy() or state["fetching"] or store.destination_attempted(fresh)
                    or fresh.get("closed") is True or not verified_destination(fresh)
                    or fresh["status"] in {"active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"}):
                raise HTTPException(409, "The application changed during inspection. Refresh its status before trying again.")
            fields = inspected.get("fields") or []
            public_questions = plan_form(fields, {}, None).pending_questions
            profile = configuration()[0]
            pending = plan_form(fields, profile, None).pending_questions
            if question is not None:
                expected = _label(question)
                selected = [item for item in public_questions if item["question"] == expected]
                if len(selected) != 1:
                    raise HTTPException(422, "The current form did not expose one reusable control for this question.")
                # A targeted lookup refreshes only that question: unrelated
                # dropdowns have not been opened and must keep their choices.
                public_questions = [item for item in fresh.get("form_questions", fresh.get("pending_questions", []))
                                    if item.get("question") != expected] + selected
                pending = [item for item in fresh.get("pending_questions", []) if item.get("question") != expected] + [
                    item for item in pending if item["question"] == expected]
            store.update_job(job_id, fresh["status"], form_questions=public_questions, pending_questions=pending,
                             questions_refreshed_at=now())
            store.event("questions", "Current employer questions were read without filling or submitting an application.", job_id)
            if question is not None:
                selected = next(item for item in public_questions if item["question"] == _label(question))
                return {"question": selected["question"], "options": selected.get("options", []),
                        "searchable": selected.get("searchable", False), "lookup_required": selected.get("lookup_required", False),
                        "message": "Choose a returned employer option, then confirm it." if selected.get("options") else
                                   "No choices returned. Try a more specific search or refresh the form questions."}
            return {"questions": len(public_questions), "pending": len(pending),
                    "message": "Employer questions and choices refreshed. No answers were changed and no application was submitted."}

    @app.post("/api/jobs/{job_id}/questions/refresh")
    async def refresh_questions(job_id: str):
        return await refresh_form_questions(job_id)

    @app.post("/api/jobs/{job_id}/questions/options")
    async def lookup_question(job_id: str, body: QuestionLookupRequest):
        return await refresh_form_questions(job_id, question=body.question, query=body.query)

    @app.post("/api/profile")
    async def save_profile(profile: Profile):
        store.set("profile", profile.model_dump())
        invalidate()
        rank_all()
        store.event("profile", "Profile saved. Previous submission approval was revoked.")
        return {"ok": True}

    @app.post("/api/rules")
    async def save_rules(rules: Rules):
        store.set("rules", rules.model_dump())
        invalidate()
        rank_all()
        store.event("rules", "Rules saved. Review and approve this configuration before submissions.")
        return {"ok": True}

    @app.post("/api/resume")
    async def upload_resume(file: UploadFile = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".txt"}:
            raise HTTPException(400, "Upload a PDF or UTF-8 text resume.")
        contents = await file.read(MAX_RESUME_BYTES + 1)
        if len(contents) > MAX_RESUME_BYTES:
            raise HTTPException(413, "Resume exceeds the 5 MB limit.")
        if not contents:
            raise HTTPException(400, "The resume is empty.")
        try:
            if suffix == ".pdf":
                if not contents.startswith(b"%PDF-"):
                    raise ValueError("Not a PDF")
                reader = PdfReader(io.BytesIO(contents))
                if reader.is_encrypted or len(reader.pages) > 50:
                    raise ValueError("Encrypted or too many pages")
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
            else:
                text = contents.decode("utf-8-sig")
        except Exception:
            raise HTTPException(400, "Couldn't read that resume. Use an unencrypted, text-based PDF or UTF-8 .txt file.")
        if not text.strip() or len(text) > 100_000 or "\x00" in text:
            raise HTTPException(400, "Resume text is empty, malformed, or exceeds 100,000 characters. Image-only PDFs need text extraction first.")
        directory = data_dir / "resumes"
        directory.mkdir(parents=True, exist_ok=True)
        stored_name = uuid.uuid4().hex + suffix
        (directory / stored_name).write_bytes(contents)
        store.set("resume", {"loaded": True, "filename": Path(file.filename).name, "stored_name": stored_name, "sha256": hashlib.sha256(contents).hexdigest(), "text": text})
        invalidate()
        rank_all()
        store.event("resume", "Resume saved locally. Previous submission approval was revoked.")
        return {"ok": True}

    @app.post("/api/approve")
    async def approve(body: ApprovalRequest):
        if body.acknowledge is not True:
            raise HTTPException(400, "Review and acknowledge the exact profile, resume and employer rules first.")
        profile, rules, resume, _ = configuration()
        if body.expected_fingerprint != fingerprint(profile, rules, resume):
            raise HTTPException(409, "Settings changed since your review. Reload and review the current profile, resume and rules.")
        problems = configuration_problems(profile, rules, resume)
        if problems:
            raise HTTPException(400, " ".join(problems))
        store.set("approval", {"approved": True, "approved_at": now(), "fingerprint": fingerprint(profile, rules, resume)})
        store.set("stop_requested", False)
        store.event("approval", "Auto-submit approved for the exact current profile, resume, employers, destinations and limits.")
        return {"ok": True}

    @app.post("/api/fetch")
    async def fetch_endpoint():
        try:
            return await fetch()
        except Exception as error:
            store.event("error", f"Public job fetch failed ({type(error).__name__}). Try again later; saved jobs are intact.")
            raise HTTPException(502, "Couldn't refresh the public job source. Saved jobs are still available.")

    @app.post("/api/run")
    async def run_endpoint():
        if not approved():
            raise HTTPException(409, "Approve your verified profile, resume and submission rules first.")
        if worker_busy():
            raise HTTPException(409, "The queue is already running.")
        snapshot = get_state()
        if not snapshot["queue"]["can_run"]:
            raise HTTPException(409, " ".join(snapshot["queue"]["blockers"]))
        store.set("stop_requested", False)
        state["running"] = True
        state["task"] = asyncio.create_task(run_queue())
        return {"message": f"Approved queue started with {snapshot['queue']['ready']} eligible role(s).", "ready": snapshot["queue"]["ready"]}

    @app.post("/api/queue/matching")
    async def queue_matching_endpoint():
        result = add_matching_jobs()
        message = (f"Loaded {result['added']} role(s) scoring at least {result['min_score']}/100 into your application queue. "
                   f"{result['ready']} ready; {result['blocked']} need review. Submission checks still apply."
                   if result["added"] else f"No additional available roles meet your {result['min_score']}/100 score threshold. "
                   "Already queued, active, attempted and duplicate applications are excluded.")
        return {**result, "message": message}

    @app.post("/api/automation")
    async def automation_endpoint(body: AutomationRequest):
        enabled = body.enabled
        if enabled and not approved():
            raise HTTPException(409, "Approve the current profile and rules before enabling automation.")
        if enabled:
            problems = configuration_problems(*configuration()[:3])
            if problems:
                raise HTTPException(409, " ".join(problems))
        store.set("automation", {"enabled": enabled})
        store.set("stop_requested", not enabled)
        store.event("automation", "Scheduled automation enabled while this app is running." if enabled else "Automation stopped; an in-flight click may already have completed. Check its recorded result.")
        return {"ok": True}

    @app.post("/api/run/stop")
    async def stop_run():
        store.set("stop_requested", True)
        store.set("automation", {"enabled": False})
        tracker.request_stop()
        store.event("stop_requested", "Stop requested. No further submission will start; an application already sent cannot be undone.")
        return {"ok": True, "message": "Stop requested before the next submission. Scheduling is off."}

    @app.post("/api/jobs/{job_id}/status")
    async def job_status(job_id: str, body: JobStatusRequest):
        status = body.status
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        if job["status"] in {"active", "applying", "filling", "submitting", "submitted", "uncertain", "applied_elsewhere"} or job.get("attempted_at"):
            raise HTTPException(409, "An active or attempted application cannot be requeued automatically. Verify its outcome first.")
        actions = queue_actions(job)
        already_selected = job["status"] == "queued" and not job.get("demo")
        if status == "queued" and (not (actions["can_add"] or already_selected) or store.destination_attempted(job)):
            raise HTTPException(409, actions["reason"] or "This record cannot be added to the application queue.")
        if status == "new" and not actions["can_remove"]:
            raise HTTPException(409, "Only a selected queued role can be removed from the queue.")
        if status == "queued" and already_selected:
            return {"ok": True, "status": status, "already_queued": True, "message": actions["reason"]}
        store.update_job(job_id, status, error=None)
        return {"ok": True, "status": status, "already_queued": False}

    @app.post("/api/applied-elsewhere")
    async def applied_elsewhere(body: ExternalApplicationRequest):
        try:
            result = store.record_external_application(url=body.url, job_id=body.job_id)
        except LookupError as error:
            raise HTTPException(404, str(error))
        except ValueError as error:
            raise HTTPException(400, str(error))
        if result["recorded"] or result["updated_jobs"]:
            message = "A previous application was recorded. Listings with the same employer and title are held for review, including possible reposts." if result.get("identity_key") else "A previously submitted employer posting was recorded. Matching listings will not be submitted automatically."
            store.event("applied_elsewhere", message)
        return {"ok": True, **result}

    async def demo(base_url):
        from .autofill import run_application
        state["demo_running"] = True
        job_id = "demo-" + uuid.uuid4().hex[:12]
        job = {"id": job_id, "company": "Local demo employer", "title": "Backend engineer · synthetic application", "location": "Local mock form", "ats": "local demo", "apply_url": base_url + "/mock/apply", "description": "Synthetic test only. No real employer receives data.", "score": None, "score_detail": {"matches": [], "not_evidenced": [], "eligibility": [], "reasons": ["Local fixture: no real resume or employer."]}, "status": "new", "demo": True}
        store.save_job(job)
        store.update_job(job_id, "filling")
        tracker.start(mode="demo", total=1)
        store.event("demo", "Starting a synthetic application on this computer only.", job_id)
        demo_status = "completed"
        try:
            path = data_dir / "demo-resume.txt"
            path.write_text("Synthetic demo resume. Alex Example: Java, REST APIs, AWS. No real person or credentials.", encoding="utf-8")
            profile = Profile(name="Alex Example", email="alex@example.test", phone="555-0100", current_company="Example Studio", location="Boston, MA").model_dump()

            async def before_submit():
                if store.get("stop_requested", False):
                    raise ValueError("Local demonstration was stopped before submission.")
                store.reserve_attempt(job_id, "demo-only", 1_000_000)

            async def on_progress(event):
                tracker.report(event, job)

            result = await run_application(job, profile, path, allowed_hosts={"127.0.0.1"}, artifacts_dir=data_dir / "artifacts" / job_id, submit=True, fixture=True, before_submit=before_submit, on_progress=on_progress)
            store.update_job(job_id, result["status"], message=result.get("message"), error=None if result["status"] == "submitted" else result.get("message"), confirmation=result.get("confirmation"), pending_questions=result.get("pending_questions", []), screenshot=result.get("screenshot"))
            tracker.outcome(job, result)
            store.event("demo", f"Local mock application: {result.get('message', result['status'])}", job_id)
        except Exception as error:
            demo_status = "failed"
            store.update_job(job_id, "failed", error=f"Demo failed ({type(error).__name__}). Verify browser installation.")
            tracker.report({"stage": "failed"}, job)
            tracker.outcome(job, {"status": "failed"})
            store.event("error", "Local demo failed. Run the documented Playwright browser installation and try again.", job_id)
        finally:
            tracker.finish(status="paused" if store.get("stop_requested", False) else demo_status)
            state["demo_running"] = False

    @app.post("/api/demo")
    async def demo_endpoint(request: Request):
        if worker_busy():
            raise HTTPException(409, "A local demo is already running.")
        base_url = str(request.base_url).rstrip("/")
        # The fixture runner is deliberately loopback-only even if Host is localhost.
        base_url = base_url.replace("http://localhost:", "http://127.0.0.1:")
        state["demo_running"] = True
        store.set("stop_requested", False)
        state["task"] = asyncio.create_task(demo(base_url))
        return {"message": "Local mock application started. No real employer will receive data."}

    @app.get("/mock/apply", response_class=HTMLResponse)
    def mock_form(variant: str = "standard"):
        token = uuid.uuid4().hex
        if len(state["mock_tokens"]) > 100:
            state["mock_tokens"].clear()
        state["mock_tokens"].add(token)
        extra = '<label>Secret project experience <input name="secret_experience" required></label>' if variant == "unknown" else '<label><input type="checkbox" name="terms" required> I agree to the terms and certify this application</label>' if variant == "agreement" else ''
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Local mock application</title><style>body{{font:16px system-ui;max-width:650px;margin:60px auto;padding:24px;background:#f5f6f8}}form{{display:grid;gap:20px;background:white;padding:32px;border-radius:20px}}label{{display:grid;gap:8px}}input,button{{padding:12px;font:inherit}}small{{color:#596777}}</style></head><body><h1>Local mock employer</h1><p>Synthetic application fixture. This is not a job opening.</p><form method="post" enctype="multipart/form-data" action="/mock/thanks"><input type="hidden" name="mock_token" value="{token}"><label>Full name <input name="name" required></label><label>Email <input name="email" type="email" required></label><label>Phone <input name="phone"></label><label>Current company <input name="org"></label><label>Current location <input name="location"></label><label>Resume/CV <input type="file" name="resume" required></label>{extra}<button type="submit">Submit application</button></form></body></html>'''

    @app.post("/mock/thanks", response_class=HTMLResponse)
    async def mock_submit(request: Request):
        form = await request.form()
        # Only the synthetic demo identity can be sent to this test sink.
        if form.get("name") != "Alex Example" or form.get("email") != "alex@example.test":
            raise HTTPException(400, "This fixture accepts the synthetic demo identity only.")
        attachment = form.get("resume")
        if not hasattr(attachment, "read") or not (await attachment.read()).startswith(b"Synthetic demo resume."):
            raise HTTPException(400, "A synthetic resume attachment is required.")
        token = form.get("mock_token")
        if token not in state["mock_tokens"]:
            raise HTTPException(403, "The mock form token is missing or already used.")
        state["mock_tokens"].remove(token)
        receipt = store.receipt("Alex Example", "alex@example.test")
        return f'<html lang="en"><head><title>Application submitted — local demo</title></head><body><h1>Your application has been submitted</h1><p>Local demo confirmation #{receipt}. No real employer was contacted.</p></body></html>'

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
