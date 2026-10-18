import asyncio
import json
from datetime import date

import httpx
import pytest

from jobradar import ingest


FIRST = "https://jobright.ai/jobs/info/12345678abcdef"
SECOND = "https://jobright.ai/jobs/info/abcdefgh123456"
MARKDOWN = f"""# Public table
| Company | Job Title | Location | Work Model | Date Posted |
| --- | --- | --- | --- | --- |
| **[Acme](https://acme.example)** | **[Engineer]({FIRST}?utm_source=repo)** | Boston, MA, United States | On Site | Oct 17 |
| ↳ | **[Backend Engineer]({SECOND})** | Remote, United States | Remote | Oct 16 |
| ↳ | **[Duplicate]({FIRST}?different=tracking)** | Boston | On Site | Oct 15 |
| Bad | **[Attack](https://jobright.ai.evil.example/jobs/info/12345678)** | Boston | On Site | Oct 15 |
"""


def document(**overrides):
    value = {"jobId": "12345678abcdef", "jobSummary": "Build reliable backend systems for our customers.",
             "coreResponsibilities": ["Develop Java and Python REST APIs with PostgreSQL and AWS."],
             "qualifications": {"mustHave": ["Java proficiency required", "2+ years of software development experience"], "preferredHave": ["Kafka preferred"]},
             "detailQualifications": {"mustHave": {"hardSkill": [{"skill": "Java"}]}}, "isDeleted": False}
    value.update(overrides)
    payload = {"props": {"pageProps": {"dataSource": {"jobResult": value}}}}
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script></html>"


def test_markdown_canonical_dedup_arrow_and_stable_id():
    jobs = ingest.parse_markdown(MARKDOWN)
    assert len(jobs) == 2
    assert [job["company"] for job in jobs] == ["Acme", "Acme"]
    assert jobs[0]["title"] == "Engineer"
    assert jobs[0]["detail_url"] == FIRST
    assert jobs[0]["source_url"] == ingest.SOURCE_URL
    assert jobs[0]["id"] == ingest.parse_markdown(MARKDOWN.replace("utm_source=repo", "utm_source=another"))[0]["id"]
    assert len(jobs[0]["id"]) == 64
    assert len(ingest.parse_markdown(MARKDOWN, 1)) == 1
    assert ingest.parse_markdown(MARKDOWN, 0) == []


def test_posted_dates_use_current_year_and_handle_new_year_rollover():
    assert ingest.normalize_posted_date("Oct 18", date(2026, 10, 18)) == "2026-10-18"
    assert ingest.normalize_posted_date("Dec 30", date(2027, 1, 2)) == "2026-12-30"
    assert ingest.normalize_posted_date("Jan 1", date(2027, 1, 2)) == "2027-01-01"
    assert ingest.normalize_posted_date("Yesterday", date(2027, 1, 1)) == "2026-12-31"
    assert ingest.normalize_posted_date("unknown", date(2026, 10, 18)) is None


def test_malformed_employer_website_keeps_plain_company_label():
    markdown = MARKDOWN.replace("[Acme](https://acme.example)", "[Metropolitan Transportation Authority](https:/mta.info/)")
    jobs = ingest.parse_markdown(markdown)
    assert jobs[0]["company"] == "Metropolitan Transportation Authority"
    assert jobs[1]["company"] == "Metropolitan Transportation Authority"


@pytest.mark.parametrize("url", ["http://jobright.ai/jobs/info/12345678", "https://jobright.ai@evil.example/jobs/info/12345678", "https://jobright.ai:8443/jobs/info/12345678", "https://127.0.0.1/jobs/info/12345678", "https://jobright.ai/jobs/info/%2e%2e/admin", "https://jobright.ai/jobs/info/12345678/else"])
def test_detail_url_rejects_non_public_or_non_detail(url):
    assert ingest.canonical_detail_url(url) is None


