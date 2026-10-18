"""Candidate refresh uses synthetic HTTP only, never live history or browsers."""
import asyncio
import copy

import httpx
import pytest

from jobradar import ingest, refresh
from jobradar.destinations import JOBRIGHT_SOURCE_URL, public_detail_url, verified_destination
from test_destinations import GREENHOUSE, LEVER, fetched


DESCRIPTION = "Build Python backend services and SQL databases. Required: 2 years of professional software engineering experience. " * 2


def payload(job, **changes):
    from jobradar.destinations import parse_destination
    destination = parse_destination(job["apply_url"])
    if destination["ats"] == "lever":
        data = {"id": destination["posting_id"], "text": job["title"], "applyUrl": destination["apply_url"],
                "hostedUrl": destination["canonical_url"], "categories": {"location": job["location"]},
                "descriptionPlain": DESCRIPTION}
    else:
        data = {"id": int(destination["posting_id"]), "title": job["title"], "absolute_url": destination["canonical_url"],
                "location": {"name": job["location"]}, "content": DESCRIPTION}
    return {**data, **changes}


def install_http(monkeypatch, handler):
    calls = []
    def handle(request):
        calls.append((request.method, str(request.url)))
        return handler(request)
    monkeypatch.setattr(ingest, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    return calls


@pytest.mark.parametrize("url", [GREENHOUSE, LEVER])
def test_one_official_get_refreshes_description_but_never_reuses_stale_requirements(monkeypatch, url):
    job = fetched(url)
    job.update(requirements=["Old cached requirement"], confirmation="Preserve synthetic history", status="new")
    snapshot = copy.deepcopy(job)
    calls = install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert calls == [("GET", public_detail_url(url))]
    assert result["candidate_refresh"]["status"] == "verified"
    assert result["candidate_refresh"]["checked_at"]
    assert result["description"] == DESCRIPTION.strip()
    assert result["requirements"] == result["required_skills"] == result["preferred_skills"] == []
    assert result["score"] is None  # Caller must rerank the refreshed requirements.
    assert verified_destination(result)
    assert result["confirmation"] == "Preserve synthetic history"
    assert job == snapshot


def test_legacy_jobright_uses_official_get_without_fabricating_simplify_source_evidence(monkeypatch):
    job = fetched(LEVER)
    job.update(source="Jobright", source_url=JOBRIGHT_SOURCE_URL, detail_url="https://jobright.ai/jobs/info/synthetic-role-1234")
    job["destination_provenance"] = {"kind": "jobright_detail", "source_url": job["detail_url"],
                                     "application_key": job["employer_verified"]["application_key"]}
    original_provenance = copy.deepcopy(job["destination_provenance"])
    assert verified_destination(job)
    calls = install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert len(calls) == 1
    assert result["candidate_refresh"]["status"] == "verified"
    assert result["source"] == "Jobright" and result["source_url"] == JOBRIGHT_SOURCE_URL
    assert result["detail_url"] == job["detail_url"]
    assert result["destination_provenance"] == original_provenance
    assert verified_destination(result)


@pytest.mark.parametrize("status_code", [404, 410, 429, 500, 301])
def test_missing_unavailable_or_redirected_posting_cannot_reuse_cached_evidence(monkeypatch, status_code):
    job = fetched()
    calls = install_http(monkeypatch, lambda request: httpx.Response(status_code, headers={"Location": public_detail_url(GREENHOUSE)}))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert len(calls) == 1  # Even a redirect to itself is not retried.
    assert result["candidate_refresh"]["status"] == ("closed" if status_code in {404, 410} else "unavailable")
    assert result["closed"] is (True if status_code in {404, 410} else None)
    assert result["employer_verified"] is result["destination_provenance"] is None
    assert result["description"] == "" and result["score"] is None
    assert not verified_destination(result)


@pytest.mark.parametrize("changed", [{"title": "Senior Software Engineer"}, {"location": {"name": "Toronto, Canada"}}])
def test_title_or_location_changes_are_held_for_source_review(monkeypatch, changed):
    job = fetched()
    calls = install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job, **changed)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert len(calls) == 1
    assert result["candidate_refresh"]["status"] == "changed"
    assert not verified_destination(result)


