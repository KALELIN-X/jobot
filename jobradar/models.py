from __future__ import annotations

import re
from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApprovalRequest(StrictModel):
    acknowledge: StrictBool = False
    expected_fingerprint: str = Field(default="", max_length=64)


class AutomationRequest(StrictModel):
    enabled: StrictBool


class JobStatusRequest(StrictModel):
    status: Literal["new", "queued", "dismissed"]


class QuestionLookupRequest(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    query: str = Field(default="", max_length=240)

    @field_validator("question", "query")
    @classmethod
    def check_lookup_text(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("Use ordinary question/search text without control characters.")
        return value.strip()


class ExternalApplicationRequest(StrictModel):
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    job_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def check_application_url(cls, value):
        from .store import application_key
        if value is not None and application_key({"apply_url": value}) is None:
            raise ValueError("Enter an HTTPS Lever or Greenhouse posting URL with a valid posting ID.")
        return value

    @model_validator(mode="after")
    def require_listing_or_url(self):
        if self.url is None and self.job_id is None:
            raise ValueError("Provide a saved job ID or an HTTPS Lever or Greenhouse posting URL.")
        return self


class Profile(StrictModel):
    name: str = Field(default="", max_length=160)
    first_name: str = Field(default="", max_length=160)
    last_name: str = Field(default="", max_length=160)
    preferred_first_name: str = Field(default="", max_length=160)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    location: str = Field(default="", max_length=240)
    approved_work_locations: list[str] = Field(default_factory=list, max_length=30)
    linkedin: str = Field(default="", max_length=1000)
    github: str = Field(default="", max_length=1000)
    portfolio: str = Field(default="", max_length=1000)
    current_company: str = Field(default="", max_length=200)
    skills: list[str] = Field(default_factory=list, max_length=100)
    experience_months: int | None = Field(default=None, ge=0, le=720)
    graduation_date: str | None = None
    graduation_month: StrictStr | None = None
    work_authorized: bool | None = None
    needs_sponsorship: bool | None = None
    answers: dict[str, StrictStr | StrictBool] = Field(default_factory=dict, max_length=60)

    @field_validator("email")
    @classmethod
    def check_email(cls, value):
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address.")
        return value

    @field_validator("linkedin", "github", "portfolio")
    @classmethod
    def check_link(cls, value):
        from urllib.parse import urlsplit
        if value:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Profile links must be HTTPS URLs without credentials.")
        return value

    @field_validator("graduation_date")
    @classmethod
    def check_date(cls, value):
        if value:
            date.fromisoformat(value)
        return value or None

    @field_validator("skills")
    @classmethod
    def check_skills(cls, values):
        if any(not item.strip() or len(item) > 100 for item in values):
            raise ValueError("Skills must be nonempty and at most 100 characters each.")
        return list(dict.fromkeys(item.strip() for item in values))

    @field_validator("graduation_month")
    @classmethod
    def check_graduation_month(cls, value):
        if value is not None:
            if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value):
                raise ValueError("Graduation month must use YYYY-MM; provide only a month you know.")
            date.fromisoformat(value + "-01")
        return value

    @field_validator("approved_work_locations")
    @classmethod
    def check_work_locations(cls, values):
        if any(not item.strip() or len(item) > 200 for item in values):
            raise ValueError("Approved work locations must be nonempty and at most 200 characters each.")
        return list(dict.fromkeys(item.strip() for item in values))

    @field_validator("answers")
    @classmethod
    def check_answers(cls, values):
        if any(not key.strip() or len(key) > 500 or (isinstance(value, str) and (not value.strip() or len(value) > 4000)) for key, value in values.items()):
            raise ValueError("Saved answers need a question and answer (up to 4,000 characters).")
        return {key.strip(): value.strip() if isinstance(value, str) else value for key, value in values.items()}


class Rules(StrictModel):
    company_scope: Literal["any", "selected"] = "any"
    companies: list[str] = Field(default_factory=list, max_length=30)
    company_boards: dict[str, str] = Field(default_factory=dict, max_length=10)
    locations: list[str] = Field(default_factory=list, max_length=30)
    allowed_ats: list[str] = Field(default_factory=lambda: ["lever", "greenhouse"])
    min_score: int = Field(default=60, ge=0, le=100)
    max_required_experience_months: int | None = Field(default=None, strict=True, ge=0, le=120)
    prefer_new_grad: StrictBool = False
    daily_limit: int | None = Field(default=None, strict=True, ge=1, le=500)
    interval_minutes: int = Field(default=360, ge=30, le=1440)
    require_known_eligibility: bool = True
    enforce_graduation_window: StrictBool = True

    @model_validator(mode="before")
    @classmethod
    def preserve_legacy_company_scope(cls, values):
        if isinstance(values, dict) and "company_scope" not in values and values.get("companies"):
            return {**values, "company_scope": "selected"}
        return values

    @field_validator("companies", "locations")
    @classmethod
    def check_names(cls, values):
        if any(not item.strip() or len(item) > 200 for item in values):
            raise ValueError("Use nonempty company or location names, up to 200 characters.")
        return list(dict.fromkeys(item.strip() for item in values))

    @field_validator("company_boards")
    @classmethod
    def check_boards(cls, values):
        if any(not company.strip() or len(company) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", slug) for company, slug in values.items()):
            raise ValueError("Lever boards need an exact company name and a simple board slug.")
        return {company.strip(): slug for company, slug in values.items()}

    @field_validator("allowed_ats")
    @classmethod
    def check_ats(cls, values):
        if len(values) != len(set(values)) or any(value not in {"lever", "greenhouse"} for value in values):
            raise ValueError("Supported automatic application systems are Lever and Greenhouse; leave the list empty to disable them.")
        return values


def company_scope(rules: dict) -> str:
    """Honor explicit scope and retain an older nonempty employer restriction."""
    return rules.get("company_scope") or ("selected" if rules.get("companies") else "any")