def test_public_job_data_is_description_not_inferred_apply_link():
    detail = ingest.parse_detail(document(), FIRST)
    assert "Java and Python" in detail["description"]
    assert detail["required_skills"] == ["Java"]
    assert detail["closed"] is False
    assert detail["apply_url"] is None
    assert "application URL" in detail["fetch_error"]


def test_actual_apply_link_and_jsonld():
    detail = ingest.parse_detail(document(applyUrl="https://jobs.lever.co/acme/role?utm_source=jr"), FIRST)
    assert detail["apply_url"] == "https://jobs.lever.co/acme/role"
    assert detail["ats"] == "lever"
    assert detail["fetch_error"] is None
    structured = {"@type": "JobPosting", "description": "<p>" + "Python APIs and distributed systems. " * 8 + "</p>", "applicationUrl": "https://jobs.ashbyhq.com/acme/role"}
    parsed = ingest.parse_detail('<script type="application/ld+json">' + json.dumps(structured) + '</script>')
    assert parsed["ats"] == "ashby"
    assert "<p>" not in parsed["description"]


def test_does_not_extract_recommended_role_or_fake_blocked_page():
    wrong_role = ingest.parse_detail(document(jobId="different-job-id"), FIRST)
    assert wrong_role["description"] == ""
    blocked = ingest.parse_detail("<html>Access denied. Verify you are human. Requirements " + "Python " * 50 + "</html>", FIRST)
    assert blocked["description"] == ""
    assert blocked["closed"] is None


def test_broken_script_can_use_readable_description():
    text = '<script type="application/json">{broken</script><main>Responsibilities <p>' + "Build Python APIs and operate production software. " * 6 + '</p><a href="https://boards.greenhouse.io/acme/jobs/42">Apply now</a></main>'
    detail = ingest.parse_detail(text, FIRST)
    assert detail["description_source"] == "Public Jobright readable text"
    assert detail["ats"] == "greenhouse"


@pytest.mark.parametrize("url", ["https://jobs.lever.co.evil.example/acme/role", "https://localhost/private", "https://127.0.0.1/private", "https://10.1.1.1/private", "https://jobs.lever.co@evil.example/acme/role"])
def test_no_ats_suffix_confusion_or_private_application_links(url):
    assert ingest.classify_ats(url) == "unsupported"
    if "localhost" in url or "127.0.0.1" in url or "10.1.1.1" in url:
        assert ingest._public_application_url(url) is None


def test_http_redirect_cannot_escape_allowlist():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="Blocked redirect"):
                await ingest._fetch_text(client, FIRST, 1000)
    asyncio.run(run())
    assert requests == [FIRST]


def test_streaming_byte_limit_even_without_content_length():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 101))) as client:
            with pytest.raises(ValueError, match="byte limit"):
                await ingest._fetch_text(client, FIRST, 100)
    asyncio.run(run())


