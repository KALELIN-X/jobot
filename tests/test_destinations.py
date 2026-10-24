"""Supported URL identities and evidence checks using only synthetic public rows."""
import pytest

from jobradar.destinations import (
    SIMPLIFY_SOURCE_URL, application_key_from_url, listing_binding,
    parse_destination, public_detail_url, verified_destination,
)
from jobradar.approval import job_problems
from jobradar.models import Rules


LEVER = "https://jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc/apply"
GREENHOUSE = "https://job-boards.greenhouse.io/example/jobs/1234567009"


def fetched(url=GREENHOUSE):
    destination = parse_destination(url)
    job = {"id": "synthetic-feed-row", "company": "Example", "title": "New Grad Backend Engineer",
           "location": "Boston, MA", "country_code": "US", "source": "SimplifyJobs",
           "source_url": SIMPLIFY_SOURCE_URL, "detail_url": destination["canonical_url"],
           "apply_url": destination["apply_url"], "ats": destination["ats"], "closed": False,
           "description": "Build Python backend services.", "score": 85,
           "score_detail": {"eligibility": [], "experience_requirement": {"minimum_months": 0}}}
    job["destination_provenance"] = {"kind": "simplify_feed", "source_url": SIMPLIFY_SOURCE_URL,
                                     "application_key": destination["application_key"], "listing_binding": listing_binding(job)}
    job["employer_verified"] = {"source_url": public_detail_url(destination),
                                "application_key": destination["application_key"], "posting_id": destination["posting_id"]}
    return job


@pytest.mark.parametrize("host", ["boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"])
def test_greenhouse_history_key_collapses_official_hosts_but_browser_origin_is_preserved(host):
    url = GREENHOUSE.replace("job-boards.greenhouse.io", host)
    parsed = parse_destination(url + "/?gh_src=tracking")
    assert parsed["application_key"] == "greenhouse:example/1234567009"
    assert parsed["apply_url"] == url
    assert parsed["host"] == host
    assert parsed["posting_id"] == "1234567009"


def test_existing_lever_keys_remain_unchanged_and_regions_stay_distinct():
    assert application_key_from_url(LEVER) == "lever:jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc"
    assert application_key_from_url(LEVER.replace("/apply", "") + "?source=other") == application_key_from_url(LEVER)
    assert application_key_from_url(LEVER.replace("jobs.lever.co", "JOBS.LEVER.CO").replace("example", "EXAMPLE").replace("abcd", "ABCD")) == application_key_from_url(LEVER)
    assert application_key_from_url(LEVER.replace("jobs.lever.co", "jobs.eu.lever.co")) != application_key_from_url(LEVER)


@pytest.mark.parametrize("url", [GREENHOUSE.replace("https:", "http:"), GREENHOUSE.replace("example/jobs", "other/example/jobs"), GREENHOUSE.replace("1234567009", "not-a-number"), GREENHOUSE.replace("1234567009", "01234"), GREENHOUSE.replace("job-boards.greenhouse.io", "job-boards.greenhouse.io.evil.test"), GREENHOUSE.replace("job-boards.greenhouse.io", "user@job-boards.greenhouse.io"), GREENHOUSE.replace("job-boards.greenhouse.io", "job-boards.greenhouse.io:444"), GREENHOUSE.replace("example", "%65xample"), GREENHOUSE + "/../other", GREENHOUSE + "#application", "https://jobs.ashbyhq.com/example/role"])
def test_unsupported_or_ambiguous_routes_do_not_confer_capability(url):
    assert parse_destination(url) is None


def test_fragment_cannot_evade_history_but_does_not_become_a_browser_capability():
    assert application_key_from_url(GREENHOUSE + "#app") == application_key_from_url(GREENHOUSE)
    assert parse_destination(GREENHOUSE + "#app") is None


def test_public_metadata_url_is_derived_not_interpolated_from_caller_fields():
    assert public_detail_url(GREENHOUSE) == "https://boards-api.greenhouse.io/v1/boards/example/jobs/1234567009?content=true&pay_transparency=true"
    assert public_detail_url(LEVER) == "https://api.lever.co/v0/postings/example/12345678-abcd-1234-abcd-123456789abc"
    assert public_detail_url(LEVER.replace("jobs.lever.co", "jobs.eu.lever.co")).startswith("https://api.eu.lever.co/")
    assert public_detail_url({"apply_url": "https://evil.test/role", "board": "example"}) is None


