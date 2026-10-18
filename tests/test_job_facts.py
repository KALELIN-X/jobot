"""Synthetic posting facts: direct evidence, missing facts and ambiguity."""
import copy
import json

import pytest

from jobradar.job_facts import MAX_EVIDENCE, MAX_SUMMARY, job_facts, published_pay


def test_compact_facts_keep_only_the_requested_posting_requirements():
    job = {"title": "Software Engineer", "location": "Boston, MA", "work_model": "hybrid",
           "description": """About us
We build developer tools and serve customers around the world.
Qualifications
Bachelor's degree in Computer Science or equivalent practical experience.
Required: 0-1 years of software engineering experience.
Candidates must graduate between May 2026 and June 2027.
Compensation
Base salary USD 100,000 - USD 120,000 per year, plus equity.
"""}
    original = copy.deepcopy(job)
    facts = job_facts(job)
    assert job == original
    assert set(facts) == {"location", "degree", "graduation", "experience", "pay"}
    assert all(value["status"] == "stated" for value in facts.values())
    assert facts["location"]["summary"] == "Boston, MA · hybrid"
    assert "equivalent practical experience" in facts["degree"]["summary"]
    assert "May 2026" in facts["graduation"]["summary"]
    assert facts["experience"]["minimum_months"] == 0 and facts["experience"]["maximum_months"] == 12
    assert facts["pay"]["ranges"][0] == {"minimum": 100000, "maximum": 120000, "currency": "USD", "currency_symbol": None,
                                        "period": "year", "kind": "base", "scope": ""}
    assert "serve customers" not in json.dumps(facts)


@pytest.mark.parametrize("job", [None, [], {}, {"description": ["not text"], "location": {}, "requirements": 4,
                                                "published_pay": [None, {"minimum": True, "maximum": float("nan"), "period": []}],
                                                "score_detail": {"experience_requirement": {"status": []}}}])
def test_missing_or_malformed_input_does_not_manufacture_facts(job):
    facts = job_facts(job)
    assert all(value["status"] == "not_stated" for value in facts.values())
    assert facts["pay"]["ranges"] == []
    assert facts["experience"]["minimum_months"] is None


def test_current_ranker_experience_evidence_is_reused_without_applicant_eligibility_details():
    requirement = {"status": "known", "minimum_months": 6, "maximum_months": 12, "evidence": ["6-12 months of relevant experience required."]}
    facts = job_facts({"score_detail": {"experience_requirement": requirement,
                                      "eligibility": [{"label": "Graduation date", "detail": "PRIVATE CANDIDATE INFORMATION"}]}})
    assert facts["experience"]["summary"] == "6 months–1 year required"
    assert facts["experience"]["evidence"] == requirement["evidence"]
    assert "PRIVATE" not in json.dumps(facts)


@pytest.mark.parametrize("requirement", [
    {"status": "known", "minimum_months": 12, "maximum_months": "invalid"},
    {"status": "known", "minimum_months": True, "maximum_months": 12},
    {"status": "known", "minimum_months": 24, "maximum_months": 12},
])
def test_malformed_numeric_experience_is_reviewable(requirement):
    fact = job_facts({"score_detail": {"experience_requirement": requirement}})["experience"]
    assert fact["status"] == "unclear" and fact["minimum_months"] is None and fact["maximum_months"] is None


def test_new_grad_does_not_infer_zero_experience_or_graduation_dates():
    facts = job_facts({"title": "New Grad Backend Engineer", "source_new_grad": True,
                      "description": "We welcome recent graduates. Master's degree preferred."})
    assert facts["degree"]["status"] == "stated" and "preferred" in facts["degree"]["summary"]
    assert facts["graduation"]["status"] == facts["experience"]["status"] == "not_stated"


def test_alternative_education_experience_path_stays_unclear():
    facts = job_facts({"description": "Requirements\nBachelor's degree and 2 years of experience, or equivalent education and projects."})
    assert facts["degree"]["status"] == "stated"
    assert facts["experience"]["status"] == "unclear"
    assert facts["experience"]["minimum_months"] is None


@pytest.mark.parametrize("text", [
    "Our founders graduated from college in 2008. We hire engineers of all backgrounds.",
    "Our company was founded in 2020, and we welcome new graduates.",
    "The team is given a high degree of autonomy. Our tools are used by thousands of teams.",
])
def test_company_history_and_non_academic_degree_do_not_become_requirements(text):
    facts = job_facts({"description": text})
    assert facts["degree"]["status"] == facts["graduation"]["status"] == "not_stated"


@pytest.mark.parametrize("title,text", [
    ("Software Engineer - Class of 2027", "Build Python services."),
    ("2026 New Grad Software Engineer", "Build Python services."),
    ("Software Engineer", "Candidates must apply within 12 months of graduation."),
])
def test_direct_title_or_relative_applicant_graduation_window_is_preserved(title, text):
    assert job_facts({"title": title, "description": text})["graduation"]["status"] == "stated"


