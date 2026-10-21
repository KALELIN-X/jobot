"""Capabilities are truthful and public checks never authorize applications."""
from copy import deepcopy
import json

import pytest
from fastapi.testclient import TestClient

from jobradar.app import create_app
from jobradar.autofill import URLNotApproved, _answer, validate_application_url
from jobradar.destinations import SIMPLIFY_SOURCE_URL, SUPPORTED_ATS
from jobradar.models import Profile
from jobradar.platform_checks import _report
from jobradar.platforms import platform_catalog, platform_for_url, platform_support
from test_workflow import HEADERS, configure_and_approve


ASHBY = "https://jobs.ashbyhq.com/example/12345678-abcd-1234-abcd-123456789abc/application"
SMART = "https://jobs.smartrecruiters.com/Example/744000123456789-engineer"


@pytest.mark.parametrize("url,platform", [
    (ASHBY, "ashby"), (SMART, "smartrecruiters"),
    ("https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Engineer_123", "workday"),
    ("https://careers-example.icims.com/jobs/123/engineer/job", "icims"),
    ("https://example.taleo.net/careersection/2/jobdetail.ftl?job=123", "taleo"),
    ("https://ats.rippling.com/example/jobs/123", "rippling"),
    ("https://career5.successfactors.eu/career?company=example", "successfactors"),
])
def test_provider_recognition_does_not_grant_legacy_runner_permission(url, platform):
    assert platform_for_url(url) == platform
    assert platform_support({"apply_url": url})["automatic_submission_supported"] == (platform in {"ashby", "smartrecruiters", "workday"})
    with pytest.raises(URLNotApproved):
        validate_application_url(url, {"jobs.ashbyhq.com", "jobs.smartrecruiters.com", "example.wd5.myworkdayjobs.com"})
    assert SUPPORTED_ATS == {"lever", "greenhouse", "ashby", "smartrecruiters", "workday"}


@pytest.mark.parametrize("url", [
    ASHBY.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com.evil.test"),
    SMART.replace("jobs.smartrecruiters.com", "user:password@jobs.smartrecruiters.com"),
    SMART.replace("https", "http"), "https://example.wd5.myworkdayjobs.com.evil.test/role",
    "https://localhost/role", "javascript:alert(1)", "https://[", ASHBY.replace("example", "%65xample"),
])
def test_malformed_or_confused_platform_links_do_not_enable_inspection(url):
    assert not platform_support({"apply_url": url})["can_inspect"]


def test_catalog_separates_automatic_inspection_and_manual():
    catalog = {item["platform"]: item for item in platform_catalog()}
    assert {key for key, item in catalog.items() if item["automatic_submission_supported"]} == {"lever", "greenhouse", "ashby", "smartrecruiters", "workday"}
    assert catalog["ashby"]["resumable"] and catalog["smartrecruiters"]["resumable"]
    assert catalog["workday"]["resumable"]
    assert not platform_support({"apply_url": "https://jobs.ashbyhq.com/example"})["can_inspect"]


def test_report_keeps_field_metadata_but_removes_candidate_values():
    report = _report({"stage": "inspected", "body": "SECRET", "fields": [
        {"label": "Email", "type": "email", "required": True, "value": "SECRET", "options": [{"label": "Choice", "value": "SECRET"}]},
        {"label": "Token", "type": "hidden", "value": "SECRET"}], "gates": []}, platform_support({"apply_url": ASHBY}))
    assert report["field_count"] == 1
    assert report["fields"][0]["label"] == "Email"
    assert "SECRET" not in json.dumps(report)
    assert not report["automatic_submission_supported"]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as value:
        yield value


def source_job(client, url=ASHBY):
    job = {"id": "public-check", "company": "Example", "title": "Engineer", "location": "Boston, MA",
           "status": "needs_attention", "apply_url": url, "ats": "unsupported", "source": "SimplifyJobs",
           "source_url": SIMPLIFY_SOURCE_URL, "source_listing": {"company": "Example", "title": "Engineer", "location": "Boston, MA"}}
    from jobradar.destinations import parse_destination, public_detail_url
    from jobradar.sources import _stamp_source
    destination = parse_destination(url)
    job.update(ats=destination["ats"], detail_url=destination["canonical_url"], apply_url=destination["apply_url"],
               employer_verified={"source_url": public_detail_url(destination), "application_key": destination["application_key"], "posting_id": destination["posting_id"]})
    _stamp_source(job)
    client.app.state.store.save_job(job)
    return job


