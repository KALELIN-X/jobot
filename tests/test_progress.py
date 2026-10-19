"""Progress is based on real browser actions, never simulated completion."""

import asyncio
import json

import pytest

import test_greenhouse_forms as forms
from test_greenhouse_forms import PROFILE, employer, hosted_transport
from jobradar.autofill import run_application
from jobradar.progress import ProgressReporter


def run_progress_fixture(employer, tmp_path, *, variant="success", callback=None, profile=None):
    url, receipts = employer
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_bytes(b"Synthetic progress fixture resume.")
    reservations = []
    async def reserve():
        assert not receipts
        reservations.append(True)
        return True
    result = asyncio.run(run_application(
        {"apply_url": url + "?variant=" + variant, "demo": True},
        PROFILE if profile is None else profile, resume,
        allowed_hosts={"127.0.0.1"}, artifacts_dir=tmp_path / "artifacts",
        fixture=True, before_submit=reserve, on_progress=callback))
    return result, receipts, reservations


def test_real_browser_progress_reports_actions_counts_and_actual_request_in_order(employer, tmp_path):
    events = []
    async def progress(event):
        if event["stage"] == "submitting":
            assert not employer[1], "Request event precedes the actual network release"
        events.append(event)
    result, receipts, reservations = run_progress_fixture(employer, tmp_path, callback=progress)
    assert result["status"] == "submitted" and len(receipts) == len(reservations) == 1
    stages = [event["stage"] for event in events]
    assert stages[0] == "opening_form" and stages[-1] == "finished"
    assert stages.index("opening_form") < stages.index("inspecting_form") < stages.index("filling_fields")
    assert stages.index("checking_answers") < stages.index("submitting") < stages.index("awaiting_confirmation") < stages.index("finished")
    assert stages.count("submitting") == stages.count("awaiting_confirmation") == 1
    counts = [event for event in events if event["stage"] == "filling_fields"]
    assert counts[0]["completed_fields"] == 0
    assert counts[-1]["completed_fields"] == counts[-1]["field_count"] > 0
    assert events[-1]["status"] == "submitted" and events[-1]["screenshot"] == result["screenshot"]
    payload = json.dumps(events)
    assert PROFILE["email"] not in payload and PROFILE["phone"] not in payload
    assert "Alex" not in payload and "synthetic-resume.txt" not in payload


def test_real_browser_unknown_question_emits_attention_without_filling_or_submitting(employer, tmp_path):
    events = []
    async def progress(event):
        events.append(event)
    result, receipts, reservations = run_progress_fixture(employer, tmp_path, variant="unknown", callback=progress)
    assert result["status"] == "needs_attention" and not receipts and not reservations
    stages = [event["stage"] for event in events]
    assert stages == ["opening_form", "inspecting_form", "checking_answers", "needs_attention"]
    assert events[-1]["pending_question_count"] == len(result["pending_questions"]) > 0


def test_real_browser_malformed_form_emits_attention_without_request(employer, tmp_path, monkeypatch):
    monkeypatch.setattr(forms, "form_html", lambda _: '<h1>Unrelated page</h1><input aria-label="Unknown required question" required>')
    events = []
    async def progress(event):
        events.append(event)
    result, receipts, reservations = run_progress_fixture(employer, tmp_path, callback=progress, profile={})
    assert result["status"] == "needs_attention" and not receipts and not reservations
    assert events[-1]["stage"] == "needs_attention"
    assert not {"filling_fields", "submitting", "awaiting_confirmation"} & {event["stage"] for event in events}


def test_real_browser_observer_failure_does_not_change_single_submission(employer, tmp_path):
    calls = []
    async def broken(event):
        calls.append(event)
        raise RuntimeError("Synthetic dashboard observer failure")
    result, receipts, reservations = run_progress_fixture(employer, tmp_path, callback=broken)
    assert result["status"] == "submitted" and len(receipts) == len(reservations) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("variant", ["success", "upload_only"])
def test_real_browser_upload_stage_is_actual_upload_and_click_alone_is_not_submitting(hosted_transport, tmp_path, variant):
    config, receipts = hosted_transport
    config["variant"] = variant
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_bytes(b"Synthetic progress fixture resume.")
    events = []
    async def progress(event):
        if event["stage"] == "submitting":
            assert len(receipts["upload"]) == 1 and not receipts["application"]
        events.append(event)
    async def reserve():
        assert len(receipts["upload"]) == 1 and not receipts["application"]
        return True
    result = asyncio.run(run_application(
        {"apply_url": config["form_origin"] + "/mock/apply", "demo": True}, PROFILE, resume,
        allowed_hosts={"127.0.0.1"}, artifacts_dir=tmp_path / "artifacts", fixture=True,
        fixture_transport={"submit_url": config["submit_url"], "upload_url": config["upload_url"]},
        before_submit=reserve, on_progress=progress))
    stages = [event["stage"] for event in events]
    assert stages.count("uploading_resume") == 1 and len(receipts["upload"]) == 1
    if variant == "success":
        assert result["status"] == "submitted" and stages[-1] == "finished"
        assert stages.index("uploading_resume") < stages.index("submitting") < stages.index("awaiting_confirmation")
    else:
        assert result["status"] == "uncertain" and stages[-1] == "uncertain"
        assert "submitting" not in stages and "awaiting_confirmation" not in stages


def test_progress_helper_filters_private_unknown_and_unbounded_fields_and_finishes_once():
    events = []
    async def capture(event):
        events.append(event)
        event["mutated"] = True
    async def scenario():
        reporter = ProgressReporter(capture)
        await reporter.emit("invented_stage", email="private@example.test")
        await reporter.emit("filling_fields", field_count=4, completed_fields=1, email="private@example.test", answers={"secret": "answer"}, pending_question_count=-1)
        await reporter.emit("filling_fields", field_count=4, completed_fields=1)
        await reporter.finish("submitted", pending_question_count=0)
        await reporter.emit("submitting")
    asyncio.run(scenario())
    assert events == [{"stage": "filling_fields", "field_count": 4, "completed_fields": 1, "mutated": True}, {"stage": "finished", "pending_question_count": 0, "status": "submitted", "mutated": True}]


def test_progress_helper_disables_timed_out_observer_once():
    calls = []
    async def slow(event):
        calls.append(event)
        await asyncio.sleep(10)
    async def scenario():
        reporter = ProgressReporter(slow, timeout=0.01)
        await reporter.emit("opening_form")
        await reporter.emit("inspecting_form")
        assert reporter.disabled
    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("url,missing_resume,status", [
    ("https://unapproved.example/application", False, "needs_attention"),
    ("http://127.0.0.1:8765/mock/apply", True, "failed"),
])
def test_rejected_preflight_reports_terminal_status_without_claiming_browser_work(tmp_path, url, missing_resume, status):
    events = []
    async def capture(event):
        events.append(event)
    result = asyncio.run(run_application(
        {"apply_url": url, "demo": True}, {}, tmp_path / "missing-resume.pdf" if missing_resume else None,
        allowed_hosts={"127.0.0.1"}, artifacts_dir=tmp_path / "artifacts", fixture=True,
        on_progress=capture))
    assert result["status"] == status
    assert events == [{"stage": status, "status": status}]
