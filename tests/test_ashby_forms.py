"""Ashby preflight and visible controls, using synthetic local HTML only.

These tests do not demonstrate live application submission support. They make
no requests to Ashby/employers and use no candidate résumé or private database.
"""

import asyncio
import json

import pytest
from playwright.async_api import async_playwright

from jobradar.ashby_adapter import (
    ASHBY_SNAPSHOT_JS, ashby_document_allowed, choose_ashby_yesno,
    inspect_ashby_page, inspect_ashby_snapshot, parse_ashby_destination,
    public_ashby_board_url, verified_public_ashby_posting, verify_ashby_document,
)


POSTING = "https://jobs.ashbyhq.com/ExampleCo/12345678-1234-1234-1234-123456789abc"
APPLY = POSTING + "/application"
IDENTITY = {"organization": {"hostedJobsPageSlug": "ExampleCo"},
            "posting": {"id": "12345678-1234-1234-1234-123456789abc", "title": "Software Engineer I", "isListed": True},
            "maintenanceMode": False}


def document(identity=None):
    return "<html><script>window.__appData = " + json.dumps(identity or IDENTITY) + ";\n// loader follows\n</script></html>"


def row(**changes):
    return {"jobUrl": POSTING, "applyUrl": APPLY, "title": "Software Engineer I", "isListed": True, **changes}


def test_strict_hosted_identity_preserves_board_case_and_unifies_apply_path():
    target = parse_ashby_destination(APPLY + "?utm_source=example")
    assert target["apply_url"] == APPLY
    assert target["canonical_url"] == POSTING
    assert target["board"] == "ExampleCo"
    assert target["application_key"] == "ashby:exampleco/12345678-1234-1234-1234-123456789abc"
    assert public_ashby_board_url(APPLY) == "https://api.ashbyhq.com/posting-api/job-board/ExampleCo?includeCompensation=true"


@pytest.mark.parametrize("url", [
    "http://jobs.ashbyhq.com/ExampleCo/12345678-1234-1234-1234-123456789abc/application",
    APPLY.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com.evil.test"),
    APPLY.replace("jobs.ashbyhq.com", "user:password@jobs.ashbyhq.com"),
    APPLY.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com:444"),
    APPLY + "#section", APPLY + "/extra", POSTING + "/apply",
    APPLY.replace("ExampleCo", "%45xampleCo"), APPLY.replace("/ExampleCo/", "/ExampleCo/../ExampleCo/"),
    APPLY.replace("ExampleCo", "Example Co"), APPLY.replace("ExampleCo", "Example\\Co"),
    "https://jobs.ashbyhq.com/ExampleCo", "https://jobs.ashbyhq.com/ExampleCo/not-a-posting/application",
    None, {},
])
def test_unsupported_url_never_grants_a_posting_capability(url):
    assert parse_ashby_destination(url) is None
    assert public_ashby_board_url(url) is None


def test_document_navigation_stays_on_exact_board_and_posting():
    assert ashby_document_allowed(POSTING, APPLY)
    assert ashby_document_allowed(APPLY + "?utm_source=example", APPLY)
    assert not ashby_document_allowed(APPLY.replace("ExampleCo", "another"), APPLY)
    assert not ashby_document_allowed(APPLY.replace("ExampleCo", "exampleco"), APPLY)
    assert not ashby_document_allowed(APPLY.replace("789abc", "789def"), APPLY)
    assert not ashby_document_allowed("https://jobs.ashbyhq.com/api/non-user-graphql", APPLY)


def test_public_listing_requires_both_urls_and_one_unique_listed_row():
    assert verified_public_ashby_posting(APPLY, {"apiVersion": "1", "jobs": [row()]}) == row()
    assert verified_public_ashby_posting(APPLY, {"apiVersion": "2", "jobs": [row()]}) is None
    assert verified_public_ashby_posting(APPLY, {"apiVersion": "1", "jobs": [row(), row()]}) is None
    for changes in ({"applyUrl": APPLY.replace("ExampleCo", "Other")}, {"applyUrl": "https://evil.test/apply"},
                    {"isListed": False}, {"title": ""}, {"title": 3}):
        assert verified_public_ashby_posting(APPLY, {"apiVersion": "1", "jobs": [row(**changes)]}) is None
    assert verified_public_ashby_posting(APPLY, {"apiVersion": "1", "jobs": [row(jobUrl=POSTING.replace("789abc", "789def"))]}) is None


def test_hosted_document_verifies_json_without_evaluating_scripts():
    assert verify_ashby_document(APPLY, document())
    assert not verify_ashby_document(APPLY, "<script>window.__appData = fetch('/danger');</script>")
    assert not verify_ashby_document(APPLY, "<!-- " + document() + " -->")
    assert not verify_ashby_document(APPLY, document() + document())
    assert not verify_ashby_document(APPLY, document().replace('"ExampleCo"', '"OtherCo"'))
    assert not verify_ashby_document(APPLY, document().replace('"isListed": true', '"isListed": false'))
    assert not verify_ashby_document(APPLY, document().replace('"maintenanceMode": false', '"maintenanceMode": true'))
    assert not verify_ashby_document(APPLY, document().replace('"12345678-1234-1234-1234-123456789abc"', '3'))
    assert not verify_ashby_document(APPLY, "x" * 5_000_001)


