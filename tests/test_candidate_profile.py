"""Synthetic candidate records: no real applicant data or employer requests."""

import json

import pytest
from pydantic import ValidationError

from jobradar.candidate_profile import (
    APPLICATION_FACT_KEYS,
    ONBOARDING_STEPS,
    ApplicationFacts,
    ApplicationProfile,
    AvailabilityPeriod,
    EducationRecord,
    EmploymentRecord,
    SavedAnswerBinding,
    onboarding_catalog,
)


def education(**changes):
    return {"id": "education-1", "institution": "Example University", "degree": "BS",
            "status": "completed", "start_month": "2020-09", "end_month": "2024-05", **changes}


def employment(**changes):
    return {"id": "work-1", "company": "Example Employer", "title": "Engineer", "kind": "employee",
            "start_month": "2024-07", "end_month": "2025-08", "current": False, **changes}


def period(**changes):
    return {"id": "availability-1", "start_date": "2027-06-01", "end_date": "2027-08-31",
            "hours_per_week": 40, "weeks": 12, **changes}


def test_new_profile_never_turns_unknown_into_no_or_candidate_defaults():
    profile = ApplicationProfile().model_dump()
    assert profile["version"] == 1
    assert set(profile["facts"]) == APPLICATION_FACT_KEYS
    assert all(value is None for value in profile["facts"].values())
    assert all(profile[key] == [] for key in ("education", "employment", "availability", "completed_steps", "saved_answer_bindings"))
    assert profile["confirmed_at"] == {}
    assert "first_name" not in profile["facts"] and "work_authorized" not in profile["facts"]


@pytest.mark.parametrize("field,value", [("sponsorship_now", "false"), ("us_citizen", 1),
                                         ("completed_internships", "1"), ("domestic_travel_percent", True),
                                         ("target_base_salary", True), ("salary_currency", "usd")])
def test_facts_reject_coercion_and_invalid_currency(field, value):
    with pytest.raises(ValidationError):
        ApplicationFacts.model_validate({field: value})


def test_sponsorship_timing_and_explicit_false_remain_distinct():
    facts = ApplicationFacts(sponsorship_now=False, sponsorship_future=True)
    assert facts.sponsorship_now is False
    assert facts.sponsorship_future is True
    assert facts.us_citizen is None


@pytest.mark.parametrize("value", [-1, 101, 1.5])
def test_travel_is_a_bounded_whole_percentage(value):
    with pytest.raises(ValidationError):
        ApplicationFacts(domestic_travel_percent=value)


def test_salary_target_requires_currency_and_does_not_imply_a_minimum():
    with pytest.raises(ValidationError):
        ApplicationFacts(salary_mode="target", target_base_salary=100_000)
    facts = ApplicationFacts(salary_mode="target", target_base_salary=100_000, salary_currency="USD")
    assert facts.target_base_salary == 100_000
    assert "minimum_salary" not in facts.model_dump()
    assert ApplicationFacts(salary_mode="posted_range").target_base_salary is None


def test_offer_deadline_preserves_unknown_and_rejects_a_conflicting_no():
    assert ApplicationFacts(has_offer_deadline=True).offer_deadline is None
    with pytest.raises(ValidationError):
        ApplicationFacts(has_offer_deadline=False, offer_deadline="2027-02-10")
    with pytest.raises(ValidationError):
        ApplicationFacts(offer_deadline="2027-02-30")


def test_distinct_completed_and_incoming_programs_keep_their_own_status_and_gpa():
    profile = ApplicationProfile(education=[
        EducationRecord(**education(gpa=3.1, gpa_scale=4)),
        EducationRecord(**education(id="education-2", institution="Another University", degree="MS",
                                    status="incoming", start_month="2027-01", end_month="2028-05")),
    ])
    assert profile.education[0].gpa == 3.1
    assert profile.education[1].gpa is None
    assert profile.education[1].status == "incoming"


def test_no_education_or_employment_record_is_automatically_primary():
    profile = ApplicationProfile(education=[EducationRecord(**education())],
                                 employment=[EmploymentRecord(**employment())])
    assert profile.education[0].primary_for_applications is False
    assert profile.employment[0].primary_for_applications is False


def test_explicit_primary_preferences_do_not_change_incoming_or_past_status():
    profile = ApplicationProfile(education=[EducationRecord(**education(
        status="incoming", primary_for_applications=True))], employment=[
        EmploymentRecord(**employment(primary_for_applications=True))])
    assert profile.education[0].status == "incoming"
    assert profile.education[0].primary_for_applications is True
    assert profile.employment[0].current is False
    assert profile.employment[0].primary_for_applications is True


