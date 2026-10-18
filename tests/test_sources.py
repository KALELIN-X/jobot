"""Direct public-source ingestion with synthetic HTTP responses only."""
import asyncio
import json

import httpx
import pytest

from jobradar import ingest, sources
from jobradar.destinations import SIMPLIFY_SOURCE_URL, parse_destination, public_detail_url, verified_destination


LEVER = "https://jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc"
GREENHOUSE = "https://job-boards.greenhouse.io/example/jobs/1234567"
DESCRIPTION = "Build reliable Python backend services and REST APIs. No professional experience is required. " * 3


def row(url=GREENHOUSE, company="Example", title="Software Engineer I", location="Boston, MA", age="1d", application=None):
    application = application if application is not None else f'<a href="{url}"><img alt="Apply"></a><a href="https://simplify.jobs/p/other"><img alt="Simplify"></a>'
    return f"<tr><td>{company}</td><td>{title}</td><td>{location}</td><td>{application}</td><td>{age}</td></tr>"


def document(*rows):
    return "## 💻 Software Engineering New Grad Roles\n<table>" + "".join(rows) + "</table>\n## Product Management New Grad Roles\n<table>" + row(title="Product Manager") + "</table>"


def posting(url=GREENHOUSE):
    return sources.parse_simplify_readme(document(row(url)), 100)[0]


def employer_payload(url=GREENHOUSE, **changes):
    parsed = parse_destination(url)
    if parsed["ats"] == "lever":
        payload = {"id": parsed["posting_id"], "text": "Software Engineer I", "applyUrl": parsed["apply_url"],
                   "categories": {"location": "Boston, MA"}, "descriptionPlain": DESCRIPTION}
    else:
        payload = {"id": int(parsed["posting_id"]), "title": "Software Engineer I", "absolute_url": url,
                   "location": {"name": "Boston, Massachusetts, United States"}, "content": "&lt;p&gt;" + DESCRIPTION + "&lt;/p&gt;"}
    return {**payload, **changes}


def test_parser_preserves_direct_links_and_software_category_without_guessing_boards():
    jobs = sources.parse_simplify_readme(document(row()), 100)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["source"] == "SimplifyJobs"
    assert job["source_url"] == SIMPLIFY_SOURCE_URL
    assert job["apply_url"] == GREENHOUSE
    assert job["country_code"] == "US"
    assert job["source_new_grad"] is True
    assert job["source_category"] == "Software Engineering New Grad Roles"
    assert job["description"] == ""
    assert job["employer_verified"] is None
    assert not verified_destination(job)
    assert "experience_requirement" not in job


def test_supported_rows_have_priority_and_aliases_deduplicate_by_posting_identity():
    jobs = sources.parse_simplify_readme(document(
        row("https://careers.example.test/jobs/one", company="Manual"),
        row(LEVER + "?utm_source=one", company="Lever"),
        row(GREENHOUSE + "?ref=one", company="Greenhouse"),
        row(GREENHOUSE.replace("job-boards", "boards") + "?ref=another", company="↳"),
    ), 2)
    assert [job["ats"] for job in jobs] == ["lever", "greenhouse"]
    first = sources.parse_simplify_readme(document(row(GREENHOUSE)))[0]
    alias = sources.parse_simplify_readme(document(row(GREENHOUSE.replace("job-boards", "boards") + "?utm_source=other")))[0]
    assert first["id"] == alias["id"]


def test_company_carry_forward_and_multilocation_markup_are_preserved():
    jobs = sources.parse_simplify_readme(document(
        row(GREENHOUSE, company="🔥 Example", location="<details><summary>2 locations</summary>Boston, MA</br>Seattle, WA</details>"),
        row(LEVER, company="↳", location="Remote in USA"),
    ))
    assert len(jobs) == 2
    assert all(job["company"] == "Example" for job in jobs)
    assert jobs[0]["location"] == "Boston, MA; Seattle, WA"
    assert jobs[1]["work_model"] == "Remote"


@pytest.mark.parametrize("location", ["Remote", "Remote in Canada", "Toronto, ON", "London, UK", "Remote in CA", "Berlin, DE", "Toronto, CA",
                                      "London, United Kingdom (US hours)", "Toronto, Canada - US time zones", "Remote - US business hours"])
