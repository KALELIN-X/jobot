from __future__ import annotations

import hashlib
import json
from pathlib import Path
from .destinations import SUPPORTED_ATS, parse_destination, verified_destination
from .locations import location_matches
from .models import company_scope

POLICY_VERSION = 5


def material_profile(profile):
    """Only candidate values and answer scope affect submission approval.

    Setup progress and confirmation timestamps are audit metadata. Normalize
    optional schema defaults so completing a walkthrough or saving identical
    answers does not force another approval.
    """
    from .models import Profile
    values = Profile.model_validate(profile).model_dump()
    application = values.get("application_profile", {})
    application.pop("completed_steps", None)
    application.pop("confirmed_at", None)
    for binding in application.get("saved_answer_bindings", []):
        binding.pop("confirmed_at", None)
    return values


def fingerprint(profile, rules, resume):
    values = {"profile": material_profile(profile), "rules": rules, "resume_sha256": resume.get("sha256"), "policy": POLICY_VERSION}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resume_intact(resume, data_dir: Path):
    filename = resume.get("stored_name", "")
    path = data_dir / "resumes" / filename
    if not filename or Path(filename).name != filename or not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == resume.get("sha256")


def valid_approval(profile, rules, resume, approval):
    return bool(approval.get("approved") and approval.get("fingerprint") == fingerprint(profile, rules, resume))


def setup_problems(profile, rules, resume):
    problems = []
    if not profile.get("name") or not profile.get("email"):
        problems.append("Save your verified name and email in Profile.")
    if not resume.get("loaded"):
        problems.append("Upload the resume you authorize for these employers.")
    if company_scope(rules) == "selected" and not rules.get("companies"):
        problems.append("Choose at least one employer for selected-company mode, or allow any fetched company.")
    if not set(rules.get("allowed_ats", [])) & SUPPORTED_ATS:
        problems.append("Enable Lever or Greenhouse applications in Rules before approving automatic submissions.")
    return problems


def job_problems(job, rules):
    problems = []
    if job.get("attempted_at") or job.get("status") in ("submitted", "uncertain", "submitting"):
        problems.append("A previous submission attempt must be checked; automatic retry is disabled.")
    companies = {name.casefold() for name in rules.get("companies", [])}
    if company_scope(rules) == "selected" and job.get("company", "").casefold() not in companies:
        problems.append("Employer is outside your approved company list.")
    ats = job.get("ats")
    if ats not in SUPPORTED_ATS:
        problems.append("Only supported verified Lever or Greenhouse application forms can run automatically.")
    elif ats not in rules.get("allowed_ats", []):
        problems.append(f"{ats.title()} applications are disabled in your saved rules.")
    parsed = parse_destination(job.get("apply_url"))
    if not parsed or parsed["ats"] != ats or parsed["apply_url"] != job.get("apply_url"):
        problems.append("A canonical supported HTTPS employer application URL is required.")
    if not verified_destination(job):
        problems.append("A supported application link published for this exact fetched role is required. Refresh jobs; missing or unverified links need manual review.")
    if job.get("closed") is True:
        problems.append("The employer posting is closed; it cannot be submitted automatically.")
    if not job.get("description") or job.get("score") is None:
        problems.append("A full job description and explainable resume ranking are required.")
    elif job["score"] < rules["min_score"]:
        problems.append("Resume ranking is below your approved minimum.")
    limit = rules.get("max_required_experience_months")
    if limit is not None:
        minimum = job.get("score_detail", {}).get("experience_requirement", {}).get("minimum_months")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            problems.append("The posting's minimum required experience is unverified. Review it before applying under an experience limit.")
        elif minimum > limit:
            problems.append(f"The posting requires at least {minimum} months of experience, above your {limit}-month limit.")
    if rules.get("locations") and not location_matches(job, rules["locations"]):
        problems.append("Job location is outside your approved locations.")
    for item in job.get("score_detail", {}).get("eligibility", []):
        if (not rules.get("enforce_graduation_window", True)
                and str(item.get("label", "")).casefold() in {"graduation", "graduation date", "graduation window"}):
            continue
        if item.get("label") == "Approved company":
            # Scope is checked directly above; stale ranking metadata cannot
            # retain a previous selected-company restriction in any mode.
            continue
        if item.get("label") == "Experience filter" and limit is not None:
            continue
        if item.get("status") == "fail" or (item.get("status") == "unknown" and rules.get("require_known_eligibility", True)):
            problems.append(f"{item.get('label', 'Requirement')}: {item.get('detail', 'needs confirmation')}")
    return problems
