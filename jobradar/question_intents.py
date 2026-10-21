"""Deterministic question meanings and explicitly confirmed application facts.

This is a bounded intent catalog, not fuzzy text similarity. A matching word is
not enough to answer a compound, negative, foreign-country, or employer-specific
question. Observation never changes a profile. All functions are pure.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


def normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def question_label(item: dict | str) -> str:
    value = item if isinstance(item, str) else item.get("label", item.get("question", ""))
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"\s*(?:[*✱∗]|\(required\))\s*$", "", value, flags=re.I).strip()


@dataclass(frozen=True)
class AnswerResolution:
    intent_id: str | None = None
    status: str = "unknown"
    value: Any = None
    source: str | None = None
    scope: str = "global"
    reason: str = "No confirmed answer matches this question."

    def as_dict(self):
        return asdict(self)


# The path is also the explicit write target when a user confirms a mapping.
CORE_INTENTS = {
    "identity.full_name": "name", "identity.first_name": "first_name", "identity.last_name": "last_name",
    "identity.preferred_first_name": "preferred_first_name", "contact.email": "email", "contact.phone": "phone",
    "contact.location": "location", "links.linkedin": "linkedin", "links.github": "github", "links.portfolio": "portfolio",
    "employment.current_company": "current_company", "experience.months": "experience_months", "skills": "skills",
    "education.graduation_date": "graduation_date", "education.graduation_month": "graduation_month",
    "education.graduation_year": "graduation_month", "eligibility.work_authorized_us": "work_authorized",
    "eligibility.sponsorship_us": "needs_sponsorship",
}
FACT_INTENTS = {
    "identity.preferred_full_name": "preferred_full_name", "identity.pronouns": "pronouns",
    "eligibility.us_citizen": "us_citizen", "eligibility.sponsorship_now_us": "sponsorship_now",
    "eligibility.sponsorship_future_us": "sponsorship_future", "location.relocate": "willing_to_relocate",
    "location.relocation_assistance": "needs_relocation_assistance", "location.self_funded_relocation": "relocate_at_own_expense",
    "location.onsite": "willing_onsite", "location.hybrid": "willing_hybrid",
    "travel.domestic_percent": "domestic_travel_percent", "travel.international_percent": "international_travel_percent",
    "availability.immediate": "available_immediately", "availability.full_time_after_internship": "full_time_after_internship",
    "compensation.target_base": "target_base_salary", "experience.completed_internships": "completed_internships",
    "accessibility.accommodation": "needs_accommodation", "history.government_employment": "government_employment",
    "history.employment_restrictions": "has_employment_restrictions", "history.nda": "has_nda",
    "availability.offer_deadline": "has_offer_deadline", "availability.offer_deadline_date": "offer_deadline",
    "eligibility.adult": "age_18_or_older",
    "contact.postal_code": "postal_code", "contact.country": "current_country", "contact.state": "current_state",
    "contact.city": "current_city",
    "availability.earliest_month": "earliest_start_month", "employer.current_employer_contact": "may_contact_current_employer",
}
ALIASES = {
    "identity.full_name": {"name", "full name", "fullname", "your name", "legal name", "full legal name"},
    "identity.first_name": {"first name", "firstname", "given name", "givenname", "legal first name", "legal given name"},
    "identity.last_name": {"last name", "lastname", "family name", "familyname", "surname", "legal last name", "legal family name"},
    "identity.preferred_first_name": {"preferred first name", "preferredfirstname", "preferred given name"},
    "identity.preferred_full_name": {"preferred name", "preferred full name", "what name do you prefer to go by"},
    "identity.pronouns": {"pronouns", "preferred pronouns", "your pronouns", "what are your pronouns"},
    "contact.email": {"email", "email address", "emailaddress", "your email", "confirm email", "confirm your email", "confirm email address", "email confirmation", "re enter email", "re enter your email"},
    "contact.phone": {"phone", "phone number", "phonenumber", "telephone", "mobile phone", "mobile number"},
    "contact.location": {"location", "current location", "currentlocation", "location city", "city country", "location city country"},
    "employment.current_company": {"current company", "currentcompany", "current employer", "organization"},
    "links.linkedin": {"linkedin", "linkedin url", "linkedin profile", "linkedin profile url", "urls linkedin"},
    "links.github": {"github", "github url", "github profile", "github profile url", "urls github"},
    "links.portfolio": {"portfolio", "portfolio url", "personal website", "personal website url", "website", "urls portfolio"},
    "experience.months": {"experience months", "months of experience", "experience in months", "relevant experience in months"},
    "experience.completed_internships": {"number of completed internships", "how many internships have you completed", "completed internships", "number of completed internships co ops"},
    "skills": {"skills", "technical skills"},
    "education.graduation_date": {"graduation date", "graduationdate", "expected graduation date", "anticipated graduation date"},
    "education.graduation_month": {"graduation month", "expected graduation month", "graduation month year", "expected graduation month year"},
    "education.graduation_year": {"graduation year", "expected graduation year", "anticipated graduation year"},
    "compensation.target_base": {"desired salary", "desired annual salary", "salary expectation", "salary expectations", "expected annual base salary", "desired annual base salary", "desired pay"},
    "contact.postal_code": {"postal code", "zip code", "zip postal code", "zip"},
    "contact.country": {"country", "current country", "country of residence"},
    "contact.state": {"state", "current state", "state province", "state of residence"},
    "contact.city": {"city", "current city", "city of residence", "town city"},
    "availability.earliest_month": {"earliest start month", "earliest available start month"},
}
RECORD_INTENTS = {
    "education.institution": "institution", "education.degree": "degree", "education.discipline": "discipline",
    "education.gpa": "gpa", "education.gpa_scale": "gpa_scale", "education.record_end_month": "end_month",
    "employment.record_company": "company", "employment.record_title": "title",
    "employment.record_start_month": "start_month", "employment.record_end_month": "end_month",
}
_RECORD_ALIASES = {
    "education.institution": {"school", "school name", "college", "university", "university name", "educational institution", "current university", "current school", "undergraduate university", "undergraduate institution", "graduate university", "graduate institution"},
    "education.degree": {"degree", "degree type", "current degree", "completed degree", "highest degree completed", "undergraduate degree", "graduate degree"},
    "education.discipline": {"major", "discipline", "field of study", "undergraduate major", "graduate major"},
    "education.gpa": {"gpa", "cumulative gpa", "undergraduate gpa", "graduate gpa", "current gpa"},
    "education.gpa_scale": {"gpa scale", "undergraduate gpa scale", "graduate gpa scale"},
    "education.record_end_month": {"undergraduate graduation month", "graduate graduation month", "bachelors graduation month", "masters graduation month"},
    "employment.record_company": {"company", "company name", "employer", "internship company", "previous internship company", "employer name"},
    "employment.record_title": {"job title", "position title", "current job title", "current role title", "internship title", "previous internship title"},
    "employment.record_start_month": {"employment start month", "employment start date", "internship start month", "internship start date", "previous internship start month"},
    "employment.record_end_month": {"employment end month", "employment end date", "internship end month", "internship end date", "previous internship end month"},
}
_LEGAL = re.compile(r"\b(?:agree(?:ment)?|consent|certif\w*|attest\w*|signature|arbitration|terms|waiver|release of liability|i authorize)\b", re.I)
_PRIVATE = re.compile(r"\b(?:password|passcode|captcha|verification code|security code|one time code|otp|totp|2fa|mfa|api key|access token|auth token|authentication token|social security|ssn|bank account|routing number)\b", re.I)
_FOREIGN = re.compile(r"\b(?:canada|canadian|united kingdom|uk|england|ireland|germany|france|australia|india|singapore|china|japan|mexico|europe|eu|netherlands|israel|brazil|switzerland|country of employment|country where|country in which)\b")
_NEGATIVE = re.compile(r"\b(?:not|without|never|cannot|cant|unable|exempt|dont|doesnt)\b")
_EMPLOYER = re.compile(r"(?:\b(?:previously|ever|before|past)\b.{0,80}\b(?:worked|employed|interviewed|applied)\b|\b(?:worked|employed|interviewed|applied)\b.{0,80}\b(?:previously|before|past|ever)\b|\bwhy\b.{0,90}\b(?:company|join|work here|interested|role|position)\b|\b(?:family|relative|relatives|referral|referred|hear about|heard about)\b|\b(?:current employer|references?)\b.{0,40}\bcontact\b|\bcontact\b.{0,40}\b(?:current employer|references?)\b)")
_COUNTRY_CONTEXT = re.compile(r"\bcountry (?:(?:in which|where) (?:you are applying|this (?:job|position|role) is (?:located|based))|of employment)\b")
_ELIGIBILITY_WORDS = frozenset("are you your legally legal authorized authorised authorization authorisation eligible eligibility right work working employment valid do have the a an to for any employer company in united states of america us usa u s now currently today will would need needs require requires required requiring sponsorship sponsor visa and or future present please confirm can".split())
_EXTRA_CONSTRAINT = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|hours?|days?|weeks?|months?|years?|through|until|weekends?|overnight|nights?|shifts?|daily|rotating|overtime|unpaid|exclusive|exclusively)\b")


def _explicit_us_job(job):
    if not isinstance(job, dict):
        return False
    country = normalized(job.get("country"))
    location = normalized(job.get("location")) if isinstance(job.get("location"), str) else ""
    if _FOREIGN.search(location) or (country and country not in {"us", "u s", "usa", "united states", "united states of america"}):
        return False
    return country in {"us", "u s", "usa", "united states", "united states of america"} or bool(re.search(r"\b(?:us|usa|united states(?: of america)?)$", location))


def employer_dependent(question: str) -> bool:
    text = normalized(question)
    if text in ALIASES["identity.last_name"]:
        return False
    if "government" in text and not re.search(r"\b(?:family|relative|relatives|spouse|child)\b", text):
        return False
    return bool(_EMPLOYER.search(text) or
                re.search(r"\b(?:current|former|previous|currently|previously)\b.{0,35}\b(?:employee|contractor|employed)\b", text) or
                re.search(r"\b(?:employed by|employee of|contractor for|work for us|work here|work for this company)\b", text) or
                re.search(r"\b(?:interested|interest|excites|excited|attracted|motivates)\b.{0,70}\b(?:company|role|position|join|us|here)\b", text))


def _agreement_history_question(text: str) -> bool:
    return bool(re.match(r"^(?:have you|are you|do you|has your|are there)\b", text)
                and re.search(r"\b(?:signed|bound by|subject to|have|restrict)\b", text)
                and re.search(r"\b(?:nda|non disclosure|nondisclosure|non compete|noncompete|non solicitation|nonsolicitation|restrictive covenant|agreements)\b", text)
                and not re.search(r"\b(?:agree|accept|consent|authorize|certify|attest|submitting|sign here|signature)\b", text))


def _legal_history_intent(text: str) -> str | None:
    """Recognize signed history separately from a current or dated obligation."""
    nda = r"(?:nda|non disclosure agreement|nondisclosure agreement|nda (?:or )?non disclosure agreement)"
    if re.fullmatch(r"have you (?:ever )?signed (?:any |a |an )?" + nda, text):
        return "history.nda"
    if text in {
        "do agreements restrict accepting another job or performing its duties",
        "do you have any agreements that restrict accepting another job or performing its duties",
        "do you have any employment restrictions",
        "are you subject to any employment restrictions",
    }:
        return "history.employment_restrictions"
    return None


def protected_question(question: str) -> bool:
    text = normalized(question)
    if _agreement_history_question(text) or _legal_history_intent(text):
        return False
    return bool(_PRIVATE.search(text) or _LEGAL.search(text) or "privacy" in text or
                re.search(r"\b(?:acknowledge|acknowledgment|read|reviewed)\b", text))


def classify_question(item: dict | str, job: dict | None = None) -> AnswerResolution:
    """Identify only meanings whose qualifiers are covered by this catalog."""
    label = question_label(item)
    if not label and isinstance(item, dict):
        label = str(item.get("name") or "")
    text = normalized(label)
    if _COUNTRY_CONTEXT.search(text) and _explicit_us_job(job):
        return classify_question({"label": _COUNTRY_CONTEXT.sub("united states", text)})
    if not text or len(label) > 1000:
        return AnswerResolution()
    if _PRIVATE.search(text):
        return AnswerResolution(status="review", scope="application", reason="Verification or private credentials require direct review.")
    history_intent = _legal_history_intent(text)
    if history_intent and not _NEGATIVE.search(text):
        return AnswerResolution(history_intent, "recognized", reason="This asks about existing agreement history, not acceptance of a new agreement.")
    if _agreement_history_question(text):
        return AnswerResolution(status="review", scope="application", reason="This asks about a specific current, historical, or dated agreement obligation; the general saved fact is insufficient.")
    if _LEGAL.search(text):
        return AnswerResolution(status="review", scope="application", reason="This is a site-specific agreement or declaration.")
    if "privacy" in text or re.search(r"\b(?:acknowledge|read|reviewed)\b", text):
        return AnswerResolution(intent_id="review.privacy_acknowledgment", status="review", scope="application", reason="A privacy/read acknowledgment requires review of this site's notice; it is not a profile fact.")
    # A visible question wins over a contradictory machine field identifier.
    for intent, aliases in ALIASES.items():
        if text in aliases:
            return AnswerResolution(intent_id=intent, status="recognized", reason="Recognized profile question.")
    for intent, aliases in _RECORD_ALIASES.items():
        if text in aliases:
            return AnswerResolution(intent, "recognized", scope="application", reason="Uses a specifically identified education or employment record; ambiguous records need review.")
    if employer_dependent(label) and "government" not in text:
        intent = "employer.current_employer_contact" if "contact" in text and "current employer" in text else None
        return AnswerResolution(intent_id=intent, status="recognized", scope="employer", reason="The answer depends on the employer or application.")
    if isinstance(item, dict) and not label:
        identifier = normalized(item.get("name"))
        for intent, aliases in ALIASES.items():
            if identifier in aliases:
                return AnswerResolution(intent_id=intent, status="recognized", reason="Recognized field identifier.")
    auth = bool(re.search(r"\b(?:authorized|authorised|authorization|authorisation|eligible|eligibility|right)\b.*\bwork\b|\bwork\s+(?:authorization|authorisation|authorized|authorised)\b", text))
    sponsor = "sponsor" in text or "sponsorship" in text
    citizen = bool(re.search(r"\b(?:citizen|citizenship)\b", text))
    if auth or sponsor or citizen:
        work_country = re.search(r"\b(?:work|employment|job|position)\s+(?:legally\s+)?in\s+(?:the\s+)?(.+)", text)
        unknown_country = work_country and not re.match(r"(?:united states(?: of america)?|u s(?: a)?|usa|us)(?:\b|$)", work_country[1])
        if _FOREIGN.search(text) or unknown_country:
            return AnswerResolution(status="review", scope="application", reason="This eligibility question is not unambiguously scoped to the United States.")
        if (_NEGATIVE.search(text) or (auth and sponsor) or (citizen and (auth or sponsor))
                or re.search(r"\b(?:ever|previously|historically|clearance|export|itar|conviction|convicted)\b", text)):
            return AnswerResolution(status="review", scope="application", reason="Negative or combined eligibility wording needs an explicit answer for this wording.")
        # A finite country list cannot establish that every other country is
        # the US. Unrecognized residual words (including arbitrary country
        # names, dates and extra conditions) must not inherit the US fact.
        permitted = _ELIGIBILITY_WORDS | ({"citizen", "citizenship", "american"} if citizen else set())
        if set(text.split()) - permitted:
            return AnswerResolution(status="review", scope="application", reason="This eligibility wording adds a country, date, status or condition not covered by the saved fact.")
        if auth:
            if re.search(r"\b(?:future|will|would)\b|\bany employer\b", text):
                return AnswerResolution(status="review", scope="application", reason="Current work authorization does not establish future or unrestricted authorization for every employer.")
            return AnswerResolution("eligibility.work_authorized_us", "recognized", reason="US or unspecified work authorization question.")
        if sponsor:
            if not re.search(r"\b(?:need|require|requires|will|required|requiring|sponsorship)\b", text):
                return AnswerResolution(status="review", scope="application", reason="The sponsorship question's meaning is unclear.")
            intent = "eligibility.sponsorship_us"
            if "future" in text and not re.search(r"\b(?:now|currently|present|today)\b", text):
                intent = "eligibility.sponsorship_future_us"
            elif re.search(r"\b(?:now|currently|present|today)\b", text) and "future" not in text:
                intent = "eligibility.sponsorship_now_us"
            return AnswerResolution(intent, "recognized", reason="US or unspecified sponsorship requirement question.")
        if citizen and re.search(r"\b(?:us|u s|usa|united states|american)\b", text):
            return AnswerResolution("eligibility.us_citizen", "recognized", reason="US citizenship question.")
        return AnswerResolution(status="review", scope="application", reason="Citizenship requires an explicitly stated country.")
    if _NEGATIVE.search(text):
        return AnswerResolution(status="review", scope="application", reason="Negative wording is outside the reusable intent templates.")
    if re.fullmatch(r"(?:are you|are you a student) currently enrolled(?: in (?:a |an )?(?:college|university|degree|degree program|bachelors program|masters program))?", text):
        return AnswerResolution("education.currently_enrolled", "recognized", reason="Incoming and completed programs do not count as current enrollment.")
    # Complete templates deliberately reject any leftover clause. A willingness
    # fact is not a commitment to a particular country, schedule, duration,
    # funding arrangement, or additional qualification.
    templates = [
        ("location.relocation_assistance", r"(?:(?:do you|will you) (?:need|require)|(?:need|require)) (?:any |company |employer )?(?:relocation|moving) (?:assistance|support)"),
        ("location.self_funded_relocation", r"(?:can you|are you (?:willing|able) to) relocate at your own (?:expense|cost)"),
        ("location.relocate", r"(?:(?:are you|would you be) (?:willing|open|able|prepared) to (?:relocate|relocation)|(?:can you|would you) relocate|(?:willing|open) to (?:relocate|relocation))"),
        ("location.onsite", r"(?:(?:are you|would you be) (?:willing|able|comfortable) (?:to |with )?|(?:willing|able) to )work(?:ing)? (?:onsite|on site|in office|in an office)"),
        ("location.hybrid", r"(?:(?:are you|would you be) (?:willing|able|comfortable) (?:to |with )?|(?:willing|able) to )work(?:ing)? (?:a )?hybrid(?: schedule)?"),
        ("availability.immediate", r"(?:(?:are you )?available to|can you|are you able to) (?:start|begin)(?: a full time (?:job|role|position))? (?:immediately|asap)"),
        ("availability.full_time_after_internship", r"(?:can you|could you|are you able to|are you available to|would you be able to) (?:accept|take|start|work) (?:a )?full time(?: job| role| position)? (?:after|following) (?:an|the|your) internship"),
        ("availability.offer_deadline", r"(?:do you have|are there) (?:any |an )?(?:active )?(?:offer|recruiting|recruitment|offer or recruiting) deadlines?"),
        ("accessibility.accommodation", r"(?:do you|will you|would you) (?:need|require) (?:any |a |an )?(?:reasonable )?accommodations?"),
        ("eligibility.adult", r"(?:are you )?(?:18(?: years old| years of age)? (?:or older|or over|and over)|at least 18(?: years old| years of age)?)"),
        ("history.government_employment", r"have you (?:ever )?(?:worked for|been employed by) (?:a |the )?government(?: employer| agency)?"),
    ]
    matches = [intent for intent, pattern in templates if re.fullmatch(pattern, text)]
    if len(matches) == 1:
        return AnswerResolution(matches[0], "recognized", reason="Recognized complete single-fact question.")
    if re.search(r"\b(?:relocat\w*|onsite|on site|hybrid|immediately|after.{0,30}internship|government|nda|noncompete|non compete|18)\b", text):
        return AnswerResolution(status="review", scope="application", reason="Additional conditions or a different time scope require an exact answer for this application.")

    return AnswerResolution(reason="This wording does not match a supported single-fact intent.")


def _binding_matches(binding: dict, label: str, intent: str | None, job: dict | None) -> bool:
    if binding.get("question") != label and not (intent in {*CORE_INTENTS, *FACT_INTENTS} and binding.get("intent_id") == intent):
        return False
    scope = binding.get("scope")
    if scope == "application":
        return bool(job and job.get("id") and binding.get("job_id") == job["id"])
    if scope == "employer":
        return bool(job and job.get("company") and normalized(binding.get("company")) == normalized(job["company"]))
    return scope == "global" and not employer_dependent(label)


def _present(value):
    return value is not None and value != "" and value != []


def canonical_source_fingerprint(intent_id: str | None, profile: dict) -> str | None:
    """Bind a user-confirmed form choice to the fact version they reviewed."""
    application = profile.get("application_profile") or {}
    if intent_id in CORE_INTENTS:
        value = {"core": profile.get(CORE_INTENTS[intent_id])}
        if intent_id.startswith("education."):
            value.update(month=profile.get("graduation_month"), date=profile.get("graduation_date"), education=application.get("education", []))
    elif intent_id in FACT_INTENTS:
        facts = application.get("facts") or {}
        value = {"fact": facts.get(FACT_INTENTS[intent_id])}
        if intent_id == "compensation.target_base":
            value.update(mode=facts.get("salary_mode"), currency=facts.get("salary_currency"))
    elif intent_id in RECORD_INTENTS:
        value = application.get("education" if intent_id.startswith("education.") else "employment", [])
    else:
        return None
    return hashlib.sha256(json.dumps({"intent": intent_id, "value": value}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def explicit_boolean(answer):
    if type(answer) is bool:
        return answer
    text = str(answer or "").strip()
    if normalized(text) in {"yes", "true"} or re.match(r"^yes\s*[,;:—–-]\s*\S", text, re.I):
        return True
    if normalized(text) in {"no", "false"} or re.match(r"^no\s*[,;:—–-]\s*\S", text, re.I):
        return False
    return None


def canonical_boolean(intent_id, profile):
    if intent_id in CORE_INTENTS:
        value = profile.get(CORE_INTENTS[intent_id])
    elif intent_id in FACT_INTENTS:
        value = ((profile.get("application_profile") or {}).get("facts") or {}).get(FACT_INTENTS[intent_id])
    else:
        return None
    return value if type(value) is bool else None


def _graduation_value(intent: str, item: dict, profile: dict):
    month = profile.get("graduation_month")
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        return None
    year, number = month.split("-")
    if intent == "education.graduation_year":
        return year
    if item.get("type") == "month":
        return month
    if item.get("type") == "number":
        return str(int(number))
    choices = {number, str(int(number)), calendar.month_name[int(number)], calendar.month_abbr[int(number)]}
    available = [option for option in item.get("options", []) if option.get("label") in choices or str(option.get("value")) in choices]
    return available[0]["label"] if len(available) == 1 else calendar.month_name[int(number)]


def _record_resolution(intent, item, application):
    text = normalized(question_label(item))
    section = "education" if intent.startswith("education.") else "employment"
    records = [record for record in application.get(section, []) if isinstance(record, dict)]
    if text == "highest degree completed":
        return AnswerResolution(intent, "review", scope="application", reason="The highest completed degree needs a degree-level check; a primary preference does not establish the highest degree.")
    if item.get("ambiguous_record_control"):
        return AnswerResolution(intent, "conflict", scope="application", reason="Repeated record controls need an explicit record for each entry.")
    if intent == "education.currently_enrolled":
        # An empty history is unknown, not proof that the person is not enrolled.
        if not records:
            return AnswerResolution(intent, "missing", reason="Confirm your education history and enrollment status first.")
        return AnswerResolution(intent, "resolved", any(record.get("status") == "current" for record in records),
                                "application_profile.education.status", reason="Uses the explicitly confirmed enrollment statuses.")
    if section == "education":
        if "current" in text:
            records = [record for record in records if record.get("status") == "current"]
        elif "completed" in text:
            records = [record for record in records if record.get("status") == "completed"]
        if re.search(r"\b(?:undergraduate|bachelors)\b", text):
            records = [record for record in records if re.search(r"\b(?:bachelor|bachelors|bs|ba|bsc)\b", normalized(record.get("degree")))]
        elif re.search(r"\b(?:graduate|masters)\b", text):
            records = [record for record in records if re.search(r"\b(?:master|masters|ms|ma|msc|phd|doctor|doctorate)\b", normalized(record.get("degree")))]
    else:
        if "internship" in text:
            records = [record for record in records if record.get("kind") == "internship"]
        if "current" in text:
            records = [record for record in records if record.get("current") is True]
        if "previous" in text:
            records = [record for record in records if record.get("current") is False]
    primary = [record for record in records if record.get("primary_for_applications") is True]
    if len(primary) == 1:
        records = primary
    if len(records) != 1:
        return AnswerResolution(intent, "missing" if not records else "conflict", scope="application",
                                reason="This question does not identify one confirmed record. Choose the specific record for this application.")
    record = records[0]
    value = record.get(RECORD_INTENTS[intent])
    if intent == "education.gpa" and record.get("status") not in {"completed", "current"}:
        value = None
    if intent.endswith("_month") and value:
        if item.get("type") == "date":
            value = None
        elif item.get("type") != "month":
            # A visible month/year text control may receive known precision.
            value = str(value)
    if type(value) in {int, float}:
        value = str(value)
    return AnswerResolution(intent, "resolved" if _present(value) else "missing", value,
                            f"application_profile.{section}.{record.get('id')}.{RECORD_INTENTS[intent]}", "application",
                            "Uses the explicitly identified record; missing precision stays unknown.")


def resolve_question(item: dict, profile: dict, job: dict | None = None) -> AnswerResolution:
    """Resolve facts, scoped bindings, then legacy exact answers in that order."""
    label = question_label(item)
    classification = classify_question(item, job)
    intent = classification.intent_id
    if protected_question(label):
        return classification
    application = profile.get("application_profile") or {}
    facts = application.get("facts") or {}
    if intent and intent.startswith("eligibility.sponsorship"):
        aggregate = profile.get("needs_sponsorship")
        periods = [facts.get("sponsorship_now"), facts.get("sponsorship_future")]
        if (aggregate is False and any(period is True for period in periods)) or (aggregate is True and all(period is False for period in periods)):
            return AnswerResolution(intent, "conflict", reason="Your aggregate and time-specific sponsorship facts disagree.")
    if intent in {"education.graduation_date", "education.graduation_month", "education.graduation_year"}:
        primary = [record for record in application.get("education", []) if isinstance(record, dict) and record.get("primary_for_applications") is True]
        primary_month = primary[0].get("end_month") if len(primary) == 1 else None
        explicit_month = profile.get("graduation_month") or str(profile.get("graduation_date") or "")[:7]
        if primary_month and explicit_month and primary_month != explicit_month:
            return AnswerResolution(intent, "conflict", reason="Your primary education graduation month disagrees with your saved graduation month. Resolve the degree/date in your profile once.")
    # A selected employer option can format a fact differently from the profile
    # (e.g. a full city label). It is usable only for this exact question and
    # scope while the reviewed canonical fact remains unchanged.
    if classification.status == "recognized" and (item.get("type") in {"select", "radio"} or item.get("tag") == "select") and not item.get("ambiguous_record_control"):
        bridge = [binding for binding in application.get("saved_answer_bindings", []) if isinstance(binding, dict)
                  and binding.get("question") == label and binding.get("scope") in {"employer", "application"}
                  and binding.get("source_fingerprint") and binding.get("source_fingerprint") == canonical_source_fingerprint(intent, profile)
                  and _binding_matches(binding, label, intent, job)]
        known_boolean = canonical_boolean(intent, profile)
        bridge = [binding for binding in bridge if known_boolean is None or explicit_boolean(binding.get("answer")) in {None, known_boolean}]
        if bridge:
            highest = "application" if any(binding.get("scope") == "application" for binding in bridge) else "employer"
            bridge = [binding for binding in bridge if binding.get("scope") == highest]
            if len({(type(binding.get("answer")).__name__, str(binding.get("answer"))) for binding in bridge}) == 1:
                return AnswerResolution(intent, "resolved", bridge[-1]["answer"], "application_profile.saved_answer_bindings", highest,
                                        "Uses your explicit current form choice, bound to the reviewed fact version.")
    salary_per_application = False
    if intent == "compensation.target_base":
        numeric = item.get("type") == "number"
        currencies = {"usd": "USD", "eur": "EUR", "gbp": "GBP", "cad": "CAD", "aud": "AUD"}
        stated = [code for word, code in currencies.items() if re.search(r"\b" + word + r"\b", normalized(label))]
        expected = stated[0] if len(stated) == 1 else "USD" if numeric and not stated else None
        salary_per_application = (facts.get("salary_mode") == "ask" or
                                  (numeric and facts.get("salary_mode") == "posted_range") or
                                  (expected is not None and facts.get("salary_currency") != expected))
        if salary_per_application:
            classification = AnswerResolution(intent, "missing", scope="application", reason="Confirm a salary for this application; your salary mode or currency does not authorize a numeric amount here.")
    if classification.status == "recognized" and (intent in RECORD_INTENTS or intent == "education.currently_enrolled"):
        record_result = _record_resolution(intent, item, application)
        if item.get("ambiguous_record_control"):
            return record_result
        if record_result.status == "resolved":
            return record_result
        # Scoped, explicitly confirmed form answers can disambiguate a record.
        classification = record_result
    if classification.status == "recognized" and intent in {"education.graduation_date", "education.graduation_month", "education.graduation_year"}:
        primary = [record for record in application.get("education", []) if isinstance(record, dict) and record.get("primary_for_applications") is True]
        primary_month = primary[0].get("end_month") if len(primary) == 1 else None
        explicit_month = profile.get("graduation_month") or str(profile.get("graduation_date") or "")[:7]
        if primary_month and explicit_month and primary_month != explicit_month:
            return AnswerResolution(intent, "conflict", reason="Your primary education graduation month disagrees with your saved graduation month. Resolve the degree/date in your profile once.")
        if primary_month and not explicit_month:
            if intent == "education.graduation_date":
                value = primary_month if item.get("type", "text") != "date" else None
            else:
                value = _graduation_value(intent, item, {"graduation_month": primary_month})
            if value is not None:
                return AnswerResolution(intent, "resolved", value, "application_profile.education.primary_for_applications.end_month",
                                        reason="Uses the known month/year of the degree you explicitly selected for applications.")
    # Existing typed core fields remain authoritative over older exact answers.
    if classification.status == "recognized" and intent in CORE_INTENTS:
        key = CORE_INTENTS[intent]
        value = profile.get(key)
        if intent in {"education.graduation_month", "education.graduation_year"}:
            value = _graduation_value(intent, item, profile)
        elif intent == "education.graduation_date" and item.get("type") == "month":
            value = profile.get("graduation_month")
        if intent in {"eligibility.work_authorized_us", "eligibility.sponsorship_us"} and type(value) is not bool:
            value = None
        if intent == "eligibility.sponsorship_us" and type(value) is bool:
            periods = [facts.get("sponsorship_now"), facts.get("sponsorship_future")]
            if (value is False and any(period is True for period in periods)) or (value is True and all(period is False for period in periods)):
                return AnswerResolution(intent, "conflict", reason="Your aggregate and time-specific sponsorship facts disagree.")
        if _present(value):
            if intent == "skills" and isinstance(value, list):
                value = ", ".join(value)
            elif intent == "experience.months":
                value = str(value)
            return AnswerResolution(intent, "resolved", value, f"profile.{key}", reason="Uses your explicitly saved profile fact.")
    if classification.status == "recognized" and intent in FACT_INTENTS:
        key = FACT_INTENTS[intent]
        value = facts.get(key)
        if intent == "compensation.target_base" and facts.get("salary_mode") == "posted_range":
            if item.get("type", "text") in {"text", "textarea"}:
                return AnswerResolution(intent, "resolved", "Open to the posted range; total compensation is negotiable.",
                                        "application_profile.facts.salary_mode", reason="Uses your confirmed preference for the employer's posted range.")
            value = None
        # A legacy aggregate sponsorship answer is also sufficient for either
        # time period, but conflicting explicit per-period facts require review.
        if intent in {"eligibility.sponsorship_now_us", "eligibility.sponsorship_future_us"}:
            aggregate = profile.get("needs_sponsorship")
            if value is True and aggregate is False:
                return AnswerResolution(intent, "conflict", scope="global", reason="Your aggregate and time-specific sponsorship facts disagree.")
            if value is None and aggregate is False:
                return AnswerResolution(intent, "resolved", False, "profile.needs_sponsorship", reason="Your confirmed no-sponsorship answer covers now and the future.")
        if _present(value):
            if type(value) in {int, float}:
                value = str(value)
            return AnswerResolution(intent, "resolved", value, f"application_profile.facts.{key}", reason="Uses your explicitly confirmed application fact.")
    if intent == "eligibility.sponsorship_us" and classification.status == "recognized":
        now, future = facts.get("sponsorship_now"), facts.get("sponsorship_future")
        if type(now) is bool and type(future) is bool:
            return AnswerResolution(intent, "resolved", now or future, "application_profile.facts.sponsorship_now+sponsorship_future", reason="Both time periods were explicitly confirmed.")
    # Review classifications do not fall through to broad legacy facts. An
    # application-scoped exact answer can still resolve ordinary negative wording.
    bindings = application.get("saved_answer_bindings") or []
    matches = [binding for binding in bindings if isinstance(binding, dict) and _binding_matches(binding, label, intent, job)
               and not binding.get("source_fingerprint")
               and (not salary_per_application or binding.get("scope") == "application")
               and (classification.status != "review" or (binding.get("scope") == "application" and binding.get("question") == label))]
    if matches:
        priority = {"global": 0, "employer": 1, "application": 2}
        best = max(priority.get(binding.get("scope"), -1) for binding in matches)
        matches = [binding for binding in matches if priority.get(binding.get("scope"), -1) == best]
        values = {(type(binding.get("answer")).__name__, str(binding.get("answer"))) for binding in matches}
        if len(values) != 1:
            return AnswerResolution(intent, "conflict", scope=classification.scope, reason="Confirmed answers in this scope disagree.")
        binding = matches[-1]
        return AnswerResolution(intent, "resolved", binding["answer"], "application_profile.saved_answer_bindings", binding["scope"], "Uses an answer confirmed for this scope.")
    if classification.status == "review":
        return classification
    if salary_per_application:
        return classification
    if intent in RECORD_INTENTS or intent == "education.currently_enrolled":
        return classification
    if label in (profile.get("answers") or {}) and not employer_dependent(label):
        return AnswerResolution(intent, "resolved", profile["answers"][label], "profile.answers", reason="Uses a legacy answer for this exact question; no semantic promotion was inferred.")
    return AnswerResolution(intent, "missing" if intent else "unknown", scope=classification.scope, reason=classification.reason)


def field_answer(resolution: AnswerResolution, item: dict):
    """Render a known yes/no fact for a free-text question without guessing."""
    if resolution.status != "resolved":
        return None
    value = resolution.value
    kind = "select" if item.get("tag") == "select" else item.get("type", "text")
    if type(value) is bool and kind in {"text", "textarea", "search"}:
        return "Yes" if value else "No"
    if resolution.intent_id == "education.degree" and kind in {"select", "radio"} and isinstance(value, str):
        def degree_level(text):
            text = normalized(text)
            levels = {
                "bachelor": {"bachelor", "bachelors", "bachelor s", "bachelor degree", "bachelors degree", "bachelor s degree", "bachelor of science", "bachelor of arts", "ba", "bs", "bsc"},
                "master": {"master", "masters", "master s", "master degree", "masters degree", "master s degree", "master of science", "master of arts", "ma", "ms", "msc"},
                "doctorate": {"phd", "ph d", "doctorate", "doctoral degree", "doctor of philosophy"},
                "associate": {"associate", "associates", "associate s", "associate degree", "associates degree", "associate s degree", "aa", "as"},
            }
            return next((level for level, names in levels.items() if text in names), None)
        level = degree_level(value)
        matches = [option for option in item.get("options", []) if level and not option.get("disabled") and
                   degree_level(str(option.get("label") or "")) == level]
        if len(matches) == 1:
            return str(matches[0]["label"])
    return value


def pending_group_key(question: str, resolution: AnswerResolution, job: dict | None = None) -> str:
    key = resolution.intent_id or "exact:" + question
    if resolution.scope == "employer":
        key += ":employer:" + normalized((job or {}).get("company"))
    elif resolution.scope == "application" or resolution.status == "review":
        key += ":application:" + str((job or {}).get("id", ""))
    return key