@pytest.mark.parametrize("query", ["content=true", "content=true&pay_transparency=true"])
def test_greenhouse_public_fetch_and_saved_metadata_allow_only_exact_current_or_legacy_query(query):
    from jobradar.ingest import _allowed_fetch_url
    job = fetched()
    endpoint = "https://boards-api.greenhouse.io/v1/boards/example/jobs/1234567009?" + query
    assert _allowed_fetch_url(endpoint)
    job["employer_verified"]["source_url"] = endpoint
    assert verified_destination(job)


@pytest.mark.parametrize("query", ["", "content=false", "pay_transparency=true", "content=true&pay_transparency=false",
                                 "pay_transparency=true&content=true", "content=true&pay_transparency=true&extra=1",
                                 "content=true&pay_transparency=true&content=false", "content=true&pay_transparency=true&",
                                 "content=true&pay%5Ftransparency=true", "content=true&pay_transparency=TRUE"])
def test_greenhouse_public_fetch_and_saved_metadata_reject_unapproved_query_variants(query):
    from jobradar.ingest import _allowed_fetch_url
    endpoint = "https://boards-api.greenhouse.io/v1/boards/example/jobs/1234567009?" + query
    assert not _allowed_fetch_url(endpoint)
    job = fetched()
    job["employer_verified"]["source_url"] = endpoint
    assert not verified_destination(job)


@pytest.mark.parametrize("change", [lambda url: url.replace("boards-api.greenhouse.io", "boards-api.greenhouse.io.evil.test"),
                                    lambda url: url.replace("example/jobs", "other/jobs"),
                                    lambda url: url.replace("1234567009", "1234567010"),
                                    lambda url: url.replace("https:", "http:"),
                                    lambda url: url + "#private"])
@pytest.mark.parametrize("legacy", [False, True])
def test_optional_public_pay_query_does_not_widen_bound_posting_or_origin(change, legacy):
    job = fetched()
    endpoint = public_detail_url(GREENHOUSE)
    if legacy:
        endpoint = endpoint.removesuffix("&pay_transparency=true")
    job["employer_verified"]["source_url"] = change(endpoint)
    assert not verified_destination(job)


@pytest.mark.parametrize("url", [LEVER, GREENHOUSE])
def test_bound_source_row_and_individual_employer_metadata_can_pass_approval(url):
    job = fetched(url)
    assert verified_destination(job)
    assert job_problems(job, Rules(max_required_experience_months=12).model_dump()) == []


@pytest.mark.parametrize("field,value", [("source", "pasted URL"), ("source_url", "https://evil.test/README.md"), ("company", "Another employer"), ("title", "Different role"), ("location", "Different location"), ("destination_provenance", None), ("employer_verified", None), ("detail_url", "https://job-boards.greenhouse.io/other/jobs/1234567009"), ("apply_url", "https://job-boards.greenhouse.io/example/jobs/99999"), ("ats", "lever")])
def test_missing_changed_or_unbound_source_evidence_fails_closed(field, value):
    job = fetched()
    job[field] = value
    assert not verified_destination(job)
    assert job_problems(job, Rules().model_dump())


@pytest.mark.parametrize("proof_field", ["source_url", "application_key", "posting_id"])
def test_employer_proof_must_match_exact_endpoint_and_posting(proof_field):
    job = fetched()
    job["employer_verified"][proof_field] = "unrelated"
    assert not verified_destination(job)


def test_supported_system_can_be_disabled_and_closed_postings_cannot_run():
    assert any("Greenhouse applications are disabled" in problem for problem in job_problems(fetched(), Rules(allowed_ats=["lever"]).model_dump()))
    closed = fetched()
    closed["closed"] = True
    assert any("closed" in problem for problem in job_problems(closed, Rules().model_dump()))


def test_new_ats_defaults_do_not_widen_explicitly_saved_lever_only_rules():
    assert Rules().allowed_ats == ["lever", "greenhouse", "ashby", "smartrecruiters", "workday"]
    assert Rules(allowed_ats=["lever"]).allowed_ats == ["lever"]
    assert Rules(allowed_ats=["greenhouse"]).allowed_ats == ["greenhouse"]
    assert Rules(allowed_ats=[]).allowed_ats == []


@pytest.mark.parametrize("systems", [["icims"], ["taleo"], ["lever", "lever"], ["greenhouse", "unknown"]])
def test_unsupported_or_duplicate_ats_rules_are_rejected(systems):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Rules(allowed_ats=systems)