def test_public_check_keeps_profile_approval_queue_and_attempts_unchanged(client, monkeypatch):
    configure_and_approve(client)
    job = source_job(client)
    store = client.app.state.store
    config = {key: deepcopy(store.get(key)) for key in ("profile", "rules", "resume", "approval", "automation", "stop_requested")}
    async def inspected(public_job):
        assert public_job == {"apply_url": ASHBY}
        return {"stage": "inspection_only", "fields": [{"label": "Full name", "type": "text", "value": "DO NOT RETAIN"}], "gates": []}
    monkeypatch.setattr("jobradar.platform_checks.inspect_public_platform", inspected)
    response = client.post(f"/api/jobs/{job['id']}/platform/check", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["field_count"] == 1
    assert "DO NOT RETAIN" not in response.text
    assert not response.json()["automatic_submission_supported"]
    for key, value in config.items():
        assert store.get(key) == value
    saved = store.job(job["id"])
    assert saved["status"] == "needs_attention" and saved["attempted_at"] is None
    assert not store.destination_attempted(saved)
    state = client.get("/api/state").json()
    assert state["approval"]["approved"] and len(state["platforms"]) >= 8
    assert state["jobs"][0]["platform_check_available"] is True


def test_endpoint_resanitize_preserves_transport_gate_and_still_drops_candidate_values(client, monkeypatch):
    job = source_job(client)
    support = platform_support(job)
    report = _report({"stage": "inspection_only", "fields": [{"label": "Name", "type": "text"}],
                      "gates": ["Public form review"]}, support, blocked=1)
    # The endpoint sanitizes a service report again. Preserve the recognized
    # diagnostic kind while discarding unexpected fields and candidate values.
    report["fields"][0]["value"] = "PRIVATE CANDIDATE VALUE"
    report["body"] = "PRIVATE CANDIDATE VALUE"
    report["gates"].append({"kind": "unknown_external_kind", "message": "Additional public review", "value": "PRIVATE CANDIDATE VALUE"})
    async def inspected(public_job):
        assert public_job == {"apply_url": ASHBY}
        return report
    monkeypatch.setattr("jobradar.platform_checks.inspect_public_platform", inspected)
    response = client.post(f"/api/jobs/{job['id']}/platform/check", headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert [gate["kind"] for gate in result["gates"]] == ["review", "transport", "review"]
    assert "PRIVATE CANDIDATE VALUE" not in response.text
    assert "unknown_external_kind" not in response.text
    assert all(set(gate) == {"kind", "message"} for gate in result["gates"])
    assert client.app.state.store.job(job["id"])["platform_check"] == result


@pytest.mark.parametrize("status,attempted,closed", [
    ("submitted", None, False), ("uncertain", None, False), ("dismissed", None, False),
    ("filling", None, False), ("failed", "2026-10-21T00:00:00+00:00", False), ("new", None, True),
])
def test_protected_roles_never_launch_a_platform_check(client, monkeypatch, status, attempted, closed):
    job = source_job(client)
    store = client.app.state.store
    if attempted:
        store.update_job(job["id"], "queued")
        assert store.claim_job(job["id"])
        store.reserve_attempt(job["id"], "2026-10-21", None)
    store.update_job(job["id"], status, closed=closed)
    async def forbidden(*args, **kwargs):
        pytest.fail("A protected role cannot open a browser")
    monkeypatch.setattr("jobradar.platform_checks.inspect_public_platform", forbidden)
    assert client.post(f"/api/jobs/{job['id']}/platform/check", headers=HEADERS).status_code == 409
    assert client.get("/api/state").json()["jobs"][0]["platform_check_available"] is False


def test_unbound_source_cannot_open_public_inspector(client, monkeypatch):
    job = source_job(client)
    client.app.state.store.update_job(job["id"], "new", source_url="https://example.test/untrusted")
    async def forbidden(*args, **kwargs):
        pytest.fail("Unbound source cannot launch a browser")
    monkeypatch.setattr("jobradar.platform_checks.inspect_public_platform", forbidden)
    assert client.post(f"/api/jobs/{job['id']}/platform/check", headers=HEADERS).status_code == 409


@pytest.mark.parametrize("question", ["Confirm your email", "Confirm email address", "Re-enter your email"])
def test_email_confirmation_reuses_confirmed_email(question):
    assert _answer({"label": question, "type": "email"}, {"email": "alex@example.test"}) == "alex@example.test"


def test_city_field_uses_own_explicit_fact_and_never_splits_a_full_location():
    field = {"label": "City", "type": "text"}
    assert _answer(field, Profile(location="Example City, ST, US").model_dump()) is None
    profile = Profile(location="Example City, ST, US", application_profile={"facts": {"current_city": "Example City"}}).model_dump()
    assert _answer(field, profile) == "Example City"
