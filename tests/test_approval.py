import hashlib
from pathlib import Path

import pytest

from jobradar.approval import fingerprint, job_problems, resume_intact, setup_problems, valid_approval
from jobradar.models import Profile, Rules
from jobradar.ingest import SOURCE_URL, destination_provenance
from jobradar.store import Store

URL = "https://jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc/apply"


def eligible_job(**changes):
    job = {"id": "1", "company": "Example", "title": "Backend engineer", "location": "Boston", "ats": "lever", "apply_url": URL, "description": "Java APIs", "score": 80, "score_detail": {"eligibility": []}, **changes}
    job.setdefault("source", "Jobright")
    job.setdefault("source_url", SOURCE_URL)
    job.setdefault("detail_url", "https://jobright.ai/jobs/info/synthetic-role-1234")
    job.setdefault("destination_provenance", destination_provenance(job["apply_url"], "jobright_detail", job["detail_url"]))
    return job


def rules(**changes):
    return Rules(companies=["Example"], company_boards={"Example": "example"}, locations=["Boston"], **changes).model_dump()


def test_valid_job_and_exact_employer_destination():
    assert job_problems(eligible_job(), rules()) == []
    assert job_problems(eligible_job(company="Example Subsidiary"), rules())
    changed_destination = eligible_job()
    changed_destination["apply_url"] = URL.replace("/example/", "/other/")
    assert job_problems(changed_destination, rules())


@pytest.mark.parametrize("url", ["http://127.0.0.1:9000/", "https://jobs.lever.co:bad/", "https://jobs.lever.co.evil.test/example/123/apply", "https://user:pass@jobs.lever.co/example/123/apply"])
def test_application_url_boundary(url):
    assert job_problems(eligible_job(apply_url=url), rules())


def test_unknown_or_failed_eligibility_is_separate_from_high_fit():
    for status in ["fail", "unknown"]:
        assert job_problems(eligible_job(score=100, score_detail={"eligibility": [{"status": status, "label": "Graduation", "detail": "Check date"}]}), rules())
    assert not job_problems(eligible_job(score_detail={"eligibility": [{"status": "unknown"}]}), rules(require_known_eligibility=False))


def test_experience_limit_is_checked_directly_before_submission():
    configured = rules(max_required_experience_months=12, require_known_eligibility=False)
    assert job_problems(eligible_job(score_detail={"eligibility": []}), configured)
    assert job_problems(eligible_job(score_detail={"experience_requirement": {"minimum_months": 24}, "eligibility": []}), configured)
    assert not job_problems(eligible_job(score_detail={"experience_requirement": {"minimum_months": 12}, "eligibility": []}), configured)
    assert not job_problems(eligible_job(score_detail={"experience_requirement": {"minimum_months": 0, "maximum_months": 24}, "eligibility": []}), configured)


def test_location_keywords_match_whole_phrases_and_work_model():
    configured = rules()
    configured["locations"] = ["US"]
    assert job_problems(eligible_job(location="Austin, Unknown region"), configured)
    assert not job_problems(eligible_job(location="Austin, TX"), configured)
    assert not job_problems(eligible_job(location="Austin, TX, US"), configured)
    configured["locations"] = ["Remote"]
    assert not job_problems(eligible_job(location="Boston, MA", work_model="Remote"), configured)


def test_approval_revoked_when_any_approved_input_changes():
    profile = Profile(name="Alex", email="alex@example.test").model_dump()
    resume = {"sha256": "abc"}
    approved = {"approved": True, "fingerprint": fingerprint(profile, rules(), resume)}
    assert valid_approval(profile, rules(), resume, approved)
    assert not valid_approval({**profile, "email": "new@example.test"}, rules(), resume, approved)
    assert not valid_approval(profile, rules(daily_limit=7), resume, approved)
    assert not valid_approval(profile, rules(), {"sha256": "changed"}, approved)


def test_resume_integrity_and_path_boundary(tmp_path):
    (tmp_path / "resumes").mkdir()
    path = tmp_path / "resumes" / "approved.txt"
    path.write_bytes(b"approved resume")
    record = {"stored_name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert resume_intact(record, tmp_path)
    path.write_bytes(b"changed resume")
    assert not resume_intact(record, tmp_path)
    assert not resume_intact({"stored_name": "../secret.txt"}, tmp_path)


def test_unconfigured_app_cannot_be_approved():
    assert len(setup_problems(Profile().model_dump(), Rules().model_dump(), {})) == 2


def test_disabling_lever_is_an_explicit_submission_boundary():
    disabled = rules(allowed_ats=[])
    problems = job_problems(eligible_job(), disabled)
    assert "Lever applications are disabled in your saved rules." in problems
    assert any("Enable a supported application platform" in problem for problem in setup_problems(
        Profile(name="Alex", email="alex@example.test").model_dump(), disabled, {"loaded": True}))


def test_attempts_are_durable_unique_and_counted_before_confirmation(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(eligible_job(status="filling"))
    store.reserve_attempt("1", "2026-10-18", 1)
    with pytest.raises(ValueError, match="already"):
        store.reserve_attempt("1", "2026-10-18", 1)
    store.save_job(eligible_job(id="2", status="filling"))
    with pytest.raises(ValueError, match="limit"):
        store.reserve_attempt("2", "2026-10-18", 1)
    store.recover()
    assert store.job("1")["status"] == "uncertain"
    assert store.job("2")["status"] == "needs_attention"


def test_refresh_does_not_erase_submitted_state(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.save_job(eligible_job(status="filling"))
    store.reserve_attempt("1", "2026-10-18", 5)
    store.update_job("1", "submitted")
    store.save_job(eligible_job(title="Refreshed title"))
    assert store.job("1")["status"] == "submitted"
    assert store.job("1")["attempted_at"]