@pytest.mark.parametrize("text", [
    "Our company has raised $50 million in funding. We offer a competitive salary.",
    "$100 billion in assets. Competitive annual compensation is offered.",
    "Salary is competitive. Our products generate USD 100,000 in revenue per month.",
    "The department budget is USD 120,000 per year and pay is competitive.",
    "Benefits include an annual USD 1,000 wellness stipend. A competitive salary is offered.",
    "The annual bonus may be USD 10,000. Equity includes 5,000 shares.",
    "Hourly customer spend is USD 60. Competitive compensation is offered.",
    "Pay is USD 120,000-CAD 150,000 per year.",
    "Salary range is USD 100-120k per year.",
])
def test_non_salary_ambiguous_units_or_mixed_currency_do_not_become_pay_ranges(text):
    fact = job_facts({"description": text})["pay"]
    assert fact["status"] == "not_stated" and fact["ranges"] == []


@pytest.mark.parametrize("text,minimum,maximum,currency,period,kind,status", [
    ("Base salary USD 100,000 - USD 120,000 per year, plus equity.", 100000, 120000, "USD", "year", "base", "stated"),
    ("Base salary USD $100,000 to $120,000 per year.", 100000, 120000, "USD", "year", "base", "stated"),
    ("Hourly pay range: CAD$ 40.50 - $50.75 per hour.", 40.5, 50.75, "CAD", "hour", "pay", "stated"),
    ("Salary range: $120,000 - $150,000 USD annually, plus equity.", 120000, 150000, "USD", "year", "pay", "stated"),
    ("Hourly pay range: CAD 40.50 - CAD 50.75 per hour.", 40.5, 50.75, "CAD", "hour", "pay", "stated"),
    ("Total compensation is USD 180k-220k per year including bonus and equity.", 180000, 220000, "USD", "year", "total_compensation", "stated"),
    ("The hourly pay range is $45-$55 per hour.", 45, 55, None, "hour", "pay", "unclear"),
    ("Salary is GBP 50,000 - GBP 60,000.", 50000, 60000, "GBP", None, "pay", "unclear"),
    ("Pay is EUR 4,000 per month.", 4000, 4000, "EUR", "month", "pay", "stated"),
])
def test_direct_text_pay_preserves_currency_period_and_component(text, minimum, maximum, currency, period, kind, status):
    fact = job_facts({"description": text})["pay"]
    assert fact["status"] == status
    value = fact["ranges"][0]
    assert (value["minimum"], value["maximum"], value["currency"], value["period"], value["kind"]) == (minimum, maximum, currency, period, kind)
    if currency is None:
        assert "currency not stated" in fact["summary"] and "USD" not in fact["summary"]


def test_base_range_and_separate_bonus_are_not_combined():
    facts = job_facts({"description": "Base salary USD 100,000-120,000 per year. A USD 10,000 signing bonus may be offered."})
    assert len(facts["pay"]["ranges"]) == 1
    assert facts["pay"]["ranges"][0]["maximum"] == 120000


def test_structured_lever_and_greenhouse_salary_units_are_preserved():
    lever = published_pay({"salaryRange": {"currency": "USD", "interval": "per-hour", "min": 35.5, "max": 40}}, "lever")
    greenhouse = published_pay({"pay_input_ranges": [{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD", "title": "New York salary range"}]}, "greenhouse")
    assert lever[0]["period"] == "hour" and lever[0]["minimum"] == 35.5
    assert greenhouse[0]["minimum"] == 120000 and greenhouse[0]["period"] is None
    assert job_facts({"published_pay": lever})["pay"]["status"] == "stated"
    fact = job_facts({"published_pay": greenhouse})["pay"]
    assert fact["status"] == "unclear" and "period not stated" in fact["summary"]


@pytest.mark.parametrize("value", [None, [], {}, {"min": -1, "max": 100}, {"currency": "USD", "interval": {}, "min": float("nan"), "max": 100},
                                           {"currency": "USD", "interval": "per-year", "min": 150000, "max": 100000}])
def test_malformed_structured_salary_is_not_fabricated(value):
    assert published_pay({"salaryRange": value}, "lever") == []


def test_greenhouse_cents_require_integer_values_and_a_supported_minor_unit():
    ranges = [{"min_cents": 10000000, "max_cents": 12000000, "currency_type": currency} for currency in ("JPY", [], None)]
    ranges += [{"min_cents": 10000000.5, "max_cents": 12000000, "currency_type": "USD"}]
    assert published_pay({"pay_input_ranges": ranges}, "greenhouse") == []


def test_facts_are_bounded_and_deterministic_without_returning_description_boilerplate():
    description = "Qualifications\nBachelor's degree in Computer Science " + "or equivalent practical experience " * 100 + ".\nRequired: 1 year of experience.\n" + "About our wonderful company. " * 10000
    job = {"description": description, "location": "Boston, MA " * 1000,
           "score_detail": {"experience_requirement": {"status": "known", "minimum_months": 12, "maximum_months": None, "evidence": ["Explicit evidence " * 1000] * 1000}}}
    facts = job_facts(job)
    assert facts == job_facts(job)
    for fact in facts.values():
        assert len(fact["summary"]) <= MAX_SUMMARY
        assert len(fact["evidence"]) <= 2
        assert all(len(evidence) <= MAX_EVIDENCE for evidence in fact["evidence"])
    assert len(json.dumps(facts)) < 4000