def test_inspection_reports_only_visible_question_metadata_and_no_prefilled_answers():
    result = inspect_ashby_snapshot(APPLY, {"body": "private body", "fields": [
        {"label": "Name", "type": "text", "value": "PRIVATE NAME", "visible": True},
        {"label": "Secret hidden field", "type": "text", "value": "PRIVATE", "visible": False},
        {"label": "Will you need visa sponsorship?", "type": "select", "visible": True, "required": True,
         "selected_label": "No", "options": [{"label": "Yes", "value": "private-id-1"}, {"label": "No", "value": "private-id-2"}]},
    ]}, document_verified=True)
    assert result["stage"] == "inspection_only"
    assert result["automatic_submission_supported"] is False
    assert len(result["fields"]) == 2
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized and "private-id" not in serialized and "selected_label" not in serialized
    assert result["fields"][1]["options"] == [{"label": "Yes", "disabled": False}, {"label": "No", "disabled": False}]


def test_inspection_never_promotes_unsupported_or_unverified_page_to_supported():
    assert inspect_ashby_snapshot("https://evil.test/", {})["stage"] == "unsupported_url"
    assert inspect_ashby_snapshot(APPLY, {})["stage"] == "unverified"
    report = inspect_ashby_snapshot(APPLY, {}, document_verified=True)
    assert report["stage"] == "unavailable" and "form_unavailable" in report["gates"]
    assert report["automatic_submission_supported"] is False
    assert inspect_ashby_snapshot(APPLY, {"fields": None}, document_verified=True)["stage"] == "unavailable"
    malformed = {"fields": [{"label": "Name", "visible": True, "options": {"unexpected": "object"}}]}
    assert inspect_ashby_snapshot(APPLY, malformed, document_verified=True)["fields"][0]["options"] == []


def test_inspection_separates_verification_login_and_binding_agreements():
    report = inspect_ashby_snapshot(APPLY, {"captcha": True, "login": True, "fields": [
        {"label": "I agree to the candidate arbitration agreement", "visible": True, "type": "checkbox", "required": True},
    ]}, document_verified=True)
    assert report["stage"] == "user_action_required"
    assert report["gates"] == ["login", "verification", "agreement_review"]
    assert report["automatic_submission_supported"] is False


HTML = """<!doctype html><html><body><form>
<div class="ashby-application-form-field-entry"><div data-field-path="_systemfield_name">
<div class="ashby-application-form-question-title"><label for="name">Name*</label></div>
<input id="name" name="name" required value="Synthetic Private Name"></div></div>
<div class="ashby-application-form-field-entry"><div data-field-path="sponsor">
<div class="ashby-application-form-question-title"><label for="sponsor">Will you now or in the future need visa sponsorship?*</label></div>
<div><button type="button" data-option="yes" aria-pressed="false">Yes</button>
<button type="button" data-option="no" aria-pressed="false">No</button>
<input type="checkbox" name="sponsor" tabindex="-1" style="display:none"></div></div></div>
<div class="ashby-application-form-field-entry"><div>
<div class="ashby-application-form-question-title"><label for="resume">Resume*</label></div>
<input type="file" id="resume" style="display:none"></div></div>
<button type="submit">Submit application</button></form>
<script>document.querySelectorAll('button[data-option]').forEach(button=>button.onclick=()=>{
 const group=button.parentElement;group.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
 group.querySelector('input').checked=button.dataset.option==='yes';
});</script></body></html>"""


def test_local_browser_ashby_yesno_unknown_is_distinct_from_explicit_no():
    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(HTML)
            snapshot = await page.evaluate(ASHBY_SNAPSHOT_JS, "synthetic")
            field = next(field for field in snapshot["fields"] if field.get("ashby_yesno"))
            assert field["required"] and field["value"] == ""
            resume = next(field for field in snapshot["fields"] if field["type"] == "file")
            assert resume["visible"] and resume["label"] == "Resume*" and resume["required"]
            assert not any(field["name"] == "sponsor" and field["type"] == "checkbox" for field in snapshot["fields"])
            assert field["options"] == [{"value": "Yes", "label": "Yes", "disabled": False}, {"value": "No", "label": "No", "disabled": False}]
            await choose_ashby_yesno(page, field["id"], "No")
            snapshot = await page.evaluate(ASHBY_SNAPSHOT_JS, "after-no")
            field = next(field for field in snapshot["fields"] if field.get("ashby_yesno"))
            assert field["value"] == "No" and field["selected_label"] == "No"
            await choose_ashby_yesno(page, field["id"], "Yes")
            assert await page.locator('input[name=sponsor]').is_checked()
            report = inspect_ashby_snapshot(APPLY, snapshot, document_verified=True)
            assert "Synthetic Private Name" not in json.dumps(report)
            assert report["automatic_submission_supported"] is False
            await browser.close()
    asyncio.run(check())


def test_local_browser_ashby_choice_refuses_disabled_or_unknown_option():
    async def check():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(HTML)
            snapshot = await page.evaluate(ASHBY_SNAPSHOT_JS, "synthetic")
            field = next(field for field in snapshot["fields"] if field.get("ashby_yesno"))
            with pytest.raises(ValueError, match="not supported"):
                await choose_ashby_yesno(page, field["id"], "Maybe")
            await page.locator('button[data-option=yes]').evaluate("button=>button.disabled=true")
            with pytest.raises(ValueError, match="disabled"):
                await choose_ashby_yesno(page, field["id"], "Yes")
            assert await page.locator('button[data-option=no]').get_attribute("aria-pressed") == "false"
            # The helper never navigates a page to the requested URL.
            report = await inspect_ashby_page(page, APPLY)
            assert report["stage"] == "unverified" and page.url == "about:blank"
            await browser.close()
    asyncio.run(check())
