"""Transactional, opt-in onboarding for a single private local candidate.

Reading old profiles never migrates or rewrites them. Every save merges one
reviewed step, with a revision check under SQLite's write lock. No CV extraction,
employer answers, or defaults are promoted to facts by this module.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from pydantic import Field, StrictBool, ValidationError, field_validator

from .candidate_profile import (
    ApplicationProfile, AvailabilityPeriod, EducationRecord, EmploymentRecord,
    ONBOARDING_STEPS, onboarding_catalog,
)
from .models import Profile, StrictModel
from .approval import material_profile
from .store import now


def profile_revision(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OnboardingRequest(StrictModel):
    step: str = Field(min_length=1, max_length=40)
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_patch: dict[str, Any] = Field(default_factory=dict, max_length=30)
    facts: dict[str, Any] = Field(default_factory=dict, max_length=40)
    education: list[EducationRecord] | None = Field(default=None, max_length=20)
    employment: list[EmploymentRecord] | None = Field(default=None, max_length=40)
    availability: list[AvailabilityPeriod] | None = Field(default=None, max_length=30)
    complete: StrictBool = False

    @field_validator("step")
    @classmethod
    def valid_step(cls, value):
        if value not in ONBOARDING_STEPS:
            raise ValueError("Choose a known onboarding step.")
        return value


def onboarding_state(profile: dict, resume: dict | None = None) -> dict:
    application = ApplicationProfile.model_validate(profile.get("application_profile") or {}).model_dump()
    catalog = onboarding_catalog()
    answered, total = 0, 0
    for step in catalog:
        for field in step["fields"]:
            total += 1
            source = profile if field["storage"] == "profile" else application["facts"]
            value = source.get(field["key"])
            answered += value is not None and value != "" and value != []
    return {
        "version": 1, "catalog": catalog, "profile": profile,
        "application_profile": application, "revision": profile_revision(profile),
        "resume": {"loaded": bool((resume or {}).get("loaded")), "filename": (resume or {}).get("filename")},
        "completion": {
            "completed_steps": application["completed_steps"], "total_steps": len(ONBOARDING_STEPS),
            "answered": answered, "total": total,
            "started": bool(application["completed_steps"] or application["confirmed_at"]),
            "complete": "review" in application["completed_steps"],
        },
    }


def _safe_errors(error: ValidationError) -> list[dict]:
    # Pydantic's default errors include candidate values. UI errors need only the
    # field and explanation; never echo private input into logs or API errors.
    return [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
            for item in error.errors(include_input=False, include_context=False, include_url=False)]


def merge_step(profile: dict, body: OnboardingRequest, *, resume: dict | None = None) -> tuple[dict, list[str]]:
    step = next(item for item in onboarding_catalog() if item["id"] == body.step)
    core = {item["key"] for item in step["fields"] if item["storage"] == "profile"}
    facts = {item["key"] for item in step["fields"] if item["storage"] == "facts"}
    if set(body.profile_patch) - core or set(body.facts) - facts:
        raise HTTPException(422, "Save only the fields shown in this onboarding step.")
    allowed_records = {item["key"] for item in step.get("records", [])}
    if any(key in body.model_fields_set and key not in allowed_records for key in ("education", "employment", "availability")):
        raise HTTPException(422, "Save education, employment and availability in their own steps.")
    if body.complete and body.step != "review":
        raise HTTPException(422, "Complete onboarding from its review step.")
    updated = deepcopy(profile)
    application = ApplicationProfile.model_validate(updated.get("application_profile") or {}).model_dump()
    changed = []
    stamp = now()
    for key, value in body.profile_patch.items():
        if key in {"work_authorized", "needs_sponsorship"} and value is not None and type(value) is not bool:
            raise HTTPException(422, "Work authorization answers must be Yes, No or Unknown.")
        if updated.get(key) != value or type(updated.get(key)) is not type(value):
            changed.append(key)
        updated[key] = value
    for key, value in body.facts.items():
        if application["facts"].get(key) != value or type(application["facts"].get(key)) is not type(value):
            changed.append("facts." + key)
        application["facts"][key] = value
    for key in allowed_records:
        if key in body.model_fields_set:
            records = [item.model_dump() for item in getattr(body, key) or []]
            if records != application[key]:
                changed.append(key)
            application[key] = records
    # Skipping an optional fact remains unknown; completing the walkthrough
    # does not claim every employer question is now answerable.
    completed = application["completed_steps"]
    if body.step != "review" or body.complete:
        if body.step not in completed:
            completed.append(body.step)
    for key in changed:
        application["confirmed_at"][key] = stamp
    updated["application_profile"] = application
    try:
        updated = Profile.model_validate(updated).model_dump()
    except ValidationError as error:
        raise HTTPException(422, {"message": "Check the highlighted profile details.", "fields": _safe_errors(error)}) from None
    if body.complete:
        missing = []
        for key in ("name", "first_name", "last_name", "email"):
            if not updated.get(key):
                missing.append(key.replace("_", " "))
        if not (resume or {}).get("loaded"):
            missing.append("resume")
        if missing:
            raise HTTPException(422, "Add your " + ", ".join(missing) + " before finishing. Other unknowns can stay unanswered.")
    return updated, changed


def register_onboarding_routes(app, store, invalidate, rank_all):
    @app.get("/api/onboarding")
    def get_onboarding():
        return onboarding_state(store.get("profile", Profile().model_dump()), store.get("resume", {}))

    @app.post("/api/onboarding")
    def save_onboarding(body: OnboardingRequest):
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM config WHERE key='profile'").fetchone()
            before = json.loads(row[0]) if row else Profile().model_dump()
            if profile_revision(before) != body.expected_revision:
                raise HTTPException(409, "Your profile changed in another tab. Reload the saved details before continuing; your draft has not been saved.")
            resume_row = connection.execute("SELECT value FROM config WHERE key='resume'").fetchone()
            resume = json.loads(resume_row[0]) if resume_row else {}
            updated, changed = merge_step(before, body, resume=resume)
            material_changed = material_profile(before) != material_profile(updated)
            if updated != before:
                connection.execute("INSERT OR REPLACE INTO config VALUES ('profile',?)", (json.dumps(updated),))
            # Material changes revoke submission approval in the same transaction
            # as the profile write. Never leave a new profile approved by an old
            # configuration, including when another browser tab is active.
            if material_changed:
                for key, value in (("approval", {"approved": False, "approved_at": None}),
                                   ("automation", {"enabled": False}), ("stop_requested", True)):
                    connection.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, json.dumps(value)))
        if material_changed:
            invalidate()
            rank_all()
        store.event("profile", "Application profile step saved locally." + (" Review updated facts before the next run." if material_changed else " No application answers changed."))
        return {**onboarding_state(updated, resume), "changed": material_changed, "approval_required": material_changed}