@pytest.mark.parametrize("field,records", [
    ("education", [education(primary_for_applications=True),
                   education(id="education-2", primary_for_applications=True)]),
    ("employment", [employment(primary_for_applications=True),
                    employment(id="work-2", primary_for_applications=True)]),
])
def test_only_one_primary_record_of_each_kind_is_allowed(field, records):
    with pytest.raises(ValidationError, match="at most one primary"):
        ApplicationProfile.model_validate({field: records})


@pytest.mark.parametrize("record", [EducationRecord, EmploymentRecord])
def test_primary_preference_is_an_explicit_boolean(record):
    values = education() if record is EducationRecord else employment()
    with pytest.raises(ValidationError):
        record.model_validate({**values, "primary_for_applications": "true"})


@pytest.mark.parametrize("changes", [
    {"status": "incoming", "gpa": 4.0, "gpa_scale": 4.0},
    {"status": "withdrawn", "gpa": 3.0, "gpa_scale": 4.0},
    {"gpa": 3.0}, {"gpa_scale": 4.0}, {"gpa": 4.1, "gpa_scale": 4.0},
    {"start_month": "2024-06", "end_month": "2024-05"},
    {"start_month": "2020-09-01"}, {"end_month": "2024-13"},
])
def test_education_rejects_invented_gpa_bad_chronology_or_date_precision(changes):
    with pytest.raises(ValidationError):
        EducationRecord(**education(**changes))


def test_zero_earned_gpa_is_not_treated_as_missing_and_dates_can_be_unknown():
    record = EducationRecord(**education(gpa=0, gpa_scale=4, start_month=None, end_month=None))
    assert record.gpa == 0
    assert record.start_month is None and record.end_month is None


def test_withdrawn_program_is_representable_without_becoming_a_completed_degree():
    record = EducationRecord(**education(status="withdrawn"))
    assert record.status == "withdrawn"
    assert record.gpa is None
    statuses = next(field for step in onboarding_catalog() for group in step["records"]
                    if group["key"] == "education" for field in group["fields"] if field["key"] == "status")
    assert any(option["value"] == "withdrawn" for option in statuses["options"])


def test_separate_address_and_start_month_fields_have_no_implicit_values_or_day():
    facts = ApplicationFacts(current_country="United States", current_state="Oregon", postal_code="97000",
                             earliest_start_month="2027-03")
    assert facts.postal_code == "97000"
    assert facts.earliest_start_month == "2027-03"
    assert facts.available_immediately is None
    with pytest.raises(ValidationError):
        ApplicationFacts(earliest_start_month="2027-03-01")
    assert ApplicationFacts().current_country is None


@pytest.mark.parametrize("changes", [
    {"current": False, "end_month": None}, {"current": True, "end_month": "2025-08"},
    {"start_month": "2025-09"}, {"start_month": "2024-07-01"}, {"current": "true"},
])
def test_employment_requires_actual_months_and_consistent_current_status(changes):
    with pytest.raises(ValidationError):
        EmploymentRecord(**employment(**changes))


def test_current_work_and_a_completed_internship_remain_separate_records():
    profile = ApplicationProfile(employment=[
        EmploymentRecord(**employment(current=True, end_month=None)),
        EmploymentRecord(**employment(id="work-2", kind="internship", start_month="2023-06", end_month="2023-08")),
    ])
    assert profile.employment[0].current is True
    assert profile.employment[1].kind == "internship"
    assert profile.facts.completed_internships is None  # no automatic assertion from incomplete history


@pytest.mark.parametrize("changes", [
    {"end_date": "2027-05-31"}, {"start_date": "2027-06"}, {"end_date": "2030-01-01"},
    {"hours_per_week": 0}, {"hours_per_week": "40"}, {"weeks": 20},
])
def test_availability_has_real_dates_bounded_hours_and_a_feasible_duration(changes):
    with pytest.raises(ValidationError):
        AvailabilityPeriod(**period(**changes))


def test_one_summer_availability_does_not_expand_into_other_years():
    profile = ApplicationProfile(availability=[AvailabilityPeriod(**period())])
    assert profile.availability[0].start_date == "2027-06-01"
    assert profile.availability[0].end_date == "2027-08-31"
    assert profile.facts.available_immediately is None


@pytest.mark.parametrize("field,records", [
    ("education", [education(), education()]),
    ("employment", [employment(), employment()]),
    ("availability", [period(), period()]),
])
def test_duplicate_record_identifiers_are_rejected(field, records):
    with pytest.raises(ValidationError):
        ApplicationProfile.model_validate({field: records})


@pytest.mark.parametrize("scope,context", [
    ("employer", {}), ("application", {}), ("global", {"company": "Example"}),
    ("global", {"job_id": "job-1"}), ("employer", {"company": "Example", "job_id": "job-1"}),
])
def test_answer_scope_cannot_silently_become_global_or_another_employer(scope, context):
    with pytest.raises(ValidationError):
        SavedAnswerBinding(question="Have you worked here before?", answer=False, scope=scope, **context)


