"""Real Chromium on loopback Lever-shaped forms; no employer is contacted.

These exercise the common worker with native controls. They are not evidence
that every current Lever employer layout or a different ATS is supported.
"""

import asyncio

import pytest

import test_greenhouse_forms as fixtures
from test_greenhouse_forms import employer
from jobradar.autofill import run_application


PROFILE = {
    "name": "Alex Example", "email": "alex@example.test",
    "phone": "+15555550123", "work_authorized": True,
    "needs_sponsorship": False,
    "answers": {"Preferred shift?": "Day"},
}


def native_form(variant):
    extra = {
        "unknown": '<label for="custom">Unfamiliar required question</label><input id="custom" name="custom" required>',
        "login": '<label for="password">Password</label><input id="password" type="password">',
        "legal": '<label><input name="agreement" type="checkbox" required>I agree to the terms</label>',
        "captcha": '<div class="g-recaptcha" data-sitekey="synthetic-fixture"></div>',
    }.get(variant, "")
    return '''<!doctype html><html><body>
      <h1>Example engineering position</h1><h2>Apply for this job</h2>
      <form id="application-form" method="post" action="/mock/thanks">
      <label for="name">Full name*</label><input id="name" name="name" required>
      <label for="email">Email*</label><input id="email" name="email" type="email" required>
      <label for="phone">Phone</label><input id="phone" name="phone" type="tel">
      <label for="resume">Resume/CV*</label><input id="resume" name="resume" type="file" required>
      <fieldset><legend>Are you legally authorized to work in the United States?</legend>
      <label><input type="radio" name="auth" value="yes" required>Yes</label>
      <label><input type="radio" name="auth" value="no" required>No</label></fieldset>
      <label for="sponsor">Will you now or in the future require visa sponsorship?*</label>
      <select id="sponsor" name="sponsor" required><option value="">Choose</option>
      <option value="yes">Yes</option><option value="no">No</option></select>
      <label for="shift">Preferred shift?*</label>
      <select id="shift" name="shift" required><option value="">Choose</option>
      <option value="day">Day</option><option value="night">Night</option></select>
      EXTRA<button type="submit">Submit application</button></form>
      <script>document.querySelector('form').addEventListener('submit', async event => {
        event.preventDefault(); const data = new FormData(event.target);
        const response = await fetch('/mock/thanks?variant=VARIANT', {method:'POST',body:data});
        document.body.innerHTML = await response.text();
      });</script></body></html>'''.replace("EXTRA", extra).replace("VARIANT", variant)


def run_native(employer, tmp_path, monkeypatch, variant="success", permitted=True):
    monkeypatch.setattr(fixtures, "form_html", native_form)
    url, receipts = employer
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_bytes(b"Synthetic candidate for local tests only.")
    reservations = []

    async def reserve():
        assert not receipts
        reservations.append("reserved")
        return permitted

    result = asyncio.run(run_application(
        {"apply_url": url + "?variant=" + variant, "ats": "lever", "demo": True},
        PROFILE, resume, allowed_hosts={"127.0.0.1"}, fixture=True,
        artifacts_dir=tmp_path / "artifacts", before_submit=reserve,
    ))
    return result, reservations, receipts, resume


def test_native_lever_shape_fills_radio_and_select_and_uploads_exact_resume_once(employer, tmp_path, monkeypatch):
    result, reservations, receipts, resume = run_native(employer, tmp_path, monkeypatch)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"] and len(receipts) == 1
    assert receipts[0] == {
        "name": b"Alex Example", "email": b"alex@example.test", "phone": b"+15555550123",
        "resume": resume.read_bytes(), "auth": b"yes", "sponsor": b"no", "shift": b"day",
    }


@pytest.mark.parametrize("variant", ["unknown", "login", "legal", "captcha"])
def test_native_lever_shape_holds_unknown_login_agreement_and_challenge_before_sending(employer, tmp_path, monkeypatch, variant):
    result, reservations, receipts, _ = run_native(employer, tmp_path, monkeypatch, variant)
    assert result["status"] == "needs_attention", result
    assert not reservations and not receipts


def test_native_lever_shape_fresh_approval_refusal_never_sends(employer, tmp_path, monkeypatch):
    result, reservations, receipts, _ = run_native(employer, tmp_path, monkeypatch, permitted=False)
    assert result["status"] == "needs_attention", result
    assert reservations == ["reserved"] and not receipts


def test_native_lever_shape_server_error_is_uncertain_not_a_false_receipt(employer, tmp_path, monkeypatch):
    result, reservations, receipts, _ = run_native(employer, tmp_path, monkeypatch, "server_error")
    assert result["status"] == "uncertain", result
    assert reservations == ["reserved"] and len(receipts) == 1
