from copy import deepcopy

import pytest

from jobradar.destinations import SIMPLIFY_SOURCE_URL, application_key_from_url, parse_destination, public_detail_url, verified_destination
from jobradar.sources import employer_metadata
from jobradar.store import Store


DESCRIPTION = "Build Python backend services and reliable REST APIs for customers. " * 4
ASHBY = "https://jobs.ashbyhq.com/Example/12345678-1234-1234-1234-123456789abc/application"
SMART = "https://jobs.smartrecruiters.com/Example/744000123456789-engineer"
WORKDAY = "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Boston-MA/Software-Engineer_JR123456"


def public_payload(url):
    if url == ASHBY:
        return {"apiVersion": "1", "jobs": [{"id": "12345678-1234-1234-1234-123456789abc", "isListed": True,
                "jobUrl": ASHBY.removesuffix("/application"), "applyUrl": ASHBY, "title": "Software Engineer",
                "location": "Boston, MA, USA", "descriptionPlain": DESCRIPTION}]}
    if url == SMART:
        return {"id": "744000123456789", "company": {"identifier": "Example"}, "name": "Software Engineer",
                "location": {"city": "Boston", "region": "MA", "country": "US"},
                "jobAd": {"sections": {"jobDescription": {"text": DESCRIPTION}}}}
    return {"jobPostingInfo": {"id": "a" * 32, "jobReqId": "JR123456", "jobPostingSiteId": "Careers",
            "jobPostingId": "Software-Engineer_JR123456", "externalUrl": WORKDAY, "title": "Software Engineer",
            "location": "Boston, MA, USA", "country": {"descriptor": "United States"}, "jobDescription": DESCRIPTION}}


def source_job(url):
    d = parse_destination(url)
    return {"id": "example", "company": "Example", "title": "Software Engineer", "location": "Boston, MA, USA",
            "ats": d["ats"], "apply_url": d["apply_url"], "detail_url": d["canonical_url"],
            "source": "SimplifyJobs", "source_url": SIMPLIFY_SOURCE_URL,
            "source_listing": {"company": "Example", "title": "Software Engineer", "location": "Boston, MA, USA"}}


@pytest.mark.parametrize("url", [ASHBY, SMART, WORKDAY])
def test_new_platform_requires_matching_official_metadata(url):
    job = source_job(url)
    assert not verified_destination(job)
    fresh = employer_metadata(job, public_payload(url))
    assert verified_destination(fresh)
    assert fresh["description"] == DESCRIPTION.strip()
    assert fresh["country_code"] == "US"
    assert fresh["employer_verified"]["source_url"] == public_detail_url(url)
    changed = {**fresh, "apply_url": fresh["apply_url"].replace("Example", "Other").replace("example.wd5", "other.wd5")}
    assert not verified_destination(changed)


@pytest.mark.parametrize("url", [ASHBY, SMART, WORKDAY])
def test_new_platform_missing_or_wrong_posting_never_verifies(url):
    with pytest.raises(ValueError):
        employer_metadata(source_job(url), {})
    payload = public_payload(url)
    if url == ASHBY:
        payload["jobs"][0]["applyUrl"] = ASHBY.replace("Example", "Other")
    elif url == SMART:
        payload["company"]["identifier"] = "Other"
    else:
        payload["jobPostingInfo"]["jobReqId"] = "JR999999"
    with pytest.raises(ValueError):
        employer_metadata(source_job(url), payload)


@pytest.mark.parametrize("url", [ASHBY, SMART, WORKDAY])
def test_new_platform_attempts_are_canonical_and_survive_reopen(tmp_path, url):
    store = Store(tmp_path / "history.sqlite3")
    job = {**source_job(url), "status": "queued"}
    store.save_job(job)
    assert store.claim_job(job["id"])
    store.reserve_attempt(job["id"], "2026-10-21", None)
    duplicate = {**job, "id": "other-feed-row", "apply_url": parse_destination(url)["canonical_url"] + "?tracking=1"}
    store.save_job(duplicate)
    assert store.destination_attempted(duplicate)
    assert not store.claim_job("other-feed-row")
    assert Store(tmp_path / "history.sqlite3").destination_attempted(duplicate)


def test_workday_encoded_location_and_apply_share_identity_but_other_tenant_does_not():
    first = "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/US%2C-CA/Software-Engineer_JR123456/apply/applyManually"
    second = first.replace("US%2C-CA", "US,-CA").replace("/apply/applyManually", "")
    assert application_key_from_url(first) == application_key_from_url(second)
    assert application_key_from_url(first) != application_key_from_url(first.replace("example.wd5", "other.wd5"))
    assert parse_destination(first.replace("US%2C-CA", "US%2FCA")) is None


def test_multiple_ashby_rows_share_one_bounded_board_read(monkeypatch):
    import asyncio
    import json
    from jobradar.sources import enrich_employer_details, MAX_ASHBY_BOARD_BYTES
    other = ASHBY.replace("56789abc", "56789abd")
    jobs = [source_job(ASHBY), source_job(other)]
    payload = public_payload(ASHBY)
    second = deepcopy(payload["jobs"][0])
    second.update(id=parse_destination(other)["posting_id"], jobUrl=other.removesuffix("/application"), applyUrl=other)
    payload["jobs"].append(second)
    calls = []
    async def read(client, endpoint, limit):
        calls.append((endpoint, limit))
        return json.dumps(payload)
    monkeypatch.setattr("jobradar.ingest._fetch_text", read)
    rows = asyncio.run(enrich_employer_details(jobs))
    assert len(calls) == 1 and calls[0][1] == MAX_ASHBY_BOARD_BYTES
    assert all(verified_destination(row) for row in rows)


def test_public_workday_closed_flag_remains_closed():
    payload = public_payload(WORKDAY)
    payload["jobPostingInfo"]["canApply"] = False
    assert employer_metadata(source_job(WORKDAY), payload)["closed"] is True
