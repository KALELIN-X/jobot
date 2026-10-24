"""Strict public Workday posting identities, without employer API credentials.

The CXS detail shape is observed on public Workday hosted career pages, not a
promise of a universal or supported employer integration API.
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlsplit


_HOST = re.compile(r"(?P<tenant>[a-z0-9][a-z0-9-]{0,79})\.wd[0-9]{1,2}\.myworkdayjobs\.com")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}")
_REQ = re.compile(r"(?P<slug>[A-Za-z0-9][A-Za-z0-9_\-.,() ]{0,349})_(?P<req>[A-Za-z]{0,8}[-]?\d{3,20}(?:[-]\d{1,8})?)")


def parse_workday_destination(url: str, *, fixture: bool = False) -> dict | None:
    if not isinstance(url, str) or len(url) > 4000 or "\\" in url or any(char.isspace() for char in url):
        return None
    try:
        target = urlsplit(url)
        if target.username or target.password or target.fragment or not target.hostname:
            return None
        host = _HOST.fullmatch(target.hostname)
        if fixture:
            if target.scheme != "http" or target.hostname != "127.0.0.1" or target.port is None:
                return None
            tenant = "example"
        elif not host or target.scheme != "https" or target.port not in (None, 443):
            return None
        else:
            tenant = host["tenant"]
        # Commas/spaces in Workday's location segment are common; encoded
        # separators, traversal and double decoding must never change identity.
        path = unquote(target.path)
        if "%" in path or re.search(r"%2f|%5c", target.path, re.I) or any(segment in {".", ".."} for segment in path.split("/")):
            return None
        segments = path.strip("/").split("/")
        locale = ""
        if segments and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
            locale = segments.pop(0)
        if len(segments) < 3 or not _SITE.fullmatch(segments[0]) or segments[1] != "job":
            return None
        site = segments[0]
        tail = segments[2:]
        if tail[-2:] == ["apply", "applyManually"]:
            tail = tail[:-2]
        elif tail[-1:] == ["apply"]:
            tail = tail[:-1]
        if len(tail) not in {1, 2} or not all(tail):
            return None
        posting = _REQ.fullmatch(tail[-1])
        if not posting:
            return None
        if len(tail) == 2 and not re.fullmatch(r"[A-Za-z0-9_\-.,() ]{1,240}", tail[0]):
            return None
        prefix = "/" + "/".join(([locale] if locale else []) + [site])
        job_path = prefix + "/job/" + "/".join(quote(segment, safe="-_.()") for segment in tail)
        origin = f"{target.scheme}://{target.netloc}"
        req = posting["req"]
        return {"platform": "workday", "ats": "workday", "host": target.hostname,
                "tenant": tenant, "site": site, "locale": locale, "origin": origin,
                "requisition_id": req, "posting_slug": tail[-1], "site_path": prefix,
                "job_path": job_path, "detail_url": origin + job_path,
                "canonical_url": origin + job_path, "apply_url": origin + job_path + "/apply/applyManually",
                "application_key": f"workday:{target.hostname}/{site.casefold()}/{req.casefold()}"}
    except (TypeError, ValueError):
        return None


def public_workday_detail_url(url: str, *, fixture: bool = False) -> str | None:
    parsed = parse_workday_destination(url, fixture=fixture)
    if not parsed:
        return None
    relative = parsed["job_path"].split("/job/", 1)[1]
    return f"{parsed['origin']}/wday/cxs/{parsed['tenant']}/{parsed['site']}/job/{relative}"


def extract_workday_posting(url: str, payload: dict, *, fixture: bool = False) -> dict | None:
    parsed = parse_workday_destination(url, fixture=fixture)
    posting = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
    if not parsed or not isinstance(posting, dict):
        return None
    external = parse_workday_destination(posting.get("externalUrl"), fixture=fixture)
    if (not external or external["application_key"] != parsed["application_key"] or external["site"] != parsed["site"]
            or posting.get("jobReqId") != parsed["requisition_id"]
            or posting.get("jobPostingSiteId") != parsed["site"]
            or not re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", str(posting.get("id", "")))
            or not isinstance(posting.get("title"), str) or not posting["title"].strip()
            or posting.get("jobPostingId") != parsed["posting_slug"]):
        return None
    country = posting.get("country")
    return {"ats": "workday", "id": posting["id"], "posting_id": posting["id"],
            "requisition_id": parsed["requisition_id"], "site": parsed["site"],
            "title": posting["title"][:1000], "description": str(posting.get("jobDescription") or "")[:60000],
            "location": str(posting.get("location") or "")[:1000],
            "country": str(country.get("descriptor") or "")[:200] if isinstance(country, dict) else "",
            "closed": posting.get("canApply") is False or posting.get("posted") is False,
            "apply_url": parsed["apply_url"], "detail_url": parsed["detail_url"]}


# Kept as an explicit alias for caller readability in discovery integrations.
verified_public_workday_posting = extract_workday_posting
