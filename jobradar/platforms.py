"""Truthful platform capabilities, separate from submission authorization.

Recognizing an ATS never grants the runner permission to submit to it. Public
inspection and tested automatic applications are deliberately distinct modes.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .destinations import parse_destination


_DEFINITIONS = (
    ("lever", "Lever", True, True, "The bounded hosted-form adapter supports tested native controls and one guarded submission."),
    ("greenhouse", "Greenhouse", True, True, "The bounded hosted-form adapter supports tested uploads, dropdowns and one guarded submission."),
    ("ashby", "Ashby", False, True, "Public form inspection is available. Typing and uploads can create server-side form state; a dedicated mutation-aware application adapter is still required."),
    ("smartrecruiters", "SmartRecruiters", False, True, "Public form inspection is available. Multi-step forms, repeated records, uploads and final submission still need a dedicated application adapter."),
    ("workday", "Workday", False, False, "Tenant-specific application steps, candidate accounts and verification are not implemented in this worker."),
    ("icims", "iCIMS", False, False, "Candidate portals and employer-specific application steps are not implemented in this worker."),
    ("taleo", "Taleo", False, False, "Account-based and employer-specific application steps are not implemented in this worker."),
    ("rippling", "Rippling", False, False, "The multi-step candidate form and agreement flow are not implemented in this worker."),
    ("successfactors", "SAP SuccessFactors", False, False, "Candidate accounts and employer-specific application steps are not implemented in this worker."),
)


def _safe_parts(url):
    if not isinstance(url, str) or len(url) > 4000 or not url or "\\" in url or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 443) or unquote(parts.path) != parts.path
                or any(segment in {".", ".."} for segment in parts.path.split("/"))):
            return None
        return parts
    except ValueError:
        return None


def platform_for_url(url) -> str:
    parts = _safe_parts(url)
    if parts is None:
        return "unknown"
    parsed = parse_destination(url)
    if parsed:
        return parsed["ats"]
    host = parts.hostname
    # These identities classify public links; only the two explicit inspector
    # parsers below can confer even a read-only browser capability.
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if host in {"jobs.smartrecruiters.com", "www.smartrecruiters.com", "careers.smartrecruiters.com"}:
        return "smartrecruiters"
    if re.fullmatch(r"[a-z0-9-]+\.wd[0-9]{1,2}\.myworkdayjobs\.com", host):
        return "workday"
    if re.fullmatch(r"[a-z0-9-]+\.icims\.com", host):
        return "icims"
    if re.fullmatch(r"[a-z0-9-]+\.(?:taleo\.net|taleo\.com)", host):
        return "taleo"
    if host == "ats.rippling.com":
        return "rippling"
    if re.fullmatch(r"[a-z0-9-]+\.(?:successfactors\.com|successfactors\.eu|successfactors\.us)", host):
        return "successfactors"
    return "unknown"


def platform_catalog() -> list[dict]:
    return [{"platform": platform, "label": label, "automatic_submission_supported": automatic,
             "can_inspect": inspect, "mode": "automatic" if automatic else "inspection_only" if inspect else "manual",
             "reason": reason, "next_action": "Review your profile and queue rules before running." if automatic else
             "Use the public check to identify visible fields and blockers; finish the application on the employer site." if inspect else
             "Open the employer form and complete its required account or application steps manually."}
            for platform, label, automatic, inspect, reason in _DEFINITIONS]


def platform_support(job: dict) -> dict:
    url = job.get("apply_url") or job.get("detail_url")
    platform = platform_for_url(url)
    result = next((item for item in platform_catalog() if item["platform"] == platform), None)
    if result is None:
        return {"platform": "unknown", "label": "Other / unrecognized", "mode": "manual",
                "can_inspect": False, "automatic_submission_supported": False,
                "reason": "No tested application adapter is available for this exact destination.",
                "next_action": "Open the employer form and verify its application process manually."}
    if platform in {"ashby", "smartrecruiters"}:
        if platform == "ashby":
            from .ashby_adapter import parse_ashby_destination
            valid = bool(parse_ashby_destination(url))
        else:
            from .smartrecruiters_adapter import parse_smartrecruiters_url
            valid = bool(parse_smartrecruiters_url(url))
        if not valid:
            result.update(can_inspect=False, reason="A strict public posting or application URL is required for inspection.")
    return result
