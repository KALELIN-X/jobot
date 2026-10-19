"""Observable worker steps, independent of application approval or submission."""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timezone

from .store import now


STAGES = {
    "starting": "Starting the worker",
    "verifying_posting": "Checking the current employer posting",
    "opening_form": "Opening the employer application form",
    "inspecting_form": "Reading application fields and questions",
    "filling_fields": "Filling approved fields",
    "uploading_resume": "Uploading the approved resume",
    "checking_answers": "Checking required answers and approval",
    "submitting": "Sending the single application request",
    "awaiting_confirmation": "Waiting for the employer's confirmation",
    "finished": "Employer confirmation received",
    "needs_attention": "Paused for your review",
    "uncertain": "Submission outcome needs verification",
    "failed": "Application could not be completed",
    "completed": "Run finished",
    "paused": "Run paused",
    "stopping": "Stop requested before the next submission",
    "interrupted": "The previous worker stopped before the run finished",
}


class RunTracker:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.value = store.get("last_run", {})
        self.recorded = set()
        if self.value.get("status") in {"running", "stopping"}:
            self.value.update(status="interrupted", stage="interrupted", message=STAGES["interrupted"],
                              updated_at=now(), finished_at=now())
            self._save()
            store.event("demo_run_interrupted" if self.value.get("mode") == "demo" else "run_interrupted", "The previous run was interrupted. Check recorded outcomes before retrying.")

    def _save(self):
        self.store.set("last_run", self.value)

    def start(self, *, mode="queue", total=0):
        with self.lock:
            stamp = now()
            self.recorded.clear()
            self.value = {"id": uuid.uuid4().hex, "mode": mode, "status": "running",
                          "stage": "starting", "message": STAGES["starting"], "started_at": stamp,
                          "updated_at": stamp, "heartbeat_at": stamp, "finished_at": None,
                          "active_job": None, "total": max(0, int(total)), "processed": 0,
                          "submitted": 0, "needs_attention": 0, "uncertain": 0, "failed": 0,
                          "field_count": None, "completed_fields": None, "pending_question_count": 0,
                          "timeline": []}
            self._save()
            self.store.event("demo_run_started" if mode == "demo" else "run_started", f"{'Local demo' if mode == 'demo' else 'Application queue'} worker started with {self.value['total']} selected role(s).")

    def heartbeat(self):
        with self.lock:
            if self.value.get("status") in {"running", "stopping"}:
                self.value["heartbeat_at"] = now()

    def report(self, event, job=None):
        stage = event.get("stage")
        if stage not in STAGES:
            return
        with self.lock:
            if self.value.get("status") not in {"running", "stopping"}:
                return
            previous = (self.value.get("stage"), (self.value.get("active_job") or {}).get("id"))
            if job:
                selected = {key: str(job.get(key, ""))[:500] for key in ("id", "company", "title")}
                if (self.value.get("active_job") or {}).get("id") != selected["id"]:
                    self.value.update(field_count=None, completed_fields=None, pending_question_count=0)
                self.value["active_job"] = selected
            self.value.update(stage=stage, message=STAGES[stage], updated_at=now())
            for key in ("field_count", "completed_fields", "pending_question_count"):
                amount = event.get(key)
                if isinstance(amount, int) and not isinstance(amount, bool) and 0 <= amount <= 1000:
                    self.value[key] = amount
            current = (stage, (self.value.get("active_job") or {}).get("id"))
            if current != previous:
                item = {"stage": stage, "message": STAGES[stage], "at": self.value["updated_at"],
                        "job_id": current[1]}
                self.value["timeline"] = (self.value.get("timeline", []) + [item])[-24:]
                active = self.value.get("active_job") or {}
                prefix = f"{active.get('company', '')} — {active.get('title', '')}: " if active else ""
                self.store.event("demo_progress" if self.value.get("mode") == "demo" else "application_progress", prefix + STAGES[stage], current[1])
            self._save()

    def outcome(self, job, result):
        with self.lock:
            if job["id"] in self.recorded:
                return
            self.recorded.add(job["id"])
            self.value["processed"] += 1
            status = result.get("status", "needs_attention")
            if status in {"submitted", "needs_attention", "uncertain", "failed"}:
                self.value[status] += 1
            self._save()

    def request_stop(self):
        with self.lock:
            if self.value.get("status") != "running":
                return
            self.report({"stage": "stopping"})
            self.value["status"] = "stopping"
            self._save()

    def finish(self, *, status="completed"):
        if status not in {"completed", "paused", "failed", "interrupted"}:
            raise ValueError("Unknown terminal run state")
        with self.lock:
            if self.value.get("status") not in {"running", "stopping"}:
                return
            if self.value["status"] == "stopping" and status == "completed":
                status = "paused"
            self.value.update(status=status, stage=status, message=STAGES[status],
                              updated_at=now(), finished_at=now())
            self._save()
            self.store.event("demo_run_finished" if self.value.get("mode") == "demo" else "run_finished", f"{STAGES[status]}: {self.value['processed']} processed, {self.value['submitted']} confirmed, {self.value['needs_attention']} need answers/review, {self.value['uncertain']} uncertain, {self.value['failed']} failed.")

    def snapshot(self):
        with self.lock:
            value = copy.deepcopy(self.value)
        if not value:
            return {"status": "idle", "stage": "idle", "message": "No worker run recorded yet"}
        try:
            heartbeat = datetime.fromisoformat(value["heartbeat_at"])
            age = max(0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
        except (KeyError, TypeError, ValueError):
            age = None
        value["heartbeat_age_seconds"] = age
        value["worker_alive"] = value["status"] in {"running", "stopping"} and age is not None and age < 15
        return value
