"""Pure destination identities and public-source evidence; no browser or I/O.

Canonical keys identify postings for history. Browser URLs retain their actual
official origin: regional/legacy Greenhouse host aliases collapse only in the key.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
GREENHOUSE_HOSTS = frozenset({
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io",
})
SUPPORTED_HOSTS = LEVER_HOSTS | GREENHOUSE_HOSTS
SUPPORTED_ATS = frozenset({"lever", "greenhouse", "ashby", "smartrecruiters", "workday"})
JOBRIGHT_SOURCE_URL = "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md"
SIMPLIFY_SOURCE_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"
_BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
LEVER_POSTING_PATH = re.compile(rf"^/({_BOARD})/({_UUID})(?:/apply)?/?$")
GREENHOUSE_POSTING_PATH = re.compile(rf"^/({_BOARD})/jobs/([1-9][0-9]{{0,19}})/?$")
_JOBRIGHT_DETAIL_PATH = re.compile(r"^/jobs/info/[A-Za-z0-9_-]{8,100}/?$")


def parse_destination(url) -> dict | None:
    """Parse strict, supported HTTPS posting/apply URLs, stripping query tracking.

    Fragments, escaped paths, credentials, extra segments and nonstandard ports
    do not confer an application capability. Queries never change the posting ID.
    """
    if not isinstance(url, str) or not url or len(url) > 4000 or any(char.isspace() for char in url) or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host not in SUPPORTED_HOSTS:
            from .extended_destinations import parse_extended_destination
            return parse_extended_destination(url)
        if (parts.scheme != "https" or host not in SUPPORTED_HOSTS
                or parts.username or parts.password or parts.port not in (None, 443)
                or parts.fragment or unquote(parts.path) != parts.path):
            return None
        if host in LEVER_HOSTS:
            match = LEVER_POSTING_PATH.fullmatch(parts.path)
            if not match:
                return None
            ats, board, posting_id = "lever", match[1].casefold(), match[2].lower()
            path = f"/{board}/{posting_id}"
            key = f"lever:{host}/{board}/{posting_id}"
            apply_path = path + "/apply"
        else:
            match = GREENHOUSE_POSTING_PATH.fullmatch(parts.path)
            if not match:
                return None
            ats, board, posting_id = "greenhouse", match[1].casefold(), match[2]
            path = f"/{board}/jobs/{posting_id}"
            key = f"greenhouse:{board}/{posting_id}"
            apply_path = path
        origin = f"https://{host}"
        return {"ats": ats, "host": host, "origin": origin, "board": board,
                "posting_id": posting_id, "canonical_url": origin + path,
                "apply_url": origin + apply_path, "application_key": key}
    except (TypeError, ValueError):
        return None


def application_key_from_url(url) -> str | None:
    """Conservative history identity; fragments cannot evade an existing attempt.

    This intentionally accepts a fragment for deduplication only. The stricter
    parser still rejects it for application approval and browser navigation.
    """
    candidate = url.split("#", 1)[0] if isinstance(url, str) else url
    parsed = parse_destination(candidate)
    return parsed["application_key"] if parsed else None


def public_detail_url(destination) -> str | None:
    """Derive one read-only employer posting endpoint from a supported URL."""
    if isinstance(destination, str):
        destination = parse_destination(destination)
    if not isinstance(destination, dict):
        return None
    # Reparse rather than accepting caller-supplied board/host interpolation.
    parsed = parse_destination(destination.get("apply_url"))
    if not parsed:
        return None
    if parsed["ats"] == "lever":
        api_host = "api.eu.lever.co" if parsed["host"] == "jobs.eu.lever.co" else "api.lever.co"
        return f"https://{api_host}/v0/postings/{parsed['board']}/{parsed['posting_id']}"
    if parsed["ats"] not in {"lever", "greenhouse"}:
        from .extended_destinations import extended_public_detail_url
        return extended_public_detail_url(parsed)
    return (f"https://boards-api.greenhouse.io/v1/boards/{parsed['board']}/jobs/{parsed['posting_id']}"
            "?content=true&pay_transparency=true")


def listing_binding(job: dict) -> str | None:
    """Bind destination evidence to the fetched listing's visible identity."""
    parsed = parse_destination(job.get("apply_url"))
    if not parsed:
        return None
    values = {"application_key": parsed["application_key"]}
    for field in ("company", "title", "location"):
        value = job.get(field)
        if not isinstance(value, str) or (field in {"company", "title"} and not value.strip()):
            return None
        values[field] = " ".join(value.casefold().split())
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _jobright_detail(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.netloc == "jobright.ai" and not parts.query
                and not parts.fragment and bool(_JOBRIGHT_DETAIL_PATH.fullmatch(parts.path))
                and not parts.path.endswith("/"))
    except ValueError:
        return False


def _verified_jobright(job, parsed, provenance) -> bool:
    # Retain the existing exact Jobright primary-record/Lever-board evidence.
    if (job.get("source") != "Jobright" or job.get("source_url") != JOBRIGHT_SOURCE_URL
            or not _jobright_detail(job.get("detail_url")) or parsed["ats"] != "lever"
            or job.get("apply_url") != parsed["apply_url"]):
        return False
    kind = provenance.get("kind")
    source_url = provenance.get("source_url")
    if kind == "jobright_detail":
        if source_url != job["detail_url"]:
            return False
    elif kind == "lever_board":
        if source_url != f"https://api.lever.co/v0/postings/{parsed['board']}?mode=json":
            return False
    else:
        return False
    return provenance == {"kind": kind, "source_url": source_url, "application_key": parsed["application_key"]}


def verified_destination(job: dict) -> bool:
    """Require source-row evidence and bound official metadata for new discovery."""
    if job.get("demo"):
        return False
    parsed = parse_destination(job.get("apply_url"))
    provenance = job.get("destination_provenance")
    if not parsed or not isinstance(provenance, dict) or job.get("ats") != parsed["ats"]:
        return False
    if job.get("source") == "Jobright":
        return _verified_jobright(job, parsed, provenance)
    if (job.get("source") != "SimplifyJobs" or job.get("source_url") != SIMPLIFY_SOURCE_URL
            or job.get("detail_url") != parsed["canonical_url"] or job.get("apply_url") != parsed["apply_url"]):
        return False
    bound = listing_binding(job)
    if not bound or provenance != {"kind": "simplify_feed", "source_url": SIMPLIFY_SOURCE_URL,
                                   "application_key": parsed["application_key"], "listing_binding": bound}:
        return False
    evidence = job.get("employer_verified")
    if not isinstance(evidence, dict):
        return False
    endpoint = public_detail_url(parsed)
    endpoints = {endpoint}
    if parsed["ats"] == "greenhouse":
        # The earlier single-posting GET proves the same posting identity.
        # Preserve that saved proof; the next official refresh requests the
        # optional public pay ranges along with its already required content.
        endpoints.add(endpoint.removesuffix("&pay_transparency=true"))
    return any(evidence == {"source_url": source_url, "application_key": parsed["application_key"],
                            "posting_id": parsed["posting_id"]} for source_url in endpoints)
