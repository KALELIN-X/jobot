"""Strict identities for hosted Ashby, SmartRecruiters and Workday postings."""
import re
from urllib.parse import urlsplit, unquote

BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
UUID = r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"


def parse_extended_destination(url):
    if not isinstance(url, str) or len(url) > 4000 or not url or "\\" in url or any(c.isspace() for c in url):
        return None
    try:
        p = urlsplit(url)
        if p.hostname and re.fullmatch(r"[a-z0-9-]+\.wd[0-9]{1,2}\.myworkdayjobs\.com", p.hostname):
            from .workday_adapter import parse_workday_destination
            result = parse_workday_destination(url)
            return {**result, "board": result["site"], "posting_id": result["requisition_id"]} if result else None
        if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment
                or not p.hostname or unquote(p.path) != p.path or any(x in {".", ".."} for x in p.path.split("/"))):
            return None
        origin = "https://" + p.hostname
        if p.hostname == "jobs.ashbyhq.com":
            m = re.fullmatch(rf"/({BOARD})/({UUID})(?:/application)?/?", p.path)
            if not m:
                return None
            ats, board, posting = "ashby", m[1], m[2].lower()
            canonical, apply = f"{origin}/{board}/{posting}", f"{origin}/{board}/{posting}/application"
            key = f"ashby:{board.casefold()}/{posting}"
        elif p.hostname in {"jobs.smartrecruiters.com", "www.smartrecruiters.com"}:
            m = re.fullmatch(rf"/({BOARD})/([1-9][0-9]{{0,19}})(?:-[A-Za-z0-9][A-Za-z0-9_-]{{0,299}})?/?", p.path)
            if not m:
                return None
            ats, board, posting = "smartrecruiters", m[1], m[2]
            canonical = apply = f"{origin}/{board}/{posting}"
            key = f"smartrecruiters:{board.casefold()}/{posting}"
        else:
            return None
        return {"ats": ats, "host": p.hostname, "origin": origin, "board": board,
                "posting_id": posting, "canonical_url": canonical, "apply_url": apply, "application_key": key}
    except (ValueError, TypeError):
        return None


def extended_public_detail_url(parsed):
    if not isinstance(parsed, dict):
        return None
    parsed = parse_extended_destination(parsed.get("apply_url"))
    if not parsed:
        return None
    if parsed["ats"] == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{parsed['board']}?includeCompensation=true"
    if parsed["ats"] == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{parsed['board']}/postings/{parsed['posting_id']}"
    from .workday_adapter import public_workday_detail_url
    return public_workday_detail_url(parsed["apply_url"])