def test_ambiguous_or_foreign_locations_do_not_become_us(location):
    assert sources.parse_simplify_readme(document(row(location=location))) == []


@pytest.mark.parametrize("location", ["United States", "Remote in USA", "Remote US", "NYC", "SF", "Seattle, WA", "Austin, Texas"])
def test_positive_us_location_evidence_is_accepted(location):
    assert len(sources.parse_simplify_readme(document(row(location=location)))) == 1


def test_closed_missing_ambiguous_and_private_application_links_are_skipped():
    ambiguous = '<a href="https://jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc/apply">Apply</a><a href="https://job-boards.greenhouse.io/example/jobs/1234567">Apply</a>'
    assert sources.parse_simplify_readme(document(
        row(application="🔒"), row(application="No employer link"), row(application=ambiguous),
        row(url="https://127.0.0.1/jobs/one"),
    )) == []


@pytest.mark.parametrize("limit,expected", [(-5, 0), (0, 0), (1, 1), (100, 100), (500, 100)])
def test_parser_has_a_hard_hundred_result_limit(limit, expected):
    rows = [row(url=f"https://job-boards.greenhouse.io/example/jobs/{index+1}") for index in range(120)]
    assert len(sources.parse_simplify_readme(document(*rows), limit)) == expected


@pytest.mark.parametrize("url", [GREENHOUSE, LEVER])
def test_official_metadata_binds_exact_posting_and_provides_the_description(url):
    job = posting(url)
    result = sources.employer_metadata(job, employer_payload(url))
    assert verified_destination(result)
    assert result["closed"] is False
    assert result["description"].startswith("Build reliable Python")
    assert "<p>" not in result["description"]
    assert result["employer_verified"]["source_url"] == public_detail_url(url)
    assert job["employer_verified"] is None


@pytest.mark.parametrize("changes", [{"id": 999}, {"absolute_url": GREENHOUSE.replace("1234567", "9999999")}, {"content": "short"}])
def test_official_metadata_rejects_wrong_posting_changed_destination_and_missing_description(changes):
    with pytest.raises(ValueError):
        sources.employer_metadata(posting(), employer_payload(**changes))


def test_official_non_us_location_overrides_older_feed_country_evidence():
    result = sources.employer_metadata(posting(), employer_payload(location={"name": "Toronto, Canada"}))
    assert result["country_code"] is None
    assert "does not establish a US location" in result["location_evidence"]


@pytest.mark.parametrize("location", ["London, United Kingdom (US hours)", "Toronto, Canada - US time zones", "EMEA - US hours"])
def test_official_timezone_annotations_cannot_keep_a_stale_us_country_stamp(location):
    result = sources.employer_metadata(posting(), employer_payload(location={"name": location}))
    assert result["country_code"] is None
    assert not sources.us_location(result["location"])


def test_official_lever_preserves_bounded_optional_pay_and_explicit_work_model():
    from jobradar.job_facts import job_facts
    result = sources.employer_metadata(posting(LEVER), employer_payload(LEVER,
        salaryRange={"currency": "USD", "interval": "per-hour", "min": 40.5, "max": 50.75},
        salaryDescriptionPlain="Hourly base pay, with equity considered separately. " * 200,
        workplaceType="hybrid"))
    assert result["published_pay"][0]["period"] == "hour"
    assert result["published_pay"][0]["minimum"] == 40.5
    assert len(result["pay_description"]) == 4000
    assert result["work_model"] == "hybrid"
    assert result["description"] == DESCRIPTION.strip()
    assert job_facts(result)["pay"]["status"] == "stated"


def test_official_greenhouse_preserves_returned_pay_cents_without_inventing_period():
    from jobradar.job_facts import job_facts
    result = sources.employer_metadata(posting(), employer_payload(pay_input_ranges=[
        {"min_cents": 10000000, "max_cents": 12000000, "currency_type": "USD", "title": "Boston salary range"}]))
    assert result["published_pay"][0]["minimum"] == 100000
    assert result["published_pay"][0]["period"] is None
    assert job_facts(result)["pay"]["status"] == "unclear"