@pytest.mark.parametrize("changed", [{"id": 9876543}, {"content": "too short"}, {"closed": True}])
def test_mismatched_or_explicitly_closed_metadata_fails_closed(monkeypatch, changed):
    job = fetched()
    install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job, **changed)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert result["candidate_refresh"]["status"] != "verified"
    assert result["employer_verified"] is None


def test_unverified_input_never_makes_a_request(monkeypatch):
    job = fetched()
    job["destination_provenance"] = None
    calls = install_http(monkeypatch, lambda request: pytest.fail("An unverified posting must not make a request"))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert calls == []
    assert result["candidate_refresh"]["status"] == "unverified"


@pytest.mark.parametrize("status_code", [200, 404, 500])
def test_current_or_unavailable_refresh_clears_stale_public_pay(monkeypatch, status_code):
    job = fetched()
    job.update(published_pay=[{"minimum": 100000, "maximum": 120000, "currency": "USD", "period": "year"}],
               pay_description="Old published salary description")
    original = copy.deepcopy(job)
    calls = install_http(monkeypatch, lambda request: httpx.Response(status_code, json=payload(job)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert calls == [("GET", public_detail_url(job["apply_url"]))]
    assert result["published_pay"] == [] and result["pay_description"] == ""
    assert job == original


def test_total_timeout_is_bounded_and_cached_capability_is_removed(monkeypatch):
    async def slow_read(endpoint):
        await asyncio.sleep(1)
        return {}
    monkeypatch.setattr(refresh, "_read_official_posting", slow_read)
    monkeypatch.setattr(refresh, "REFRESH_TIMEOUT_SECONDS", 0.01)
    result = asyncio.run(refresh.refresh_candidate(fetched()))
    assert result["candidate_refresh"]["status"] == "unavailable"
    assert result["employer_verified"] is None


def test_oversized_declared_payload_is_rejected(monkeypatch):
    calls = install_http(monkeypatch, lambda request: httpx.Response(200, content=b"{}", headers={"Content-Length": str(refresh.sources.MAX_EMPLOYER_BYTES + 1)}))
    result = asyncio.run(refresh.refresh_candidate(fetched()))
    assert len(calls) == 1
    assert result["candidate_refresh"]["status"] == "unavailable"
    assert not verified_destination(result)


@pytest.mark.parametrize("url,changes", [(GREENHOUSE, {"location": {}}), (LEVER, {"categories": {}})])
def test_missing_current_location_never_reuses_the_old_us_location(monkeypatch, url, changes):
    job = fetched(url)
    install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job, **changes)))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert result["candidate_refresh"]["status"] == "unavailable"
    assert result["employer_verified"] is None
    assert not verified_destination(result)


def test_case_and_whitespace_only_title_changes_do_not_create_a_different_role(monkeypatch):
    job = fetched()
    install_http(monkeypatch, lambda request: httpx.Response(200, json=payload(job, title=" NEW GRAD  BACKEND ENGINEER ")))
    result = asyncio.run(refresh.refresh_candidate(job))
    assert result["candidate_refresh"]["status"] == "verified"
    assert verified_destination(result)


def test_streamed_size_bound_applies_without_a_content_length_header(monkeypatch):
    def oversized_response(request):
        response = httpx.Response(200, content=b"x" * (refresh.sources.MAX_EMPLOYER_BYTES + 1))
        response.headers.pop("content-length", None)
        return response
    calls = install_http(monkeypatch, oversized_response)
    result = asyncio.run(refresh.refresh_candidate(fetched()))
    assert len(calls) == 1
    assert result["candidate_refresh"]["status"] == "unavailable"
    assert result["description"] == "" and result["employer_verified"] is None


def test_malformed_json_never_restores_a_cached_verified_capability(monkeypatch):
    calls = install_http(monkeypatch, lambda request: httpx.Response(200, content=b"not valid JSON"))
    result = asyncio.run(refresh.refresh_candidate(fetched()))
    assert len(calls) == 1
    assert result["candidate_refresh"]["status"] == "unavailable"
    assert not verified_destination(result)