def test_binding_preserves_boolean_answers_and_per_employer_context():
    value = SavedAnswerBinding(question="Have you worked here before?", answer=False,
                               scope="employer", company="Example", confirmed_at="2026-10-20T12:00:00-05:00")
    assert value.answer is False
    assert value.scope == "employer"
    assert value.job_id is None


def test_scoped_live_choice_can_identify_the_confirmed_source_fact_version():
    binding = SavedAnswerBinding(question="Location (City)", answer="Example City, Example State, US",
                                 scope="application", job_id="job-1", intent_id="contact.location",
                                 source_fingerprint="a" * 64)
    assert binding.source_fingerprint == "a" * 64
    assert binding.scope == "application"
    assert SavedAnswerBinding(question="Favorite editor?", answer="Example", scope="global").source_fingerprint is None


@pytest.mark.parametrize("value", ["a" * 63, "a" * 65, "g" * 64, "A" * 64, True])
def test_live_choice_source_fingerprint_is_a_strict_sha256(value):
    with pytest.raises(ValidationError):
        SavedAnswerBinding(question="Location (City)", answer="Example City", scope="employer",
                           company="Example", source_fingerprint=value)


def test_live_choice_fingerprint_cannot_make_an_employers_option_global():
    with pytest.raises(ValidationError, match="employer or application scope"):
        SavedAnswerBinding(question="Location (City)", answer="Example City", scope="global",
                           source_fingerprint="a" * 64)


@pytest.mark.parametrize("question", [
    "I accept the Candidate Arbitration Agreement", "I have read the Candidate Privacy Notice",
    "Password", "Enter your verification code", "Complete CAPTCHA",
])
def test_binding_never_stores_agreement_acceptance_read_acknowledgments_or_credentials(question):
    with pytest.raises(ValidationError):
        SavedAnswerBinding(question=question, answer=True, scope="global")


@pytest.mark.parametrize("value", ["2026-10-20", "2026-10-20T12:00:00", "not a timestamp"])
def test_confirmation_requires_an_actual_timezone_timestamp(value):
    with pytest.raises(ValidationError):
        ApplicationProfile(confirmed_at={"facts.sponsorship_now": value})


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_profile_version_cannot_be_coerced(value):
    with pytest.raises(ValidationError):
        ApplicationProfile(version=value)


def test_profile_validation_rejects_extra_data_and_repeated_steps():
    with pytest.raises(ValidationError):
        ApplicationProfile.model_validate({"password": "synthetic-only"})
    with pytest.raises(ValidationError):
        ApplicationProfile(completed_steps=["identity", "identity"])


def test_catalog_has_ordered_steps_separate_storage_and_no_seeded_answers():
    catalog = onboarding_catalog()
    assert [step["id"] for step in catalog] == list(ONBOARDING_STEPS)
    assert all({"title", "description", "fields", "records"} <= step.keys() for step in catalog)
    fields = [field for step in catalog for field in step["fields"]]
    assert all({"key", "label", "type", "options", "description", "examples", "storage"} <= field.keys() for field in fields)
    assert all("value" not in field and "default" not in field for field in fields)
    assert all(field["key"] in APPLICATION_FACT_KEYS for field in fields if field["storage"] == "facts")
    assert next(field for field in fields if field["key"] == "work_authorized")["storage"] == "profile"
    assert next(field for field in fields if field["key"] == "name")["storage"] == "profile"
    assert {record["key"] for step in catalog for record in step["records"]} == {"education", "employment", "availability"}
    json.dumps(catalog)


def test_catalog_is_fresh_so_one_request_cannot_mutate_future_requests():
    catalog = onboarding_catalog()
    catalog[0]["fields"][0]["label"] = "Changed"
    assert onboarding_catalog()[0]["fields"][0]["label"] == "Legal full name"


def test_record_catalog_exposes_required_fields_and_numeric_precision():
    groups = {record["key"]: {field["key"]: field for field in record["fields"]}
              for step in onboarding_catalog() for record in step["records"]}
    assert groups["education"]["institution"]["required"] is True
    assert groups["education"]["status"]["required"] is True
    assert groups["education"]["gpa"]["step"] == "any"
    assert groups["employment"]["current"]["required"] is True
    assert groups["employment"]["start_month"]["required"] is True
    assert groups["availability"]["start_date"]["required"] is True
    assert groups["availability"]["hours_per_week"]["step"] == 1
    assert groups["availability"]["hours_per_week"]["max"] == 100
    assert groups["education"]["primary_for_applications"]["default"] is False
    assert groups["employment"]["primary_for_applications"]["default"] is False
    assert "default" not in groups["employment"]["current"]
