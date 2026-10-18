from pathlib import Path

import pytest

from jobradar.autofill import (
    URLNotApproved,
    _fingerprint,
    _form_blockers,
    plan_form,
    validate_application_url,
    validate_destination_url,
)


URL = "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc/apply"
HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}


def control(label, *, field_id="a", name="", kind="text", required=True, **kwargs):
    return {
        "id": field_id,
        "label": label,
        "name": name,
        "type": kind,
        "tag": "input",
        "required": required,
        "visible": True,
        "value": "",
        **kwargs,
    }


@pytest.mark.parametrize("host", ["jobs.lever.co", "jobs.eu.lever.co"])
def test_exact_lever_apply_url_is_accepted(host):
    assert validate_application_url(URL.replace("jobs.lever.co", host), HOSTS) == host


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"),
    URL.replace("jobs.lever.co", "jobs.lever.co.evil.example"),
    URL.replace("jobs.lever.co", "evil.example@jobs.lever.co"),
    URL.replace("jobs.lever.co", "jobs.lever.co:444"),
    URL.replace("jobs.lever.co", "jobs.lever.co."),
    URL.replace("12345678-1234-1234-1234-123456789abc", "not-a-job-id"),
    URL.replace("/apply", "/apply/elsewhere"),
    URL.replace("/example/", "/example/%2e%2e/"),
    URL + "#other",
    "file:///mock/apply",
])
def test_unsupported_or_deceptive_urls_are_rejected(url):
    with pytest.raises(URLNotApproved):
        validate_application_url(url, HOSTS | {"jobs.lever.co.evil.example", "jobs.lever.co."})


def test_allowlist_is_required_even_for_known_ats():
    with pytest.raises(URLNotApproved):
        validate_application_url(URL, set())


def test_fixture_is_separate_capability_and_port_is_pinned():
    mock = "http://127.0.0.1:8912/mock/apply"
    assert validate_application_url(mock, {"127.0.0.1"}, fixture=True) == "127.0.0.1"
    validate_destination_url("http://127.0.0.1:8912/mock/thanks", mock, {"127.0.0.1"}, fixture=True)
    for target in ["http://127.0.0.1:8913/mock/thanks", "http://127.0.0.1:8912/admin"]:
        with pytest.raises(URLNotApproved):
            validate_destination_url(target, mock, {"127.0.0.1"}, fixture=True)
    with pytest.raises(URLNotApproved):
        validate_application_url(mock, {"127.0.0.1"})
    with pytest.raises(URLNotApproved):
        validate_application_url("http://localhost:8912/mock/apply", {"localhost"}, fixture=True)
    with pytest.raises(URLNotApproved):
        validate_application_url(URL, HOSTS, fixture=True)


def test_navigation_must_stay_on_original_host_even_if_other_host_approved():
    validate_destination_url(URL.replace("/apply", "/thanks"), URL, HOSTS)
    with pytest.raises(URLNotApproved):
        validate_destination_url(URL.replace("jobs.lever.co", "jobs.eu.lever.co"), URL, HOSTS)


def test_explicit_standard_values_are_filled_without_inference():
    fields = [control("Full name *"), control("Email address", field_id="b"), control("First name", field_id="c")]
    plan = plan_form(fields, {"name": "Demo Candidate", "email": "demo@example.test"})
    assert [(a.field_id, a.value) for a in plan.actions] == [("a", "Demo Candidate"), ("b", "demo@example.test")]
    assert len(plan.blockers) == 1
    assert "First name" in plan.blockers[0]


def test_unicode_required_marker_is_not_part_of_exact_question():
    plan = plan_form([control("Preferred language ✱")], {"answers": {"Preferred language": "Python"}})
    assert plan.actions[0].value == "Python"
    assert not plan.blockers


@pytest.mark.parametrize("kind,tag", [("text", "input"), ("textarea", "textarea"), ("select", "select"), ("checkbox", "input")])
def test_unknown_required_controls_are_always_held_even_if_prefilled(kind, tag):
    plan = plan_form([control("Unknown question", kind=kind, tag=tag, value="Unverified default")], {})
    assert not plan.actions
    assert len(plan.blockers) == 1


def test_optional_unknown_fields_are_left_alone():
    plan = plan_form([control("Your favorite story", required=False)], {})
    assert not plan.actions
    assert not plan.blockers


def test_approved_custom_answers_require_exact_question_label():
    profile = {"answers": {"Why this role?": "The candidate's approved answer."}}
    assert plan_form([control("Why this role?")], profile).actions[0].value == profile["answers"]["Why this role?"]
    assert plan_form([control("Why this company?")], profile).blockers
    assert plan_form([control("why this role?")], profile).blockers


