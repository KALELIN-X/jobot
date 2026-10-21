"""Typed, local candidate facts and the first-run onboarding question catalog.

These are facts a person explicitly confirms, not defaults inferred from a CV or
an employer's form. Missing values stay unknown. Existing top-level Profile
identity and authorization fields remain authoritative; this module does not
duplicate or migrate their values.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator, model_validator


ONBOARDING_STEPS = ("identity", "work_authorization", "education", "experience", "preferences", "review")


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


def _month(value: str | None) -> str | None:
    if value is not None:
        if not re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value):
            raise ValueError("Use a known month in YYYY-MM format; do not invent a day.")
        date.fromisoformat(value + "-01")
    return value


def _date(value: str | None) -> str | None:
    if value is not None:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("Use a known date in YYYY-MM-DD format.")
        date.fromisoformat(value)
    return value


def _timestamp(value: str | None) -> str | None:
    if value is not None:
        if len(value) > 50:
            raise ValueError("Confirmation timestamp is too long.")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Confirmation timestamps must include a timezone.")
    return value


class ApplicationFacts(CandidateModel):
    preferred_full_name: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    pronouns: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    postal_code: StrictStr | None = Field(default=None, min_length=1, max_length=30)
    current_country: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    current_city: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    current_state: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    us_citizen: StrictBool | None = None
    sponsorship_now: StrictBool | None = None
    sponsorship_future: StrictBool | None = None
    age_18_or_older: StrictBool | None = None
    willing_to_relocate: StrictBool | None = None
    needs_relocation_assistance: StrictBool | None = None
    relocate_at_own_expense: StrictBool | None = None
    willing_onsite: StrictBool | None = None
    willing_hybrid: StrictBool | None = None
    domestic_travel_percent: int | None = Field(default=None, ge=0, le=100)
    international_travel_percent: int | None = Field(default=None, ge=0, le=100)
    available_immediately: StrictBool | None = None
    earliest_start_month: StrictStr | None = None
    full_time_after_internship: StrictBool | None = None
    salary_mode: Literal["posted_range", "target", "ask"] | None = None
    target_base_salary: float | None = Field(default=None, gt=0, le=50_000_000)
    salary_currency: StrictStr | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    completed_internships: int | None = Field(default=None, ge=0, le=100)
    needs_accommodation: StrictBool | None = None
    government_employment: StrictBool | None = None
    has_employment_restrictions: StrictBool | None = None
    has_nda: StrictBool | None = None
    has_offer_deadline: StrictBool | None = None
    offer_deadline: StrictStr | None = None
    may_contact_current_employer: StrictBool | None = None

    _validate_offer_date = field_validator("offer_deadline")(_date)
    _validate_start_month = field_validator("earliest_start_month")(_month)

    @model_validator(mode="after")
    def consistent_preferences(self):
        if self.salary_mode == "target" and (self.target_base_salary is None or self.salary_currency is None):
            raise ValueError("A salary target needs an amount and a currency.")
        if self.offer_deadline is not None and self.has_offer_deadline is False:
            raise ValueError("A deadline date conflicts with the answer that no deadline exists.")
        return self


APPLICATION_FACT_KEYS = frozenset(ApplicationFacts.model_fields)


class EducationRecord(CandidateModel):
    id: StrictStr = Field(min_length=1, max_length=100)
    institution: StrictStr = Field(min_length=1, max_length=240)
    degree: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    discipline: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["completed", "current", "incoming", "withdrawn"]
    primary_for_applications: StrictBool = False
    start_month: StrictStr | None = None
    end_month: StrictStr | None = None
    gpa: float | None = Field(default=None, ge=0, le=100)
    gpa_scale: float | None = Field(default=None, gt=0, le=100)

    _validate_months = field_validator("start_month", "end_month")(_month)

    @model_validator(mode="after")
    def valid_academic_record(self):
        if self.start_month and self.end_month and self.start_month > self.end_month:
            raise ValueError("An education end month cannot precede its start month.")
        if (self.gpa is None) != (self.gpa_scale is None):
            raise ValueError("Provide both the earned GPA and its scale, or leave both unknown.")
        if self.status not in {"current", "completed"} and self.gpa is not None:
            raise ValueError("Enter an earned GPA only for a current or completed program.")
        if self.gpa is not None and self.gpa > self.gpa_scale:
            raise ValueError("Earned GPA cannot exceed its scale.")
        return self


class EmploymentRecord(CandidateModel):
    id: StrictStr = Field(min_length=1, max_length=100)
    company: StrictStr = Field(min_length=1, max_length=240)
    title: StrictStr = Field(min_length=1, max_length=240)
    kind: Literal["employee", "internship", "contract"]
    start_month: StrictStr
    end_month: StrictStr | None = None
    current: StrictBool
    primary_for_applications: StrictBool = False

    _validate_months = field_validator("start_month", "end_month")(_month)

    @model_validator(mode="after")
    def valid_employment_record(self):
        if self.current and self.end_month is not None:
            raise ValueError("Current employment cannot have an end month.")
        if not self.current and self.end_month is None:
            raise ValueError("Past employment needs its actual end month.")
        if self.end_month and self.start_month > self.end_month:
            raise ValueError("An employment end month cannot precede its start month.")
        return self


class AvailabilityPeriod(CandidateModel):
    id: StrictStr = Field(min_length=1, max_length=100)
    start_date: StrictStr
    end_date: StrictStr
    hours_per_week: int | None = Field(default=None, ge=1, le=100)
    weeks: int | None = Field(default=None, ge=1, le=104)

    _validate_dates = field_validator("start_date", "end_date")(_date)

    @model_validator(mode="after")
    def bounded_period(self):
        start, end = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        duration = (end - start).days + 1
        if duration <= 0:
            raise ValueError("An availability end date cannot precede its start date.")
        if duration > 732:
            raise ValueError("Confirm availability for a bounded period of at most two years.")
        if self.weeks is not None and self.weeks * 7 > duration:
            raise ValueError("The stated number of weeks exceeds this availability period.")
        return self


class SavedAnswerBinding(CandidateModel):
    question: StrictStr = Field(min_length=1, max_length=500)
    answer: StrictStr | StrictBool
    scope: Literal["global", "employer", "application"]
    company: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    job_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)
    intent_id: StrictStr | None = Field(default=None, min_length=1, max_length=160)
    source_fingerprint: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confirmed_at: StrictStr | None = None

    _validate_confirmation = field_validator("confirmed_at")(_timestamp)

    @field_validator("question")
    @classmethod
    def reusable_fact_only(cls, value):
        # Import at validation time to keep the profile schema independent of
        # browser adapters while sharing the resolver's protected boundary.
        from .question_intents import protected_question
        if protected_question(value):
            raise ValueError("Agreements, read acknowledgments, credentials and verification challenges are not reusable answers.")
        return value

    @field_validator("question", "answer")
    @classmethod
    def ordinary_bounded_text(cls, value):
        if isinstance(value, str) and (not value.strip() or len(value) > 4000 or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
            raise ValueError("Use nonempty answer text of at most 4,000 characters without control characters.")
        return value

    @model_validator(mode="after")
    def scoped_context(self):
        if self.scope == "employer" and not self.company:
            raise ValueError("An employer-scoped answer needs its employer.")
        if self.scope == "application" and not self.job_id:
            raise ValueError("An application-scoped answer needs its job ID.")
        if self.scope == "global" and (self.company is not None or self.job_id is not None):
            raise ValueError("A global answer cannot contain employer or application scope.")
        if self.source_fingerprint is not None and self.scope == "global":
            raise ValueError("A live choice tied to a profile fact needs employer or application scope.")
        if self.scope == "employer" and self.job_id is not None:
            raise ValueError("Use application scope for an answer tied to one job.")
        return self


class ApplicationProfile(CandidateModel):
    version: Literal[1] = 1
    facts: ApplicationFacts = Field(default_factory=ApplicationFacts)
    education: list[EducationRecord] = Field(default_factory=list, max_length=20)
    employment: list[EmploymentRecord] = Field(default_factory=list, max_length=40)
    availability: list[AvailabilityPeriod] = Field(default_factory=list, max_length=30)
    completed_steps: list[Literal["identity", "work_authorization", "education", "experience", "preferences", "review"]] = Field(default_factory=list, max_length=6)
    confirmed_at: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=500)
    saved_answer_bindings: list[SavedAnswerBinding] = Field(default_factory=list, max_length=400)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Only application profile version 1 is supported.")
        return value

    @field_validator("confirmed_at")
    @classmethod
    def valid_confirmations(cls, value):
        for key, stamp in value.items():
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", key):
                raise ValueError("Confirmation keys must be bounded field or record identifiers.")
            _timestamp(stamp)
        return value

    @model_validator(mode="after")
    def unique_records(self):
        for name in ("education", "employment", "availability"):
            identifiers = [record.id for record in getattr(self, name)]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{name.capitalize()} record IDs must be unique.")
        for name in ("education", "employment"):
            if sum(record.primary_for_applications for record in getattr(self, name)) > 1:
                raise ValueError(f"Choose at most one primary {name} record for generic application fields.")
        if len(self.completed_steps) != len(set(self.completed_steps)):
            raise ValueError("Onboarding steps cannot be repeated.")
        return self


def _field(key, label, kind="text", *, storage="facts", options=(), description="", examples=(),
           required=False, minimum=None, maximum=None, step=None):
    result = {"key": key, "label": label, "type": kind, "options": list(options),
              "description": description, "examples": list(examples), "storage": storage,
              "required": required}
    for name, value in (("min", minimum), ("max", maximum), ("step", step)):
        if value is not None:
            result[name] = value
    if key == "primary_for_applications" and storage in {"education", "employment"}:
        # This flag chooses a rendering preference, not a personal fact. New
        # records make no primary selection unless the person explicitly does.
        result["default"] = False
    return result


def _options(*values):
    return [{"value": value, "label": label} for value, label in values]


def _record(key, title, fields, description):
    return {"key": key, "title": title, "description": description, "fields": fields}


def onboarding_catalog() -> list[dict]:
    """Return fresh, presentation-ready metadata without candidate values.

    Boolean controls must offer unknown as a third state. Examples are question
    wordings, never seeded answers. Records preserve month/date precision.
    """
    return [
        {"id": "identity", "title": "Your identity and contact details",
         "description": "Confirm the details employers receive. Legal and preferred names are separate.",
         "fields": [
             _field("name", "Legal full name", storage="profile"),
             _field("first_name", "Legal first name", storage="profile"),
             _field("last_name", "Legal last name", storage="profile"),
             _field("preferred_first_name", "Preferred first name", storage="profile"),
             _field("preferred_full_name", "Preferred full name"),
             _field("pronouns", "Pronouns (optional)"),
             _field("email", "Application email", "email", storage="profile"),
             _field("phone", "Phone including country code", "tel", storage="profile"),
             _field("location", "Current city, state and country", storage="profile", description="Where you live now, separate from places you would move to."),
             _field("current_country", "Current country (for separate address fields)"),
             _field("current_city", "Current city (for separate address fields)", description="Only the city name. Jobot does not split or guess it from your full location."),
             _field("current_state", "Current state or region (for separate address fields)"),
             _field("postal_code", "Current postal code"),
             _field("linkedin", "LinkedIn profile", "url", storage="profile"),
             _field("github", "GitHub profile", "url", storage="profile"),
             _field("portfolio", "Portfolio or personal website", "url", storage="profile"),
             _field("age_18_or_older", "Are you 18 or older?", "boolean"),
         ], "records": []},
        {"id": "work_authorization", "title": "Work authorization",
         "description": "These answers cover US applications. Leave anything you have not confirmed unknown.",
         "fields": [
             _field("work_authorized", "Are you currently authorized to work in the United States?", "boolean", storage="profile", examples=["Are you legally eligible to work in the US?"]),
             _field("needs_sponsorship", "Will you need employer sponsorship now or in the future?", "boolean", storage="profile", description="The existing overall answer remains available; the answers below distinguish timing."),
             _field("sponsorship_now", "Do you need employer sponsorship now?", "boolean"),
             _field("sponsorship_future", "Will you need employer sponsorship in the future?", "boolean"),
             _field("us_citizen", "Are you a US citizen? (optional)", "boolean", description="Citizenship is a separate fact and does not establish security-clearance eligibility."),
         ], "records": []},
        {"id": "education", "title": "Education and graduation",
         "description": "Add each program separately. Incoming enrollment is different from current enrollment, and GPA belongs to the program where it was earned.",
         "fields": [
             _field("graduation_month", "Graduation month used for job matching", "month", storage="profile", description="Use YYYY-MM. Choose the actual or expected program relevant to your search; this does not establish current enrollment."),
         ], "records": [_record("education", "Education programs", [
             _field("institution", "Institution", storage="education", required=True),
             _field("degree", "Degree", storage="education"),
             _field("discipline", "Major or discipline", storage="education"),
             _field("status", "Enrollment status", "select", storage="education", required=True, options=_options(("completed", "Completed"), ("current", "Currently enrolled"), ("incoming", "Incoming, not yet enrolled"), ("withdrawn", "Withdrawn, not completed"))),
             _field("primary_for_applications", "Use this program for generic application fields", "boolean", storage="education", description="Your explicit preference when a form simply asks for a school or degree. A question asking for your current school still requires current enrollment; choosing an incoming program does not make it current."),
             _field("start_month", "Start month", "month", storage="education"),
             _field("end_month", "Completion or expected graduation month", "month", storage="education"),
             _field("gpa", "Earned GPA (optional)", "number", storage="education", minimum=0, maximum=100, step="any", description="Leave blank for incoming programs or when no GPA has been earned."),
             _field("gpa_scale", "GPA scale", "number", storage="education", minimum=0.01, maximum=100, step="any"),
         ], "Keep dates at month precision. A withdrawn course or program is not a completed qualification.")]},
        {"id": "experience", "title": "Work history and experience",
         "description": "Record actual work dates and distinguish employment, internships and contracts. Coursework and personal projects do not become professional experience.",
         "fields": [
             _field("current_company", "Current employer(s)", storage="profile"),
             _field("experience_months", "Completed months of relevant professional experience", "number", storage="profile", minimum=0, maximum=720, step=1, description="Do not double-count overlapping jobs or round an incomplete year up."),
             _field("skills", "Confirmed skills", "list", storage="profile", description="Skills alone do not establish years of professional use."),
             _field("completed_internships", "Completed technical internships or co-ops", "number", minimum=0, maximum=100, step=1),
             _field("government_employment", "Have you worked for a government employer?", "boolean", description="Questions about family members, a specific lookback period or other affiliations still need their own facts."),
             _field("has_employment_restrictions", "Do agreements restrict accepting another job or performing its duties?", "boolean"),
             _field("has_nda", "Have you signed a nondisclosure agreement?", "boolean", description="An NDA is different from a noncompete or other employment restriction."),
             _field("may_contact_current_employer", "May a prospective employer contact your current employer?", "boolean", description="A reference-contact preference does not accept a release or authorize a new legal agreement."),
         ], "records": [_record("employment", "Employment and internships", [
             _field("company", "Employer", storage="employment", required=True),
             _field("title", "Actual role or title", storage="employment", required=True),
             _field("kind", "Work type", "select", storage="employment", required=True, options=_options(("employee", "Employment"), ("internship", "Internship or co-op"), ("contract", "Contract work"))),
             _field("start_month", "Start month", "month", storage="employment", required=True),
             _field("current", "Still working here?", "boolean", storage="employment", required=True),
             _field("primary_for_applications", "Use this role for generic application fields", "boolean", storage="employment", description="Choose at most one role when a form has no room for your full history. Questions specifically asking about current employment still require an ongoing role."),
             _field("end_month", "End month (past work only)", "month", storage="employment"),
         ], "Past work needs known start and end months. Current work has no invented end date.")]},
        {"id": "preferences", "title": "Location, pay and availability",
         "description": "Set your preferences once. A job's location, dates and requirements still have to fit them.",
         "fields": [
             _field("approved_work_locations", "Approved work locations", "list", storage="profile", description="Countries, cities or regions where you actually authorize applications."),
             _field("willing_to_relocate", "Willing to relocate?", "boolean"),
             _field("needs_relocation_assistance", "Need employer-paid relocation assistance?", "boolean"),
             _field("relocate_at_own_expense", "Willing to relocate at your own expense?", "boolean"),
             _field("willing_onsite", "Willing to work onsite?", "boolean"),
             _field("willing_hybrid", "Willing to work a hybrid schedule?", "boolean"),
             _field("domestic_travel_percent", "Maximum domestic travel percentage", "number", minimum=0, maximum=100, step=1),
             _field("international_travel_percent", "Maximum international travel percentage", "number", minimum=0, maximum=100, step=1),
             _field("available_immediately", "Available to start a full-time job immediately?", "boolean"),
             _field("earliest_start_month", "Earliest full-time start month, if known", "month", description="A known month does not authorize inventing an exact start day."),
             _field("full_time_after_internship", "Could you accept a full-time job after an internship?", "boolean"),
             _field("salary_mode", "Salary-answer preference", "select", options=_options(("posted_range", "Use the posted range when available"), ("target", "Use my confirmed target"), ("ask", "Ask me for each role"))),
             _field("target_base_salary", "Annual base salary target", "number", minimum=0.01, maximum=50_000_000, step="any", description="A target is not automatically a minimum acceptable salary."),
             _field("salary_currency", "Salary currency (three-letter code)"),
             _field("needs_accommodation", "Do you need an application or workplace accommodation? (optional)", "boolean"),
             _field("has_offer_deadline", "Do you have an active offer or recruiting deadline?", "boolean"),
             _field("offer_deadline", "Active deadline date, if known", "date"),
         ], "records": [_record("availability", "Confirmed internship availability", [
             _field("start_date", "Available from", "date", storage="availability", required=True),
             _field("end_date", "Available through", "date", storage="availability", required=True),
             _field("hours_per_week", "Hours available per week", "number", storage="availability", minimum=1, maximum=100, step=1),
             _field("weeks", "Confirmed number of weeks", "number", storage="availability", minimum=1, maximum=104, step=1),
         ], "Confirm actual date ranges. One summer's availability is not permission for every future summer.")]},
        {"id": "review", "title": "Review what Jobot can reuse",
         "description": "Review confirmed facts and unknowns before approval. Employer history stays scoped to that employer. Agreements, signatures, verification and unfamiliar claims require their own handling; a saved answer is not permission to invent a fact.",
         "fields": [], "records": []},
    ]
