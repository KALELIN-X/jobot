"""Project real application activity without deleting local test history."""

import re
from urllib.parse import urlsplit


def application_events(events, demo_job_ids):
    hidden = set()
    demo_run = False
    run_types = {"run_started", "application_progress", "run_finished", "run_interrupted", "stop_requested"}
    # Legacy demo runs used ordinary run event types. Their owned start marker
    # identifies the corresponding run events, while unrelated saves remain.
    for index, event in sorted(enumerate(events), key=lambda item: item[1]["id"]):
        kind = str(event.get("type", ""))
        if kind in {"run_started", "demo_run_started"}:
            demo_run = str(event.get("message", "")).startswith("Local demo worker started")
        if kind == "demo" or kind.startswith("demo_") or event.get("job_id") in demo_job_ids or (demo_run and kind in run_types):
            hidden.add(index)
        if kind in {"run_finished", "run_interrupted", "demo_run_finished", "demo_run_interrupted"}:
            demo_run = False
    return [event for index, event in enumerate(events) if index not in hidden]


def application_review_actions(jobs):
    """Show non-answerable holds separately from reusable personal facts."""
    result = []
    categories = (
        ("captcha", r"captcha|anti[- ]bot|verification challenge"),
        ("login", r"\blog[ -]?in\b|\bsign[ -]?in\b|password|account verification"),
        ("agreement", r"agreement|arbitration|\bnda\b|signature|declaration|legal terms"),
        ("form", r"unsupported|invalid form|validation|destination|outside.*approved|upload"),
    )
    for job in jobs:
        if job.get("demo") or job.get("status") not in {"needs_attention", "failed", "uncertain"}:
            continue
        message = job.get("error") or job.get("message") or ""
        message = message[:1500] if isinstance(message, str) else ""
        kind = "uncertain" if job.get("status") == "uncertain" else next(
            (name for name, pattern in categories if re.search(pattern, message, re.I)), None)
        if kind is None:
            continue
        raw_url = job.get("apply_url") or ""
        try:
            parts = urlsplit(raw_url) if isinstance(raw_url, str) else None
        except ValueError:
            parts = None
        valid_url = bool(parts and parts.scheme == "https" and parts.hostname and not parts.username and not parts.password
                         and not any(ord(c) <= 32 for c in raw_url))
        result.append({"job_id": job["id"], "company": job.get("company", ""), "title": job.get("title", ""),
                       "kind": kind, "message": message or "A previous attempt needs outcome verification before any further action.",
                       "apply_url": raw_url if valid_url else None})
    return result[:200]
