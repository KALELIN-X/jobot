"""Explicit, local question/answer memory for the guarded application worker.

The app learns an answer only when its owner saves it. Unknown form fields
provide review candidates; neither prefilled site values nor guesses become
profile facts. Confirmed intents and scoped bindings share the form planner's
resolver; legacy answers remain an exact-question fallback.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import Field, StrictBool, StrictStr, StringConstraints, ValidationError, field_validator

from .autofill import _label, _option_match, lookup_query_hash, reusable_question, searchable_lookup
from .models import Profile, StrictModel
from .candidate_profile import SavedAnswerBinding
from .question_intents import (CORE_INTENTS, FACT_INTENTS, classify_question, employer_dependent,
                               canonical_boolean, canonical_source_fingerprint, explicit_boolean,
                               field_answer, normalized, pending_group_key, resolve_question)
from .store import now


class AnswerQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value):
        value = _label(value)
        if not value or "\x00" in value:
            raise ValueError("Enter the exact visible question.")
        return value


class ForgetAnswerRequest(AnswerQuestion):
    scope: Literal["global", "employer", "application"] | None = None
    company: str | None = Field(default=None, min_length=1, max_length=240)
    job_id: str | None = Field(default=None, min_length=1, max_length=200)


class SaveAnswerRequest(AnswerQuestion):
    answer: StrictStr | StrictBool
    job_id: str | None = Field(default=None, min_length=1, max_length=200)
    scope: Literal["global", "employer", "application"] | None = None
    intent_id: str | None = Field(default=None, min_length=1, max_length=160)
    confirm_intent: StrictBool = False
    approve_and_continue: StrictBool = False
    expected_fingerprint: str | None = Field(default=None, min_length=1, max_length=64)
    choice_label: Annotated[StrictStr, StringConstraints(strip_whitespace=False)] | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value):
        if isinstance(value, str) and (not value.strip() or len(value) > 4000 or "\x00" in value):
            raise ValueError("Enter an answer of at most 4,000 characters.")
        return value.strip() if isinstance(value, str) else value

    @field_validator("question")
    @classmethod
    def ordinary_question_only(cls, value):
        if not reusable_question(value):
            raise ValueError("Agreements, credentials and verification challenges cannot be saved as reusable answers.")
        return value


def _candidate_answer(candidate, answer):
    """Validate the actual control without approximating wording or choices."""
    kind = candidate.get("type", "text")
    if candidate.get("multiple"):
        raise ValueError("Multiple-selection controls require manual review.")
    if kind == "checkbox":
        if type(answer) is not bool:
            raise ValueError("This question needs an explicit true or false answer.")
        return answer
    if kind in {"radio", "select"}:
        options = candidate.get("options") or []
        match = _option_match(options, answer)
        if match is None:
            if kind == "select" and searchable_lookup(candidate):
                if not isinstance(answer, str) or not answer.strip() or len(answer) > 240:
                    raise ValueError("Enter a search query of at most 240 characters; the form must still provide an exact matching choice.")
                minimum = candidate.get("minimum_query_length")
                if type(minimum) is int and 1 <= minimum <= 240 and len(answer.strip()) < minimum:
                    raise ValueError(f"This dropdown asks for at least {minimum} characters before searching.")
                if candidate.get("lookup_query_hash") == lookup_query_hash(answer):
                    raise ValueError("That search did not provide an exact available choice. Try a more specific query or review the employer form manually.")
                # This permits a future lookup, not an invented select option.
                # The worker still needs one real, exact visible option to click.
                return answer.strip()
            raise ValueError("Choose one exact available answer for this question.")
        # Visible labels usually survive across forms better than opaque values.
        label = str(match["label"])
        return label if sum(option.get("label") == label for option in options) == 1 else str(match["value"])
    if not isinstance(answer, str):
        raise ValueError("This question needs a written answer, not a boolean.")
    return answer


def _recorded_candidates(job, question):
    """Current full-form metadata wins over an older unresolved-question copy."""
    def matching(key):
        values = job.get(key)
        return [value for value in values[:60] if isinstance(value, dict) and _label(value.get("question")) == question] if isinstance(values, list) else []
    values = matching("form_questions") or matching("pending_questions")
    result, signatures = [], set()
    for value in values:
        signature = json.dumps({key: value.get(key) for key in
                                ("type", "options", "searchable", "lookup_required", "minimum_query_length", "multiple", "required", "ambiguous_record_control", "record_control_name")}, sort_keys=True)
        if signature not in signatures:
            signatures.add(signature)
            result.append(value)
    return result


def _answer_source(job, candidate):
    options = []
    raw_options = candidate.get("options")
    for option in raw_options[:300] if isinstance(raw_options, list) else []:
        if not isinstance(option, dict) or option.get("disabled"):
            continue
        label, value = str(option.get("label") or "").strip(), str(option.get("value", ""))
        if label and value and len(label) <= 500 and len(value) <= 500 and {"label": label, "value": value} not in options:
            options.append({"label": label, "value": value})
    source = {"job_id": job["id"], "company": str(job.get("company") or "")[:500], "title": str(job.get("title") or "")[:500],
              "type": str(candidate.get("type") or "text")[:30], "required": bool(candidate.get("required")), "options": options,
              "searchable": candidate.get("searchable") is True, "lookup_required": searchable_lookup(candidate)}
    minimum = candidate.get("minimum_query_length")
    if type(minimum) is int and 1 <= minimum <= 240:
        source["minimum_query_length"] = minimum
    if candidate.get("multiple"):
        source["multiple"] = True
    return source


def answer_memory_state(profile, jobs):
    answers = profile.get("answers") or {}
    pending = []
    lookup_questions = set()
    binding_questions = {binding.get("question") for binding in (profile.get("application_profile") or {}).get("saved_answer_bindings", [])
                         if isinstance(binding, dict) and isinstance(binding.get("question"), str)}
    sources = {question: [] for question in set(answers) | binding_questions}
    for job in jobs:
        if job.get("demo"):
            continue
        observed = [candidate for key in ("form_questions", "pending_questions")
                    for candidate in (job[key][:60] if isinstance(job.get(key), list) else []) if isinstance(candidate, dict)]
        questions = {_label(candidate.get("question")) for candidate in observed}
        for question in questions & sources.keys():
            if not reusable_question(question) or len(sources[question]) >= 20:
                continue
            candidates = _recorded_candidates(job, question)
            if not candidates:
                continue
            source = _answer_source(job, candidates[0])
            if len(candidates) > 1:
                source.update(type="unsupported", options=[], searchable=False, lookup_required=False, conflicting_controls=True)
            if not any(previous["job_id"] == source["job_id"] for previous in sources[question]):
                sources[question].append(source)
            if source["lookup_required"] and isinstance(answers.get(question), str):
                lookup_questions.add(question)
        seen = set()
        for candidate in job.get("pending_questions") or []:
            if not isinstance(candidate, dict):
                continue
            question = _label(candidate.get("question"))
            if question in seen or not reusable_question(question):
                continue
            seen.add(question)
            current = _recorded_candidates(job, question)
            ambiguous = len(current) > 1
            if len(current) == 1:
                authoritative = current[0]
                if searchable_lookup(authoritative) and searchable_lookup(candidate) and candidate.get("lookup_query_hash"):
                    authoritative = {**authoritative, "lookup_query_hash": candidate["lookup_query_hash"]}
                candidate = authoritative
            if ambiguous:
                candidate = {**candidate, "type": "unsupported", "options": [], "searchable": False, "lookup_required": False,
                             "conflicting_controls": True}
            item = {**candidate, "label": question}
            resolution = resolve_question(item, profile, job)
            resolved_answer = field_answer(resolution, item)
            if resolution.status == "resolved" and not ambiguous:
                if searchable_lookup(candidate) and isinstance(resolved_answer, str):
                    lookup_questions.add(question)
                try:
                    _candidate_answer(candidate, resolved_answer)
                    if not (candidate.get("type") == "checkbox" and candidate.get("required") and resolved_answer is False):
                        continue
                except ValueError:
                    pass
            pending.append({**candidate, "question": question, "key": question,
                            "job_id": job["id"], "company": job.get("company", ""), "title": job.get("title", ""),
                            "intent_id": resolution.intent_id, "group_key": pending_group_key(question, resolution, job),
                            "scope": resolution.scope, "answer_source": resolution.source,
                            "resolution_status": resolution.status, "resolution_reason": resolution.reason,
                            "has_saved_answer": resolution.status in {"resolved", "conflict"}})
    pending = pending[:200]
    groups = {}
    for item in pending:
        key = item["group_key"]
        group = groups.setdefault(key, {"group_key": key, "intent_id": item["intent_id"], "question": item["question"],
                                        "scope": item["scope"], "questions": [], "job_count": 0})
        group["questions"].append(item)
    for group in groups.values():
        group["job_count"] = len({item["job_id"] for item in group["questions"]})
    saved = [{"question": question, "answer": answer, "sources": sources[question],
              **({"answer_kind": "lookup_query"} if question in lookup_questions else {})}
             for question, answer in sorted(answers.items())]
    for binding in (profile.get("application_profile") or {}).get("saved_answer_bindings", []):
        if not isinstance(binding, dict):
            continue
        saved.append({**binding, "sources": [source for source in sources.get(binding.get("question"), [])
                                             if binding.get("scope") == "global" or
                                             (binding.get("scope") == "application" and source["job_id"] == binding.get("job_id")) or
                                             (binding.get("scope") == "employer" and normalized(source.get("company")) == normalized(binding.get("company")))]})
    return {"pending": pending, "groups": list(groups.values()), "saved": saved}



def _canonical_value(intent_id, answer):
    """Parse a confirmed mapping, without interpreting an arbitrary choice."""
    boolean = intent_id.startswith("eligibility.") or intent_id.startswith("location.") or intent_id in {
        "availability.immediate", "availability.full_time_after_internship", "availability.offer_deadline",
        "accessibility.accommodation", "history.government_employment", "history.employment_restrictions", "history.nda",
        "employer.current_employer_contact"}
    if boolean:
        if type(answer) is bool:
            return answer
        if normalized(answer) in {"yes", "true"}:
            return True
        if normalized(answer) in {"no", "false"}:
            return False
        raise ValueError("A reusable yes/no fact needs an explicit Yes or No answer. Save this choice for this application instead.")
    if intent_id in {"experience.months", "experience.completed_internships", "travel.domestic_percent", "travel.international_percent"}:
        if isinstance(answer, str) and answer.isdigit():
            return int(answer)
        raise ValueError("This reusable fact needs a whole number.")
    if intent_id == "compensation.target_base":
        if isinstance(answer, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", answer.replace(",", "")):
            return float(answer.replace(",", ""))
        raise ValueError("A salary fact needs a numeric amount; set its currency in onboarding.")
    if not isinstance(answer, str):
        raise ValueError("This reusable fact needs text.")
    return answer


def register_answer_memory_routes(app, store, invalidate, continue_after_answer=None, on_profile_changed=None):
    """Merge explicit answer confirmations under SQLite's write lock."""

    def edit_answer(body, answer=None, *, forget=False, job=None, expected_profile=None):
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM config WHERE key='profile'").fetchone()
            profile = json.loads(row[0]) if row else Profile().model_dump()
            if expected_profile is not None and profile != expected_profile:
                raise HTTPException(409, "Your profile changed during confirmation. Refresh and review it before continuing.")
            answers = dict(profile.get("answers") or {})
            application = dict(profile.get("application_profile") or {})
            bindings = list(application.get("saved_answer_bindings") or [])
            classification = classify_question(body.question, job)
            explicit_scope = getattr(body, "scope", None)
            scope = explicit_scope or (classification.scope if classification.scope in {"employer", "application"} and job else "global")
            if forget:
                if explicit_scope is None or explicit_scope == "global":
                    existed = body.question in answers
                    answers.pop(body.question, None)
                else:
                    existed = False
                remaining = []
                for binding in bindings:
                    matches = (binding.get("question") == body.question and explicit_scope is not None and binding.get("scope") == explicit_scope)
                    if explicit_scope == "application":
                        matches = matches and binding.get("job_id") == body.job_id
                    elif explicit_scope == "employer":
                        matches = matches and normalized(binding.get("company")) == normalized(body.company)
                    if matches:
                        existed = True
                    else:
                        remaining.append(binding)
                if not existed:
                    raise HTTPException(404, "Saved answer not found in this scope.")
                bindings = remaining
            else:
                if classification.scope == "employer" and scope == "global":
                    raise HTTPException(422, "This answer depends on the employer. Choose employer or application scope.")
                if classification.status == "review" and scope != "application":
                    raise HTTPException(422, "This wording requires an exact application-scoped answer; it cannot become a global fact.")
                if scope in {"employer", "application"} and not job:
                    raise HTTPException(422, "A scoped answer must identify its recorded application.")
                intent = body.intent_id if body.confirm_intent else None
                if body.confirm_intent and (not intent or intent != classification.intent_id or classification.status != "recognized"):
                    raise HTTPException(422, "Confirm the recognized meaning shown for this current question, or save an exact scoped answer.")
                if intent and scope == "global" and intent in {*CORE_INTENTS, *FACT_INTENTS}:
                    try:
                        value = _canonical_value(intent, answer)
                    except ValueError as error:
                        raise HTTPException(422, str(error)) from error
                    if intent in {"education.graduation_year", "education.graduation_month"}:
                        # A year or month name alone cannot manufacture a YYYY-MM.
                        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value):
                            intent = None
                    if intent in CORE_INTENTS:
                        key = CORE_INTENTS[intent]
                        if profile.get(key) == value and type(profile.get(key)) is type(value):
                            return False, profile
                        profile[key] = value
                    elif intent in FACT_INTENTS:
                        facts = dict(application.get("facts") or {})
                        key = FACT_INTENTS[intent]
                        if facts.get(key) == value and type(facts.get(key)) is type(value):
                            return False, profile
                        facts[key] = value
                        application["facts"] = facts
                    if intent:
                        confirmed = dict(application.get("confirmed_at") or {})
                        confirmed[intent] = now()
                        application["confirmed_at"] = confirmed
                if not (intent and scope == "global" and intent in {*CORE_INTENTS, *FACT_INTENTS}):
                    if explicit_scope is None and scope == "global" and not body.confirm_intent:
                        if body.question in answers and type(answers[body.question]) is type(answer) and answers[body.question] == answer:
                            return False, profile
                        answers[body.question] = answer
                    else:
                        source_fingerprint = canonical_source_fingerprint(classification.intent_id, profile) if scope in {"employer", "application"} else None
                        known_boolean = canonical_boolean(classification.intent_id, profile)
                        if source_fingerprint and known_boolean is not None and explicit_boolean(answer) not in {None, known_boolean}:
                            raise HTTPException(422, "This choice contradicts your saved yes/no fact. Correct the profile fact explicitly before reusing the opposite answer.")
                        binding = SavedAnswerBinding(question=body.question, answer=answer, scope=scope,
                                                     company=str(job.get("company")) if scope == "employer" else None,
                                                     job_id=job["id"] if scope == "application" else None,
                                                     intent_id=intent, confirmed_at=now(), source_fingerprint=source_fingerprint).model_dump()
                        identity = lambda value: (value.get("question"), value.get("scope"), value.get("company"), value.get("job_id"), value.get("intent_id"))
                        previous = next((value for value in bindings if identity(value) == identity(binding)), None)
                        if (previous and type(previous.get("answer")) is type(answer) and previous.get("answer") == answer
                                and previous.get("source_fingerprint") == binding.get("source_fingerprint")):
                            return False, profile
                        bindings = [value for value in bindings if identity(value) != identity(binding)] + [binding]
            application["saved_answer_bindings"] = bindings
            try:
                updated = Profile.model_validate({**profile, "answers": answers, "application_profile": application}).model_dump()
            except ValidationError as error:
                raise HTTPException(422, "The answer conflicts with the profile format or answer-library limits. Review the profile and retry.") from error
            connection.execute("INSERT OR REPLACE INTO config VALUES ('profile',?)", (json.dumps(updated),))
        return True, updated

    @app.post("/api/answers")
    def save_answer(body: SaveAnswerRequest):
        answer = body.answer
        lookup = False
        job = None
        if body.choice_label is not None and body.job_id is None:
            raise HTTPException(409, "A choice confirmation must identify its application. Refresh its choices before saving.")
        if body.job_id is not None:
            job = store.job(body.job_id)
            if job is None:
                raise HTTPException(404, "Application not found.")
            candidates = _recorded_candidates(job, body.question)
            if len(candidates) != 1:
                raise HTTPException(409, "This exact question was not recorded for that application. Refresh its questions before saving.")
            if body.choice_label is not None:
                candidate = candidates[0]
                match = _option_match(candidate.get("options") or [], answer)
                if (candidate.get("type") not in {"radio", "select"} or candidate.get("multiple")
                        or match is None or str(match.get("label", "")) != body.choice_label):
                    raise HTTPException(409, "The available choice changed. Refresh its choices and confirm the current answer again.")
            try:
                answer = _candidate_answer(candidates[0], answer)
                lookup = searchable_lookup(candidates[0])
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        if body.confirm_intent and lookup:
            raise HTTPException(422, "A provisional dropdown search is not a confirmed reusable fact. Select a current choice first.")
        token = None
        if body.approve_and_continue:
            if not body.job_id or not body.expected_fingerprint:
                raise HTTPException(422, "Confirm & continue needs an application and the reviewed settings fingerprint.")
            if continue_after_answer is None:
                raise HTTPException(409, "Confirm & continue is unavailable. Save the answer and review the queue.")
            token = continue_after_answer(action="before", body=body, before_token=None, changed=False, updated_profile=None)
        changed, updated = edit_answer(body, answer, job=job, expected_profile=token.get("profile") if token else None)
        mode = {"answer_kind": "lookup_query", "choice_validation_required": True} if lookup else {}
        if changed:
            invalidate()
            if on_profile_changed is not None:
                on_profile_changed()
            store.event("answer", "A confirmed answer was saved locally. Review and approve the updated profile before running applications.")
        continuation = {}
        if body.approve_and_continue:
            continuation = continue_after_answer(action="after", body=body, before_token=token, changed=changed, updated_profile=updated) or {}
        return {"ok": True, "changed": changed, "question": body.question, "answer": answer,
                **({"approval_required": True} if changed else {}), **mode, **continuation}

    @app.delete("/api/answers")
    def forget_answer(body: ForgetAnswerRequest):
        edit_answer(body, forget=True)
        invalidate()
        store.event("answer", "A reusable answer was removed. Previous submission approval was revoked.")
        return {"ok": True, "approval_required": True}
