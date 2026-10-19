"""Public metadata fixtures; no network requests or real candidate data."""

import json

import pytest

from jobradar import ingest
from jobradar.store import application_key


DETAIL = "https://jobright.ai/jobs/info/primaryjob12345"
PRIMARY = "https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789abc/apply"
OTHER = "https://jobs.lever.co/other/abcdef12-1234-1234-1234-123456789abc/apply"
DESCRIPTION = "Build reliable Python backend services and production REST APIs for customers. " * 3


def page(*nodes, anchors=""):
    return '<script type="application/json">' + json.dumps(list(nodes)) + "</script>" + anchors


def primary(**changes):
    return {"jobId": "primaryjob12345", "description": DESCRIPTION, "applyUrl": PRIMARY, **changes}


def structured(**changes):
    return {"@type": "JobPosting", "description": DESCRIPTION, "applicationUrl": OTHER, **changes}


def assert_primary_destination(parsed):
    assert parsed["apply_url"] == PRIMARY
    assert parsed["destination_provenance"] == {
        "kind": "jobright_detail", "source_url": DETAIL,
        "application_key": application_key({"apply_url": PRIMARY}),
    }


def test_primary_job_destination_is_not_overwritten_by_recommended_jsonld():
    parsed = ingest.parse_detail(page(primary(), structured()), DETAIL)
    assert_primary_destination(parsed)
    assert not parsed["fetch_error"]


def test_unrelated_job_id_cannot_stamp_a_destination():
    parsed = ingest.parse_detail(page(primary(jobId="other-job")), DETAIL)
    assert parsed["apply_url"] is None
    assert parsed["destination_provenance"] is None
    assert not parsed["description"]


def test_lone_unbound_jsonld_destination_stays_manual():
    parsed = ingest.parse_detail(page(structured()), DETAIL)
    assert parsed["apply_url"] == OTHER
    assert parsed["destination_provenance"] is None


@pytest.mark.parametrize("identity", [
    {"url": DETAIL}, {"@id": DETAIL + "?utm_source=example"},
    {"mainEntityOfPage": DETAIL}, {"mainEntityOfPage": {"@id": DETAIL}},
])
def test_jsonld_page_identity_can_select_display_data_but_cannot_stamp_job_identity(identity):
    parsed = ingest.parse_detail(page(structured(applicationUrl=PRIMARY, **identity), structured()), DETAIL)
    assert parsed["apply_url"] == PRIMARY
    assert parsed["destination_provenance"] is None


def test_apply_url_has_priority_over_generic_original_posting_fallback():
    parsed = ingest.parse_detail(page(primary(originalJobUrl=OTHER)), DETAIL)
    assert_primary_destination(parsed)


def test_primary_duplicate_canonical_destinations_are_one_application():
    posting = PRIMARY.removesuffix("/apply")
    parsed = ingest.parse_detail(page(primary(applyUrl=posting + "?utm_source=a"), primary(applyUrl=PRIMARY + "?source=b")), DETAIL)
    assert_primary_destination(parsed)


def test_conflicting_primary_application_fields_block_anchor_fallback():
    parsed = ingest.parse_detail(page(primary(applicationUrl=OTHER), anchors=f'<a href="{PRIMARY}">Apply</a>'), DETAIL)
    assert parsed["apply_url"] is None
    assert parsed["destination_provenance"] is None
    assert "Conflicting application URLs" in parsed["fetch_error"]


def test_conflicting_primary_job_nodes_do_not_pick_last_destination():
    parsed = ingest.parse_detail(page(primary(), primary(applyUrl=OTHER)), DETAIL)
    assert parsed["apply_url"] is None
    assert "destination needs review" in parsed["fetch_error"]


def test_multiple_unbound_structured_jobs_do_not_pick_a_recommended_job():
    parsed = ingest.parse_detail(page(structured(), structured(applicationUrl=PRIMARY)), DETAIL)
    assert parsed["apply_url"] is None
    assert parsed["destination_provenance"] is None
    assert "Multiple unbound jobs" in parsed["fetch_error"]


def test_generic_apply_anchor_is_openable_but_has_no_provenance():
    parsed = ingest.parse_detail(page(primary(applyUrl=None), anchors=f'<a href="{PRIMARY}">Apply now</a>'), DETAIL)
    assert parsed["apply_url"] == PRIMARY
    assert parsed["destination_provenance"] is None


def test_multiple_generic_apply_anchors_are_ambiguous():
    parsed = ingest.parse_detail(page(primary(applyUrl=None), anchors=f'<a href="{PRIMARY}">Apply</a><a href="{OTHER}">Apply now</a>'), DETAIL)
    assert parsed["apply_url"] is None
    assert "Multiple Apply links" in parsed["fetch_error"]


def test_unsupported_ats_link_remains_visible_without_a_lever_capability():
    url = "https://boards.greenhouse.io/acme/jobs/12345"
    parsed = ingest.parse_detail(page(primary(applyUrl=url)), DETAIL)
    assert parsed["apply_url"] == url
    assert parsed["ats"] == "greenhouse"
    assert parsed["destination_provenance"] is None


def test_invalid_lever_path_is_not_stamped_as_an_application():
    parsed = ingest.parse_detail(page(primary(applyUrl="https://jobs.lever.co/acme/not-a-posting-id")), DETAIL)
    assert parsed["destination_provenance"] is None
