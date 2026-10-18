from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

POLICY_VERSION = 2


def fingerprint(profile, rules, resume):
    values = {"profile": profile, "rules": rules, "resume_sha256": resume.get("sha256"), "policy": POLICY_VERSION}
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
    if not rules.get("companies"):
        problems.append("Choose exact employer names in Rules.")
    if "lever" not in rules.get("allowed_ats", []):
        problems.append("Enable Lever applications in Rules before approving automatic submissions.")
    boards = {company.casefold(): board for company, board in rules.get("company_boards", {}).items()}
    for company in rules.get("companies", []):
        if company.casefold() not in boards:
            problems.append(f"Set the verified Lever board for {company}.")
    return problems


def job_problems(job, rules):
    problems = []
    if job.get("attempted_at") or job.get("status") in ("submitted", "uncertain", "submitting"):
        problems.append("A previous submission attempt must be checked; automatic retry is disabled.")
    companies = {name.casefold() for name in rules.get("companies", [])}
    if job.get("company", "").casefold() not in companies:
        problems.append("Employer is outside your approved company list.")
    if job.get("ats") != "lever":
        problems.append("This version only supports verified Lever application forms.")
    elif "lever" not in rules.get("allowed_ats", []):
        problems.append("Lever applications are disabled in your saved rules.")
    try:
        url = urlsplit(job.get("apply_url") or "")
        port = url.port
    except ValueError:
        problems.append("The application URL is malformed.")
        url = urlsplit("")
        port = None
    if url.scheme != "https" or url.hostname not in {"jobs.lever.co", "jobs.eu.lever.co"} or url.username or url.password or port not in (None, 443):
        problems.append("A verified HTTPS Lever application URL is required.")
    expected_board = next((value for key, value in rules.get("company_boards", {}).items() if key.casefold() == job.get("company", "").casefold()), None)
    segments = url.path.strip("/").split("/")
    if not expected_board or not segments or segments[0].casefold() != expected_board.casefold():
        problems.append("The application destination does not match this employer's approved Lever board.")
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
    if rules.get("locations"):
        actual_location = re.sub(r"[^a-z0-9]+", " ", (str(job.get("location", "")) + " " + str(job.get("work_model", ""))).casefold()).strip()
        matches = any(
            (wanted := re.sub(r"[^a-z0-9]+", " ", location.casefold()).strip())
            and re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", actual_location)
            for location in rules["locations"]
        )
        if not matches:
            problems.append("Job location is outside your approved locations.")
    for item in job.get("score_detail", {}).get("eligibility", []):
        if item.get("label") == "Experience filter" and limit is not None:
            continue
        if item.get("status") == "fail" or (item.get("status") == "unknown" and rules.get("require_known_eligibility", True)):
            problems.append(f"{item.get('label', 'Requirement')}: {item.get('detail', 'needs confirmation')}")
    return problems