def test_failed_detail_keeps_metadata_and_unknown_availability(monkeypatch):
    def handler(request):
        if str(request.url) == ingest.SOURCE_URL:
            return httpx.Response(200, text=MARKDOWN)
        return httpx.Response(403)
    monkeypatch.setattr(ingest, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    jobs = asyncio.run(ingest.fetch_jobs())
    assert len(jobs) == 2
    assert jobs[0]["title"] == "Engineer"
    assert jobs[0]["closed"] is None
    assert "403" in jobs[0]["fetch_error"]
    assert jobs[0]["apply_url"] is None


def test_total_detail_budget_retains_all_metadata_and_joins_cancelled_tasks(monkeypatch):
    active = set()
    cancelled = []
    concurrency = []
    markdown = MARKDOWN + "".join(
        f"| ↳ | [Queued Engineer](https://jobright.ai/jobs/info/budgetdetail{index:02d}) | Boston, MA | On Site | Oct 17 |\n"
        for index in range(3)
    )

    async def slow_fetch(client, url, max_bytes):
        if url == ingest.SOURCE_URL:
            return markdown
        active.add(url)
        concurrency.append(len(active))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(url)
            raise
        finally:
            active.discard(url)

    monkeypatch.setattr(ingest, "_fetch_text", slow_fetch)
    monkeypatch.setattr(ingest, "DETAIL_RESOLUTION_BUDGET_SECONDS", 0.02)
    monkeypatch.setattr(ingest, "DETAIL_TIMEOUT_SECONDS", 1)
    jobs = asyncio.run(ingest.fetch_jobs())
    assert [job["title"] for job in jobs] == ["Engineer", "Backend Engineer"] + ["Queued Engineer"] * 3
    assert all("budget" in job["fetch_error"] for job in jobs)
    assert all(job["closed"] is None and job["description"] == "" for job in jobs)
    assert len(cancelled) == 3
    assert {FIRST, SECOND}.issubset(cancelled)
    assert max(concurrency) == 3
    assert not active


def test_per_detail_timeout_preserves_successful_description(monkeypatch):
    async def mixed_fetch(client, url, max_bytes):
        if url == ingest.SOURCE_URL:
            return MARKDOWN
        if url == FIRST:
            return document()
        await asyncio.Event().wait()

    monkeypatch.setattr(ingest, "_fetch_text", mixed_fetch)
    monkeypatch.setattr(ingest, "DETAIL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(ingest, "DETAIL_RESOLUTION_BUDGET_SECONDS", 1)
    jobs = asyncio.run(ingest.fetch_jobs())
    assert "Java and Python" in jobs[0]["description"]
    assert "request exceeded" in jobs[1]["fetch_error"]
    assert jobs[1]["closed"] is None


def test_source_has_total_read_deadline_and_does_not_start_details(monkeypatch):
    requested = []

    async def slow_source(client, url, max_bytes):
        requested.append(url)
        await asyncio.Event().wait()

    monkeypatch.setattr(ingest, "_fetch_text", slow_source)
    monkeypatch.setattr(ingest, "SOURCE_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="repository read timed out"):
        asyncio.run(ingest.fetch_jobs())
    assert requested == [ingest.SOURCE_URL]


def posting(title="Engineer", location="Boston, MA", role="role", board="acme"):
    return {"text": title, "categories": {"location": location},
            "applyUrl": f"https://jobs.lever.co/{board}/{role}/apply",
            "descriptionPlain": "Build Python APIs and production backend services. " * 4}


def test_lever_matching_requires_unique_title_location_and_correct_tenant():
    job = ingest.parse_markdown(MARKDOWN)[0]
    exact = posting()
    assert ingest.match_lever_posting(job, [exact, posting(location="Austin, TX", role="other")], "acme") == exact
    assert ingest.match_lever_posting(job, [exact, posting(role="duplicate")], "acme") is None
    assert ingest.match_lever_posting(job, [posting(title="Senior Engineer")], "acme") is None
    assert ingest.match_lever_posting(job, [posting(board="other")], "acme") is None
    assert ingest.match_lever_posting(job, [posting(location="Austin, TX")], "acme") is None
    assert ingest.match_lever_posting({**job, "title": "Engineer C++"}, [posting(title="Engineer C#")], "acme") is None


def test_explicit_lever_enrichment_only_fetches_configured_board(monkeypatch):
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=[posting()])
    monkeypatch.setattr(ingest, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    jobs = ingest.parse_markdown(MARKDOWN)
    enriched = asyncio.run(ingest.enrich_lever_jobs(jobs, {"Acme": "acme", "Untrusted": "../../admin"}))
    assert requests == ["https://api.lever.co/v0/postings/acme?mode=json"]
    assert enriched[0]["ats"] == "lever"
    assert enriched[0]["description_source"] == "Official Lever public board"
    assert enriched[0]["closed"] is False
    assert enriched[1]["apply_url"] is None
    assert "No unique" in enriched[1]["lever_resolution"]
