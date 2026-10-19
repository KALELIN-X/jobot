import pytest

from jobradar.ranking import rank_job


def ranked(description, **changes):
    job = {"title": "Software Engineer", "location": "Boston, MA", "ats": "greenhouse",
           "closed": False, "description": description}
    job.update(changes.pop("job_changes", {}))
    profile = {"graduation_month": "2024-07", "experience_months": 12,
               "approved_work_locations": ["United States"], "work_authorized": True,
               "needs_sponsorship": False}
    rules = {"locations": ["United States"], "allowed_ats": ["greenhouse"],
             "max_required_experience_months": 12, **changes}
    return rank_job(job, profile, "Built Python services.", rules)


def entry(result, label):
    return next(item for item in result["eligibility"] if item["label"] == label)


def test_graduation_window_toggle_preserves_factual_month_and_other_gates():
    description = "Graduating in 2027. Required: 2+ years of professional experience. Python."
    enforced = ranked(description)
    waived = ranked(description, enforce_graduation_window=False)
    assert entry(enforced, "Graduation date")["status"] == "fail"
    assert entry(waived, "Graduation date")["status"] == "waived"
    assert "2024-07" in entry(waived, "Graduation date")["detail"]
    assert "2024-07-01" not in str(waived)
    assert entry(waived, "Experience filter")["status"] == "fail"
    assert entry(waived, "Experience")["status"] == "fail"
    assert entry(waived, "Approved location")["status"] == "pass"
    assert entry(waived, "Work location")["status"] == "pass"


@pytest.mark.parametrize("separator", ["to", "–", "—", "-"])
def test_explicit_experience_ranges_keep_both_bounds(separator):
    result = ranked(f"Required qualifications:\n0 {separator} 2 years of professional experience.\nPython.")
    requirement = result["experience_requirement"]
    assert requirement["minimum_months"] == 0
    assert requirement["maximum_months"] == 24
    assert entry(result, "Experience filter")["status"] == "pass"


def test_source_new_grad_category_does_not_invent_experience_or_fit():
    ordinary = ranked("Python is required.")
    listed = ranked("Python is required.", job_changes={"source_new_grad": True})
    assert ordinary["is_new_grad"] is False
    assert listed["is_new_grad"] is True
    assert listed["score"] == ordinary["score"]
    assert listed["experience_requirement"]["minimum_months"] is None
    assert entry(listed, "Experience filter")["status"] == "unknown"
