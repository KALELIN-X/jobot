import pytest
from pydantic import ValidationError

from jobradar.approval import fingerprint, job_problems
from jobradar.models import Profile, Rules
from test_destinations import fetched


def test_month_precision_does_not_invent_a_graduation_day():
    profile = Profile(graduation_month="2024-07")
    assert profile.graduation_month == "2024-07"
    assert profile.graduation_date is None


@pytest.mark.parametrize("value", ["2024-7", "2024-00", "2024-13", "2024-07-01", "0000-01", 202407, True, ""])
def test_graduation_month_requires_a_real_calendar_month_string(value):
    with pytest.raises(ValidationError):
        Profile(graduation_month=value)


def test_graduation_enforcement_is_on_by_default_and_changes_approval_fingerprint():
    assert Rules().enforce_graduation_window is True
    with pytest.raises(ValidationError):
        Rules(enforce_graduation_window="false")
    assert fingerprint({}, Rules().model_dump(), {}) != fingerprint({}, Rules(enforce_graduation_window=False).model_dump(), {})


@pytest.mark.parametrize("label", ["Graduation", "Graduation date", "Graduation window"])
@pytest.mark.parametrize("status", ["fail", "unknown"])
def test_only_known_graduation_checks_can_be_explicitly_waived(label, status):
    job = fetched()
    job["score_detail"]["eligibility"] = [{"label": label, "status": status, "detail": "Posting's date window differs"}]
    assert job_problems(job, Rules().model_dump())
    assert not job_problems(job, Rules(enforce_graduation_window=False).model_dump())


@pytest.mark.parametrize("other_check", ["Work authorization", "Experience", "Work location", "Citizenship"])
def test_waiving_graduation_does_not_waive_other_eligibility(other_check):
    job = fetched()
    job["score_detail"]["eligibility"] = [{"label": other_check, "status": "fail", "detail": "Still not met"}]
    assert any(other_check in problem for problem in job_problems(job, Rules(enforce_graduation_window=False).model_dump()))


def test_waiving_graduation_never_overrides_experience_cap_or_fit_threshold():
    job = fetched()
    job["score_detail"]["experience_requirement"]["minimum_months"] = 24
    assert any("24 months" in problem for problem in job_problems(job, Rules(max_required_experience_months=12, enforce_graduation_window=False).model_dump()))
    job["score"] = 1
    assert any("ranking is below" in problem for problem in job_problems(job, Rules(enforce_graduation_window=False).model_dump()))