@pytest.mark.parametrize("url", [GREENHOUSE, LEVER])
def test_removed_official_pay_cannot_reuse_saved_pay_metadata(url):
    job = posting(url)
    job.update(published_pay=[{"minimum": 100000, "maximum": 120000, "currency": "USD", "period": "year"}], pay_description="Old salary range")
    result = sources.employer_metadata(job, employer_payload(url))
    assert result["published_pay"] == [] and result["pay_description"] == ""
    assert job["published_pay"] and job["pay_description"] == "Old salary range"


def test_successful_and_failed_employer_descriptions_are_independent(monkeypatch):
    requested = []
    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == SIMPLIFY_SOURCE_URL:
            return httpx.Response(200, text=document(row(GREENHOUSE), row(LEVER)))
        if "greenhouse" in str(request.url):
            return httpx.Response(200, json=employer_payload())
        return httpx.Response(403)
    monkeypatch.setattr(ingest, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    jobs = asyncio.run(ingest.fetch_jobs(100))
    assert len(jobs) == 2
    assert verified_destination(jobs[0])
    assert not verified_destination(jobs[1])
    assert jobs[1]["closed"] is None
    assert "403" in jobs[1]["fetch_error"]
    assert len(requested) == 3


@pytest.mark.parametrize("status,closed", [(404, True), (410, True), (429, None), (503, None)])
def test_only_explicit_not_found_marks_an_employer_posting_closed(monkeypatch, status, closed):
    monkeypatch.setattr(ingest, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))))
    job = asyncio.run(sources.enrich_employer_details([posting()]))[0]
    assert job["closed"] is closed
    assert job["employer_verified"] is None


def test_budget_cancels_and_joins_all_requests_while_retaining_metadata(monkeypatch):
    active, concurrency = set(), []
    async def slow_fetch(client, url, max_bytes):
        active.add(url)
        concurrency.append(len(active))
        try:
            await asyncio.Event().wait()
        finally:
            active.discard(url)
    monkeypatch.setattr(ingest, "_fetch_text", slow_fetch)
    monkeypatch.setattr(sources, "DETAIL_BUDGET_SECONDS", 0.02)
    jobs = sources.parse_simplify_readme(document(*[row(url=GREENHOUSE.replace("1234567", str(index+1))) for index in range(6)]))
    result = asyncio.run(sources.enrich_employer_details(jobs))
    assert len(result) == 6
    assert max(concurrency) == 3
    assert not active
    assert all("budget" in job["fetch_error"] for job in result)
    assert all(job["employer_verified"] is None for job in result)


def test_primary_source_failure_uses_bounded_us_fallback(monkeypatch):
    async def broken_source(*args):
        raise ValueError("Source response format changed")
    async def fallback(limit):
        assert limit == 100
        return [{"id": "us", "location": "Boston, MA", "apply_url": None},
                {"id": "foreign", "location": "Toronto, ON", "apply_url": None}]
    monkeypatch.setattr(ingest, "_fetch_text", broken_source)
    monkeypatch.setattr(ingest, "fetch_jobright_jobs", fallback)
    jobs = asyncio.run(sources.fetch_public_jobs(500))
    assert [job["id"] for job in jobs] == ["us"]
    assert jobs[0]["country_code"] == "US"
    assert "unavailable" in jobs[0]["discovery_note"]


def test_public_api_redirect_cannot_change_posting_or_board():
    endpoint = public_detail_url(GREENHOUSE)
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": endpoint.replace("1234567", "9999999")})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="redirect"):
                await ingest._fetch_text(client, endpoint, 1000)
    asyncio.run(run())
    assert requested == [endpoint]


@pytest.mark.parametrize("url", [
    "https://boards-api.greenhouse.io/v1/boards/example/jobs/1234567?content=true&other=1",
    "https://boards-api.greenhouse.io.evil.test/v1/boards/example/jobs/1234567?content=true",
    "https://api.lever.co/v0/postings/example/anything",
    "https://api.lever.co/v0/postings/example/12345678-abcd-1234-abcd-123456789abc?key=secret",
    "https://raw.githubusercontent.com/other/repo/main/jobs.json",
])
def test_unapproved_public_fetch_routes_are_rejected(url):
    assert not ingest._allowed_fetch_url(url)
