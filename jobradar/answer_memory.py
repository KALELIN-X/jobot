"""Explicit, local question/answer memory for the guarded application worker.

The app learns an answer only when its owner saves it. Unknown form fields
provide review candidates; neither prefilled site values nor guesses become
profile facts. Answers are reused for the same exact visible question.
"""

from __future__ import annotations

import json

from fastapi import HTTPException
from pydantic import Field, StrictBool, StrictStr, ValidationError, field_validator

from .autofill import _label, _option_match, reusable_question
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
    if kind == "checkbox":
        if type(answer) is not bool:
            raise ValueError("This question needs an explicit true or false answer.")
        return answer
    if kind in {"radio", "select"}:
        options = candidate.get("options") or []
        match = _option_match(options, answer)
        if match is None:
            raise ValueError("Choose one exact available answer for this question.")
        # Visible labels usually survive across forms better than opaque values.
        label = str(match["label"])
        return label if sum(option.get("label") == label for option in options) == 1 else str(match["value"])
    if not isinstance(answer, str):
        raise ValueError("This question needs a written answer, not a boolean.")
    return answer


def answer_memory_state(profile, jobs):
    answers = profile.get("answers") or {}
    pending = []
    for job in jobs:
        if job.get("demo"):
            continue
        seen = set()
        for candidate in job.get("pending_questions") or []:
            if not isinstance(candidate, dict):
                continue
            question = _label(candidate.get("question"))
            if question in seen or not reusable_question(question):
                continue
            seen.add(question)
            if question in answers:
                try:
                    _candidate_answer(candidate, answers[question])
                    if not (candidate.get("type") == "checkbox" and candidate.get("required") and answers[question] is False):
                        continue
                except ValueError:
                    pass
            pending.append({**candidate, "question": question, "key": question,
                            "job_id": job["id"], "company": job.get("company", ""), "title": job.get("title", "")})
    return {"pending": pending[:200], "saved": [{"question": question, "answer": answer} for question, answer in sorted(answers.items())]}


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
        if body.job_id is not None:
            job = store.job(body.job_id)
            if job is None:
                raise HTTPException(404, "Application not found.")
            candidates = [candidate for candidate in job.get("pending_questions") or []
                          if isinstance(candidate, dict) and _label(candidate.get("question")) == body.question]
            if len(candidates) != 1:
                raise HTTPException(409, "This exact question was not recorded for that application. Refresh its questions before saving.")
            try:
                answer = _candidate_answer(candidates[0], answer)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        if not edit_answer(body.question, answer):
            return {"ok": True, "changed": False, "question": body.question, "answer": answer}
        invalidate()
        # Do not put personal answers or employer-controlled questions into logs.
        store.event("answer", "A reusable answer was saved locally. Review and approve the updated profile before running applications.")
        return {"ok": True, "changed": True, "question": body.question, "answer": answer, "approval_required": True}

    @app.delete("/api/answers")
    def forget_answer(body: AnswerQuestion):
        edit_answer(body.question, forget=True)
        invalidate()
        store.event("answer", "A reusable answer was removed. Previous submission approval was revoked.")
        return {"ok": True, "approval_required": True}
