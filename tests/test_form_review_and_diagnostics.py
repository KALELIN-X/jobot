"""Unsupported optional inputs stay untouched; blocked traffic stays diagnosable."""
import json

import pytest

import test_greenhouse_forms as forms
from jobradar.autofill import _blocked_request_info, plan_form
from test_autofill import control


employer = forms.employer
hosted_transport = forms.hosted_transport


@pytest.mark.parametrize("kind,tag,combobox", [("select-multiple", "select", False), ("select", "input", True)])
@pytest.mark.parametrize("answers", [{}, {"Voluntary identity categories": "Prefer not to say"}])
def test_optional_multiselect_does_not_block_other_fields_or_apply_a_single_saved_answer(kind, tag, combobox, answers):
    optional = control("Voluntary identity categories", field_id="optional", kind=kind, tag=tag,
                       required=False, multiple=True, combobox=combobox,
                       options=[{"label": "Prefer not to say", "value": "decline"}])
    plan = plan_form([control("Full name"), optional], {"name": "Alex Example", "answers": answers})
    assert [(action.field_id, action.value) for action in plan.actions] == [("a", "Alex Example")]
    assert not plan.blockers
    assert not plan.pending_questions


@pytest.mark.parametrize("kind,tag", [("select-multiple", "select"), ("select", "input")])
def test_required_multiselect_still_requires_manual_review_even_with_an_answer(kind, tag):
    question = "Required work preferences"
    item = control(question, kind=kind, tag=tag, multiple=True,
                   options=[{"label": "Day", "value": "day"}])
    plan = plan_form([item], {"answers": {question: "Day"}})
    assert not plan.actions
    assert "multiple-selection field requires human review" in " ".join(plan.blockers)


@pytest.mark.parametrize("required", [False, True])
def test_browser_optional_multiselect_can_be_left_empty_but_required_multiselect_cannot(employer, tmp_path, monkeypatch, required):
    original = forms.form_html
    def with_multiselect(variant):
        item = '<label for="voluntary">Voluntary identity categories</label><select id="voluntary" multiple ' + (
            'required' if required else '') + '><option value="decline">Prefer not to say</option></select>'
        return original(variant).replace('<button type="submit">', item + '<button type="submit">')
    monkeypatch.setattr(forms, "form_html", with_multiselect)
    result, reservations, receipts, _ = forms.run_fixture(employer, tmp_path)
    if required:
        assert result["status"] == "needs_attention"
        assert "multiple-selection" in result["message"]
        assert not reservations and not receipts
    else:
        assert result["status"] == "submitted", result
        assert reservations == ["reserved"] and len(receipts) == 1


def test_transport_diagnostic_keeps_only_method_host_and_known_phase():
    diagnostic = _blocked_request_info(
        "POST", "https://private-user:private-password@UPLOAD.EXAMPLE.TEST/private-path?email=secret@example.test&token=private-token#secret-fragment",
        "uploading_resume")
    assert diagnostic == {"method": "POST", "host": "upload.example.test", "phase": "uploading_resume"}
    serialized = json.dumps(diagnostic)
    for secret in ("private-user", "private-password", "private-path", "secret@example.test", "private-token", "secret-fragment"):
        assert secret not in serialized
    assert _blocked_request_info("not-an-http-method", "https://host%40secret.example/", "private phase value") == {
        "method": "OTHER", "host": "unknown-host", "phase": "unknown"}


def test_browser_blocked_request_before_submission_reports_sanitized_transport_details(employer, tmp_path):
    result, reservations, receipts, _ = forms.run_fixture(employer, tmp_path, "premature")
    assert result["status"] == "needs_attention", result
    assert not reservations and not receipts
    assert result["blocked_requests"] == [{"method": "POST", "host": "127.0.0.1", "phase": "filling_fields"}]
    assert "POST to 127.0.0.1 during filling fields" in result["message"]
    assert "/mock/thanks" not in result["message"]
    assert forms.PROFILE["email"] not in result["message"]


def test_browser_blocked_request_after_submission_remains_uncertain_with_safe_diagnostics(hosted_transport, tmp_path):
    result, receipts, reservations, _ = forms.run_hosted_fixture(hosted_transport, tmp_path, "unrelated")
    assert result["status"] == "uncertain", result
    assert reservations == ["reserved"]
    assert len(receipts["application"]) == 1 and not receipts["unrelated"]
    assert result["blocked_requests"] == [{"method": "POST", "host": "127.0.0.1", "phase": "awaiting_confirmation"}]
    assert "POST to 127.0.0.1 during awaiting confirmation" in result["message"]
    assert "/collect" not in result["message"]
    assert "Do not retry" in result["message"] or "do not retry" in result["message"]
