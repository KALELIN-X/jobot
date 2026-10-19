"""Explicit, local question/answer memory for the guarded application worker.

The app learns an answer only when its owner saves it. Unknown form fields
provide review candidates; neither prefilled site values nor guesses become
profile facts. Answers are reused for the same exact visible question.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import HTTPException
from pydantic import Field, StrictBool, StrictStr, StringConstraints, ValidationError, field_validator

from .autofill import _label, _option_match, lookup_query_hash, reusable_question, searchable_lookup
from .models import Profile, StrictModel


class AnswerQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value):
        value = _label(value)
        if not value or "\x00" in value:
            raise ValueError("Enter the exact visible question.")
        return value


class SaveAnswerRequest(AnswerQuestion):
    answer: StrictStr | StrictBool
    job_id: str | None = Field(default=None, min_length=1, max_length=200)
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
                                ("type", "options", "searchable", "lookup_required", "minimum_query_length", "multiple", "required")}, sort_keys=True)
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
    sources = {question: [] for question in answers}
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
            if source["lookup_required"] and isinstance(answers[question], str):
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
            if question in answers and not ambiguous:
                if searchable_lookup(candidate) and isinstance(answers[question], str):
                    lookup_questions.add(question)
                try:
                    _candidate_answer(candidate, answers[question])
                    if not (candidate.get("type") == "checkbox" and candidate.get("required") and answers[question] is False):
                        continue
                except ValueError:
                    pass
            pending.append({**candidate, "question": question, "key": question,
                            "job_id": job["id"], "company": job.get("company", ""), "title": job.get("title", "")})
    return {"pending": pending[:200], "saved": [
        {"question": question, "answer": answer, "sources": sources[question], **({"answer_kind": "lookup_query"} if question in lookup_questions else {})}
        for question, answer in sorted(answers.items())]}


def register_answer_memory_routes(app, store, invalidate):
    """Register narrow routes while keeping worker coordination in app.py."""

    def edit_answer(question, answer=None, *, forget=False):
        # Separate Save buttons may be used in quick succession. Merge under
        # SQLite's write lock so one learned answer cannot overwrite another.
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM config WHERE key='profile'").fetchone()
            profile = json.loads(row[0]) if row else Profile().model_dump()
            answers = dict(profile.get("answers") or {})
            if forget:
                if question not in answers:
                    raise HTTPException(404, "Saved answer not found.")
                del answers[question]
            else:
                if question in answers and type(answers[question]) is type(answer) and answers[question] == answer:
                    return False
                answers[question] = answer
            try:
                updated = Profile.model_validate({**profile, "answers": answers}).model_dump()
            except ValidationError:
                raise HTTPException(422, "The answer library supports at most 60 question/answer pairs. Remove an unused answer first.") from None
            connection.execute("INSERT OR REPLACE INTO config VALUES ('profile',?)", (json.dumps(updated),))
        return True

    @app.post("/api/answers")
    def save_answer(body: SaveAnswerRequest):
        answer = body.answer
        lookup = False
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
        mode = {"answer_kind": "lookup_query", "choice_validation_required": True} if lookup else {}
        if not edit_answer(body.question, answer):
            return {"ok": True, "changed": False, "question": body.question, "answer": answer, **mode}
        invalidate()
        # Do not put personal answers or employer-controlled questions into logs.
        store.event("answer", "A reusable answer was saved locally. Review and approve the updated profile before running applications.")
        return {"ok": True, "changed": True, "question": body.question, "answer": answer, "approval_required": True, **mode}

    @app.delete("/api/answers")
    def forget_answer(body: AnswerQuestion):
        edit_answer(body.question, forget=True)
        invalidate()
        store.event("answer", "A reusable answer was removed. Previous submission approval was revoked.")
        return {"ok": True, "approval_required": True}