@pytest.mark.parametrize("label,kind,required", [
    ("I agree to the terms", "checkbox", True),
    ("I consent to data processing", "checkbox", False),
    ("I certify that the answers are accurate", "checkbox", True),
    ("Privacy agreement", "text", True),
    ("Signature", "text", True),
])
def test_legal_fields_require_human_even_with_explicit_answer(label, kind, required):
    plan = plan_form([control(label, kind=kind, required=required)], {"answers": {label: True}})
    assert not plan.actions
    assert "Human review" in plan.blockers[0]


def test_explicit_work_authorization_and_sponsorship_map_to_exact_yes_no_choices():
    options = [{"label": "Select", "value": ""}, {"label": "Yes", "value": "Y"}, {"label": "No", "value": "N"}]
    fields = [
        control("Are you legally authorized to work in the United States?", tag="select", options=options),
        control("Will you now or in the future require sponsorship?", field_id="b", tag="select", options=options),
    ]
    plan = plan_form(fields, {"work_authorized": True, "needs_sponsorship": False})
    assert [(a.field_id, a.value) for a in plan.actions] == [("a", "Y"), ("b", "N")]
    assert not plan.blockers
    assert plan_form(fields, {"work_authorized": "yes", "needs_sponsorship": None}).blockers


def test_select_does_not_guess_a_similar_option():
    item = control("Preferred office", tag="select", options=[{"label": "Chicago suburbs", "value": "chicago"}])
    plan = plan_form([item], {"answers": {"Preferred office": "Chicago"}})
    assert not plan.actions
    assert plan.blockers


def test_radio_group_chooses_only_the_explicit_option():
    fields = [
        control("Work authorization", kind="radio", name="auth", option_label="Yes", value="Y"),
        control("Work authorization", field_id="b", kind="radio", name="auth", option_label="No", value="N"),
    ]
    plan = plan_form(fields, {"work_authorized": False})
    assert [(a.field_id, a.kind, a.value) for a in plan.actions] == [("b", "check", True)]
    assert not plan.blockers


def test_false_cannot_satisfy_a_required_checkbox():
    plan = plan_form([control("Work authorization", kind="checkbox")], {"work_authorized": False})
    assert not plan.actions
    assert plan.blockers


def test_only_supplied_resume_is_attached():
    resume = Path("/explicitly/supplied/resume.pdf")
    fields = [control("Resume", kind="file"), control("Cover letter", field_id="b", kind="file")]
    plan = plan_form(fields, {}, resume)
    assert [(a.field_id, a.kind, a.value) for a in plan.actions] == [("a", "file", str(resume))]
    assert len(plan.blockers) == 1
    assert plan_form([fields[0]], {}).blockers
    assert not plan_form([dict(fields[0], has_file=True)], {}, resume).actions


def test_hidden_and_disabled_fields_are_not_interacted_with():
    fields = [control("Secret", kind="hidden"), control("Unavailable", disabled=True), control("Not shown", visible=False, required=False)]
    plan = plan_form(fields, {})
    assert not plan.actions
    assert not plan.blockers


def test_hidden_required_custom_control_holds_submission():
    plan = plan_form([control("Unknown custom selector", visible=False)], {})
    assert not plan.actions
    assert plan.blockers


def test_familiar_form_must_have_name_email_and_approved_form_action():
    fields = [control("Full name"), control("Email", field_id="b")]
    snapshot = {"fields": fields, "buttons": [{"id": "submit", "action": URL, "form_fields": ["a", "b"]}]}
    assert not _form_blockers(snapshot, URL, HOSTS, False)
    snapshot["buttons"][0]["action"] = "https://evil.example/collect"
    assert "approved destination" in " ".join(_form_blockers(snapshot, URL, HOSTS, False))


@pytest.mark.parametrize("extra", [
    {"captcha": True},
    {"login": True},
    {"body": "By submitting this application, I agree to the terms."},
    {"body": "I certify that these answers are true."},
    {"body": "By submitting this application,\nI agree to the terms."},
    {"body": "Application submitted!"},
])
def test_page_level_security_and_declarations_are_held(extra):
    snapshot = {"fields": [control("Name"), control("Email", field_id="b")], "buttons": [{"id": "s", "action": URL, "form_fields": ["a", "b"]}], **extra}
    assert _form_blockers(snapshot, URL, HOSTS, False)


def test_state_fingerprint_ignores_scanner_ids_but_detects_form_changes():
    first = {"fields": [control("Name", value="Demo")], "buttons": [{"action": URL, "form_fields": ["a"]}]}
    rescanned = {"fields": [control("Name", field_id="random-new-id", value="Demo")], "buttons": [{"action": URL, "form_fields": ["random-new-id"]}]}
    changed = {"fields": [control("Name", value="Other")], "buttons": first["buttons"]}
    assert _fingerprint(first) == _fingerprint(rescanned)
    assert _fingerprint(first) != _fingerprint(changed)
