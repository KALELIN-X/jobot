"""SmartRecruiters compatibility checks using synthetic loopback pages only.

These verify inspection, common-control planning and honest unsupported states.
They are not evidence of a live employer application or API submission.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from jobradar.autofill import plan_form
from jobradar.form_adapters import SNAPSHOT_JS
from jobradar.smartrecruiters_adapter import (
    inspect_smartrecruiters_page,
    inspect_smartrecruiters_snapshot,
    parse_smartrecruiters_url,
    smartrecruiters_capability,
)


PUBLIC = "https://jobs.smartrecruiters.com/Example/744000156836234-software-engineer"
ONECLICK = "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/publication/2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3"
PROFILE = {"first_name": "Alex", "last_name": "Example", "email": "alex@example.test",
           "phone": "+15555550123", "work_authorized": True, "needs_sponsorship": False}


@pytest.mark.parametrize("url,kind", [
    (PUBLIC, "posting"), (PUBLIC + "?trid=public-tracking", "posting"),
    (PUBLIC.replace("jobs.", "www."), "posting"), (ONECLICK, "application"),
    (ONECLICK + "?dcr_ci=Example", "application"),
])
def test_recognizes_only_hosted_shapes_without_application_capability(url, kind):
    parsed = parse_smartrecruiters_url(url)
    assert parsed and parsed["page_type"] == kind
    assert smartrecruiters_capability()["automatic_submission_supported"] is False


@pytest.mark.parametrize("url", [
    "http://jobs.smartrecruiters.com/Example/744000156836234",
    PUBLIC.replace("jobs.smartrecruiters.com", "jobs.smartrecruiters.com.evil.test"),
    PUBLIC.replace("https://", "https://user:password@"),
    PUBLIC.replace(".com/", ".com:8443/"), PUBLIC + "#another-form",
    PUBLIC + "/application", PUBLIC.replace("/Example/", "/%45xample/"),
    PUBLIC.replace("/Example/", "/Example\\Other/"),
    "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/publication/not-a-uuid",
    "https://www.smartr.me/oneclick-ui/company/Example/publication/2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3",
    "https://api.smartrecruiters.com/postings/2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3/candidates",
])
def test_does_not_grant_inspection_to_lookalikes_accounts_or_candidate_api(url):
    assert parse_smartrecruiters_url(url) is None
    with pytest.raises(ValueError):
        inspect_smartrecruiters_snapshot(url, {})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:4321/Example/744000156836234",
    "http://127.0.0.1:4321/oneclick-ui/company/Example/publication/2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3",
])
def test_fixtures_are_explicit_and_exact_loopback(url):
    assert parse_smartrecruiters_url(url) is None
    assert parse_smartrecruiters_url(url, fixture=True)
    assert parse_smartrecruiters_url(url.replace("127.0.0.1", "localhost"), fixture=True) is None
    assert parse_smartrecruiters_url(url.replace(":4321", ""), fixture=True) is None


def test_report_returns_metadata_without_entered_answers_or_hidden_fields():
    report = inspect_smartrecruiters_snapshot(ONECLICK, {
        "fields": [
            {"label": "First name", "type": "text", "visible": True, "value": "PRIVATE NAME", "checked": True},
            {"label": "Email", "type": "hidden", "value": "private@example.test"},
            {"label": "Resume", "type": "file", "visible": False, "has_file": True, "value": "private-resume.pdf"},
            {"label": "Country", "type": "select", "visible": True, "options": [{"label": "United States", "value": "private-option-value"}]},
        ],
        "body": "PRIVATE DOCUMENT BODY", "forms": [{"action": "https://private.test/?token=secret"}],
    })
    serialized = json.dumps(report)
    assert len(report["fields"]) == 2
    assert report["fields"][1]["options"] == [{"label": "United States", "disabled": False}]
    for secret in ("PRIVATE", "private@", "private-resume", "private-option-value", "private.test", "has_file", '"checked"', '"value"'):
        assert secret not in serialized
    assert report["automatic_submission_supported"] is False


@pytest.mark.parametrize("snapshot,status,stage,gate", [
    ({"body": "Please enable JS and disable any ad blocker"}, 403, "challenge", "anti_bot_challenge"),
    ({"captcha": True}, 200, "challenge", "anti_bot_challenge"),
    ({"login": True}, 200, "login", "account_or_login"),
    ({"fields": [{"label": "Password", "type": "password", "visible": True}]}, 200, "login", "account_or_login"),
    ({}, 429, "unavailable", "access_or_rate_limit"),
    ({"fields": [{"label": "I agree to the Privacy Notice", "type": "checkbox", "visible": True, "required": True}]}, 200, "agreement", "consent_or_agreement"),
])
def test_reports_gates_and_never_claims_submission(snapshot, status, stage, gate):
    report = inspect_smartrecruiters_snapshot(ONECLICK, snapshot, status_code=status)
    assert report["stage"] == stage and gate in report["gates"]
    assert report["automatic_submission_supported"] is False


def test_closed_and_confirmation_text_are_never_submission_receipts():
    report = inspect_smartrecruiters_snapshot(PUBLIC, {"body": "This job has expired"})
    assert report["stage"] == "closed"
    report = inspect_smartrecruiters_snapshot(ONECLICK, {"body": "Thank you for applying. Your application has been received."})
    assert report["stage"] == "unrecognized"
    assert report["automatic_submission_supported"] is False
    assert "submitted" not in report


def test_a_description_mentioning_captcha_is_not_itself_a_challenge():
    report = inspect_smartrecruiters_snapshot(PUBLIC, {"body": "Build CAPTCHA accessibility tools and improve web application forms."})
    assert report["stage"] == "posting"
    assert "anti_bot_challenge" not in report["gates"]


def test_dynamic_multiple_and_unlabelled_controls_are_concrete_limitations():
    report = inspect_smartrecruiters_snapshot(ONECLICK, {
        "fields": [
            {"label": "Location", "type": "select", "combobox": True, "required": True},
            {"label": "Skills", "type": "select", "multiple": True, "required": True},
            {"type": "range", "required": True},
        ], "navigation_buttons": ["Next", "Cancel"],
    })
    assert "multi_step_flow" in report["gates"]
    assert report["unsupported_controls"] == [
        {"label": "Location", "reasons": ["dynamic_choices_not_observed"]},
        {"label": "Skills", "reasons": ["multiple_selection_requires_review"]},
        {"label": "Unlabelled field", "reasons": ["unsupported_control", "missing_accessible_label"]},
    ]


@pytest.fixture
def local_employer():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(("GET", self.path))
            variant = parse_qs(urlsplit(self.path).query).get("variant", ["base"])[0]
            extras = {
                "base": "<button type='button'>Next</button>",
                "consent": "<label for='consent'>I agree to the Privacy Notice</label><input id='consent' type='checkbox' required><button>Submit application</button>",
                "login": "<label for='password'>Password</label><input id='password' type='password' value='PRIVATE PASSWORD'>",
                "challenge": "<p>Please enable JS and disable any ad blocker</p>",
                "choices": "<label for='location'>Location</label><input id='location' role='combobox' aria-controls='locations' aria-autocomplete='list'><div id='locations' role='listbox'><div role='option'>Boston, MA</div><div role='option' aria-disabled='true'>Remote</div><div role='option' style='opacity:0'>Invisible choice</div></div>",
                "closed_choices": "<label for='location'>Location</label><input id='location' role='combobox' aria-controls='locations' aria-autocomplete='list'><div id='locations' role='listbox' hidden><div role='option'>Boston, MA</div></div>",
            }.get(variant, "")
            html = """<!doctype html><html><body><h1>Example role</h1><form>
              <label for='first'>First name</label><input id='first' name='firstName' required>
              <label for='last'>Last name</label><input id='last' name='lastName' required>
              <label for='email'>Email</label><input id='email' type='email' required>
              <label for='auth'>Are you legally authorized to work in the United States?</label>
              <select id='auth' required><option value=''>Choose</option><option value='1'>Yes</option><option value='0'>No</option></select>
              <label for='sponsor'>Will you now or in the future require visa sponsorship?</label>
              <select id='sponsor' required><option value=''>Choose</option><option value='1'>Yes</option><option value='0'>No</option></select>
              <label for='resume'>Resume</label><input id='resume' type='file' required>
              EXTRA
              </form><script>
              document.addEventListener('click', () => fetch('/unexpected-click', {method:'POST'}));
              document.addEventListener('input', () => fetch('/unexpected-input', {method:'POST'}));
              document.querySelector('form').addEventListener('submit', event => {event.preventDefault(); fetch('/unexpected-submit', {method:'POST'})});
              </script></body></html>""".replace("EXTRA", extras).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        def do_POST(self):
            requests.append(("POST", self.path))
            self.send_response(500)
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/oneclick-ui/company/Example/publication/2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def inspect_fixture(local_employer, variant="base", *, plan=False, mismatch=False):
    url, requests = local_employer
    target = url + "?variant=" + variant
    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            async def read_only(route):
                parsed = urlsplit(route.request.url)
                if route.request.method == "GET" and parsed.hostname == "127.0.0.1" and parsed.port == urlsplit(url).port:
                    await route.continue_()
                else:
                    await route.abort()
            await context.route("**/*", read_only)
            page = await context.new_page()
            await page.goto(target)
            try:
                report = await inspect_smartrecruiters_page(page, target.replace("Example", "Another") if mismatch else target, fixture=True)
                raw = await page.evaluate(SNAPSHOT_JS, "planning-fixture")
                actions = plan_form(raw["fields"], PROFILE) if plan else None
                values = await page.locator("#first,#last,#email").evaluate_all("fields => fields.map(field => field.value)")
                return report, actions, values
            finally:
                await browser.close()
    return asyncio.run(run())


def test_browser_reads_native_dropdowns_and_common_fields_without_applicant_actions(local_employer):
    report, plan, values = inspect_fixture(local_employer, plan=True)
    assert report["stage"] == "application" and "multi_step_flow" in report["gates"]
    auth = next(f for f in report["fields"] if f["label"].startswith("Are you legally"))
    assert auth["options"] == [{"label": "Choose", "disabled": False}, {"label": "Yes", "disabled": False}, {"label": "No", "disabled": False}]
    assert {a.value for a in plan.actions} >= {"Alex", "Example", "alex@example.test", "1", "0"}
    assert len(plan.blockers) == 1 and "supplied file" in plan.blockers[0].lower()
    assert values == ["", "", ""]
    assert [method for method, _ in local_employer[1]] == ["GET"]
    assert report["automatic_submission_supported"] is False


@pytest.mark.parametrize("variant,stage,gate", [
    ("consent", "agreement", "consent_or_agreement"),
    ("login", "login", "account_or_login"),
    ("challenge", "challenge", "anti_bot_challenge"),
])
def test_browser_stops_at_distinct_site_actions_without_clicking(local_employer, variant, stage, gate):
    report, _, values = inspect_fixture(local_employer, variant)
    assert report["stage"] == stage and gate in report["gates"]
    assert values == ["", "", ""]
    assert "PRIVATE PASSWORD" not in json.dumps(report)
    assert [method for method, _ in local_employer[1]] == ["GET"]


def test_browser_only_reports_visible_controlled_dropdown_options(local_employer):
    report, _, _ = inspect_fixture(local_employer, "choices")
    field = next(f for f in report["fields"] if f["label"] == "Location")
    assert field["options"] == [{"label": "Boston, MA", "disabled": False}, {"label": "Remote", "disabled": True}]
    assert not report["unsupported_controls"]
    report, _, _ = inspect_fixture(local_employer, "closed_choices")
    field = next(f for f in report["fields"] if f["label"] == "Location")
    assert field["options"] == []
    assert {"label": "Location", "reasons": ["dynamic_choices_not_observed"]} in report["unsupported_controls"]
    assert all(method == "GET" for method, _ in local_employer[1])


def test_browser_rejects_a_different_employer_destination(local_employer):
    with pytest.raises(ValueError, match="changed destination"):
        inspect_fixture(local_employer, mismatch=True)
    assert [method for method, _ in local_employer[1]] == ["GET"]
