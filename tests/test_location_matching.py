import pytest

from jobradar.locations import country_is_us, location_matches
from jobradar.ranking import rank_job


@pytest.mark.parametrize("location", ["Boston, MA", "San Francisco, CA", "Santa Clara, California", "New York, NY", "Remote - United States", "Austin, TX 78701"])
def test_us_preference_accepts_qualified_us_locations(location):
    assert location_matches({"location": location}, ["United States"])


@pytest.mark.parametrize("location", ["Remote", "Toronto, CA", "Berlin, DE", "London, United Kingdom", "Vancouver, Canada", "Georgia"])
def test_us_preference_does_not_turn_foreign_or_unknown_remote_into_us(location):
    assert not location_matches({"location": location, "work_model": "Remote"}, ["United States"])


@pytest.mark.parametrize("location", [
    "London, United Kingdom (US hours)", "Toronto, Canada - US time zones",
    "Remote - US business hours", "US time-zone overlap", "Remote supporting United States customers",
    "Remote - US-based company", "London, UK (United States working hours)",
    "EMEA remote, US core hours", "APAC remote, US time zones",
])
def test_hours_customers_and_foreign_regions_do_not_establish_us_location(location):
    job = {"location": location, "work_model": "Remote"}
    assert not country_is_us(job)
    assert not location_matches(job, ["United States"])


@pytest.mark.parametrize("location", [
    "Boston, MA; Seattle, WA", "Boston, MA (US hours)", "Seattle, WA - Hybrid",
    "London, United Kingdom; Boston, MA", "Toronto, Canada / Remote - United States",
    "Remote - United States (US business hours)", "Remote US", "United States or Canada",
    "Boston, MA (UK hours)", "Remote - United States supporting Canada customers",
])
def test_separate_us_locations_and_us_remote_remain_eligible(location):
    assert country_is_us({"location": location})
    assert location_matches({"location": location}, ["United States"])


@pytest.mark.parametrize("country", ["GB", "CA", "DE", "IN", "gb"])
def test_explicit_foreign_country_code_overrides_us_references_and_city_text(country):
    job = {"location": "Boston, MA; Remote - United States", "country_code": country}
    assert not country_is_us(job)
    assert not location_matches(job, ["US"])


def test_structured_country_evidence_and_custom_locations_stay_distinct():
    assert country_is_us({"location": "Santa Clara HQ", "country_code": "US"})
    assert not country_is_us({"location": "Ontario, CA", "country_code": "CA"})
    assert not location_matches({"location": "Boston, MA", "country_code": "US"}, ["Chicago"])
    assert location_matches({"location": "Remote - Canada", "work_model": "Remote"}, ["Remote"])
    assert not location_matches({"location": "Canada"}, ["US"])


def test_country_wide_work_permission_is_not_a_city_specific_permission():
    job = {"title":"Software Engineer", "company":"Example", "location":"Boston, MA", "ats":"lever", "closed":False,
           "description":"Python required. No prior experience required."}
    profile = {"approved_work_locations":["United States"],"experience_months":12,"work_authorized":True,"needs_sponsorship":False}
    result = rank_job(job, profile, "Built Python services", {"locations":["United States"]})
    entries = {entry["label"]:entry for entry in result["eligibility"]}
    assert entries["Approved location"]["status"] == "pass"
    assert entries["Work location"]["status"] == "pass"
    profile["approved_work_locations"] = ["Chicago"]
    result = rank_job(job, profile, "Built Python services", {"locations":["United States"]})
    assert next(entry for entry in result["eligibility"] if entry["label"] == "Work location")["status"] == "unknown"
