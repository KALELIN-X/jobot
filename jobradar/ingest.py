"""Bounded, public-only ingestion for the one configured Jobright repository.

No browser authentication, private API, or arbitrary URL fetching is used here.
Application links are evidence from published data; missing links stay missing.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .autofill import LEVER_HOSTS, URLNotApproved, validate_application_url
from .store import application_key
from .destinations import SIMPLIFY_SOURCE_URL

SOURCE_URL = "https://raw.githubusercontent.com/jobright-ai/2026-Software-Engineer-New-Grad/master/README.md"
MAX_JOBS = 100
MAX_SOURCE_BYTES = 2_000_000
MAX_DETAIL_BYTES = 2_000_000
MAX_LEVER_BYTES = 3_000_000
MAX_DESCRIPTION_CHARS = 60_000
SOURCE_READ_TIMEOUT_SECONDS = 15.0
DETAIL_TIMEOUT_SECONDS = 12.0
DETAIL_RESOLUTION_BUDGET_SECONDS = 45.0
_JOB_PATH = re.compile(r"^/jobs/info/[A-Za-z0-9_-]{8,100}/?$")
_BOARD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_LINK = re.compile(r"\[((?:\\.|[^\]])+)\]\((https?://[^\s)]+)(?:\s+[^)]*)?\)")
_CELL_LINK = re.compile(r"\[((?:\\.|[^\]])+)\]\([^)]*\)")
_ATS_HOSTS = {
    "jobs.lever.co": "lever", "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby", "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse", "boards.eu.greenhouse.io": "greenhouse",
    "job-boards.eu.greenhouse.io": "greenhouse",
}


def _plain(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<(?:br\s*/?|/p|/li|/div|/h[1-6])\s*>", "\n", text, flags=re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _cell(value: str) -> str:
    # Employer website links occasionally have malformed schemes in the feed;
    # the visible label is still the employer name and never a fetch destination.
    value = _CELL_LINK.sub(lambda m: m.group(1), value)
    return _plain(value.replace("**", "").replace("\\|", "|").replace("\\[", "[").replace("\\]", "]"))


def normalize_posted_date(value: str, today: date | None = None) -> str | None:
    """Resolve feed month/day dates without the browser's implicit 2001 year."""
    today = today or datetime.now(timezone.utc).date()
    cleaned = re.sub(r"\s+", " ", value.strip())
    if cleaned.casefold() in {"today", "just now"}:
        return today.isoformat()
    if cleaned.casefold() == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        pass
    for year in (today.year, today.year - 1):
        for template in ("%b %d %Y", "%B %d %Y"):
            try:
                posted = datetime.strptime(f"{cleaned} {year}", template).date()
            except ValueError:
                continue
            # Feed entries are recent; a December row fetched in January belongs
            # to the prior year. Allow one day for UTC/local posting differences.
            if posted <= today + timedelta(days=1):
                return posted.isoformat()
    return None


def canonical_detail_url(url: str) -> str | None:
    """Drop tracking parameters, accepting only public Jobright detail paths."""
    try:
        parts = urlsplit(html.unescape(url))
        if (parts.scheme != "https" or parts.hostname != "jobright.ai"
                or parts.username or parts.password or parts.port not in (None, 443)
                or not _JOB_PATH.fullmatch(parts.path)):
            return None
        return urlunsplit(("https", "jobright.ai", parts.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        return None


def _public_application_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(html.unescape(value.strip()).replace("\\/", "/"))
        hostname = (parts.hostname or "").lower()
        if (parts.scheme != "https" or not hostname or "." not in hostname
                or hostname in {"localhost", "jobright.ai", "raw.githubusercontent.com"}
                or hostname.endswith((".localhost", ".local", ".internal"))
                or parts.username or parts.password or parts.port not in (None, 443)):
            return None
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            return None
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                           if not k.lower().startswith("utm_")])
        return urlunsplit(("https", hostname, parts.path, query, ""))
    except ValueError:
        return None


def classify_ats(url: str | None) -> str:
    safe = _public_application_url(url)
    return _ATS_HOSTS.get(urlsplit(safe).hostname, "unsupported") if safe else "unsupported"


def canonical_lever_application_url(url: Any) -> str | None:
    """Resolve only supported Lever posting URLs to a strict application URL."""
    if not isinstance(url, str):
        return None
    try:
        if urlsplit(url).fragment:
            return None
        key = application_key({"apply_url": url})
        if not key:
            return None
        canonical = "https://" + key.removeprefix("lever:") + "/apply"
        validate_application_url(canonical, set(LEVER_HOSTS))
        return canonical
    except (URLNotApproved, ValueError):
        return None


def destination_provenance(url: Any, kind: str, source_url: str) -> dict | None:
    canonical = canonical_lever_application_url(url)
    if not canonical:
        return None
    if kind == "jobright_detail":
        if canonical_detail_url(source_url) != source_url:
            return None
    elif kind == "lever_board":
        board = urlsplit(canonical).path.strip("/").split("/")[0]
        if source_url != f"https://api.lever.co/v0/postings/{board}?mode=json":
            return None
    else:
        return None
    return {"kind": kind, "source_url": source_url,
            "application_key": application_key({"apply_url": canonical})}


def verified_destination(job: dict) -> bool:
    """Require a bound source record, rather than trusting a pasted ATS URL."""
    provenance = job.get("destination_provenance")
    if not isinstance(provenance, dict):
        return False
    if (job.get("source") != "Jobright" or job.get("source_url") != SOURCE_URL
            or not job.get("detail_url") or canonical_detail_url(job["detail_url"]) != job["detail_url"]):
        return False
    canonical = canonical_lever_application_url(job.get("apply_url"))
    if canonical is None or canonical != job.get("apply_url"):
        return False
    expected = destination_provenance(canonical, provenance.get("kind"), provenance.get("source_url"))
    if not expected or provenance != expected:
        return False
    return provenance["kind"] != "jobright_detail" or provenance["source_url"] == job["detail_url"]


def parse_markdown(markdown: str, limit: int = 40) -> list[dict]:
    """Read table rows in source order, carrying the company across ↳ rows."""
    limit = max(0, min(int(limit), MAX_JOBS))
    jobs: list[dict] = []
    seen: set[str] = set()
    company = ""
    for line in markdown.splitlines()[:10_000]:
        if len(jobs) >= limit:
            break
        if not line.lstrip().startswith("|"):
            continue
        cells = [part.strip() for part in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) != 5:
            continue
        current_company = _cell(cells[0])
        if current_company not in {"↳", "↪", "→", ""}:
            company = current_company
        title_link = _LINK.search(cells[1])
        detail_url = canonical_detail_url(title_link.group(2)) if title_link else None
        if not company or not detail_url or detail_url in seen:
            continue
        seen.add(detail_url)
        jobs.append({
            "id": hashlib.sha256(detail_url.encode("utf-8")).hexdigest(),
            "company": company, "title": _cell(cells[1]),
            "location": _cell(cells[2]), "work_model": _cell(cells[3]),
            "date_posted": normalize_posted_date(_cell(cells[4])),
            "date_posted_raw": _cell(cells[4]), "source": "Jobright",
            "source_url": SOURCE_URL, "detail_url": detail_url,
            "description": "", "description_source": None,
            "requirements": [], "required_skills": [], "preferred_skills": [],
            "apply_url": None, "ats": "unsupported", "closed": None,
            "destination_provenance": None,
            "fetch_error": None,
        })
    return jobs


def _allowed_fetch_url(url: str) -> bool:
    if url in {SOURCE_URL, SIMPLIFY_SOURCE_URL}:
        return True
    if canonical_detail_url(url) == url:
        return True
    try:
        parts = urlsplit(url)
        prefix = "/v0/postings/"
        if parts.scheme != "https" or parts.fragment or parts.username or parts.password:
            return False
        if parts.netloc in {"api.lever.co", "api.eu.lever.co"} and parts.path.startswith(prefix):
            path = parts.path[len(prefix):]
            if _BOARD.fullmatch(path):
                return parts.query == "mode=json"
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", path)) and not parts.query
        return (parts.netloc == "boards-api.greenhouse.io"
                and bool(re.fullmatch(r"/v1/boards/[A-Za-z0-9][A-Za-z0-9_-]{0,79}/jobs/[1-9][0-9]{0,19}", parts.path))
                and parts.query == "content=true")
    except (TypeError, ValueError):
        return False


async def _fetch_text(client: httpx.AsyncClient, url: str, max_bytes: int) -> str:
    """Validate every hop and stream decompressed bytes under a hard bound."""
    for _ in range(4):
        if not _allowed_fetch_url(url):
            raise ValueError("Blocked URL outside the fixed public-source allowlist")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect without a location")
                redirected = urljoin(url, location)
                if not _allowed_fetch_url(redirected):
                    raise ValueError("Blocked redirect outside the fixed public-source allowlist")
                # A configured Lever board must never redirect into another board.
                if urlsplit(url).hostname in {"api.lever.co", "api.eu.lever.co", "boards-api.greenhouse.io"} and redirected != url:
                    raise ValueError("Employer posting redirect is not allowed")
                url = redirected
                continue
            response.raise_for_status()
            length = response.headers.get("content-length", "")
            if length.isdecimal() and int(length) > max_bytes:
                raise ValueError("Public response exceeds the byte limit")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError("Public response exceeds the byte limit")
            return bytes(data).decode("utf-8", errors="replace")
    raise ValueError("Too many public-source redirects")


class _PublicHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._script: list[str] | None = None
        self._script_json = False
        self._skip = 0
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self._script = []
            self._script_json = values.get("id") == "__NEXT_DATA__" or values.get("type") in {"application/json", "application/ld+json"}
        elif tag in {"style", "noscript", "svg"}:
            self._skip += 1
        elif tag == "a":
            self._anchor = (values.get("href") or "", [])
        elif tag in {"p", "li", "div", "h1", "h2", "h3", "br"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._script is not None and self._script_json:
                self.scripts.append("".join(self._script))
            self._script = None
        elif tag in {"style", "noscript", "svg"}:
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._anchor:
            self.links.append((self._anchor[0], " ".join(self._anchor[1])))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif not self._skip:
            self.text.append(data)
            if self._anchor:
                self._anchor[1].append(data)


def _walk(value: Any):
    stack = [(value, 0)]
    count = 0
    while stack and count < 15_000:
        item, depth = stack.pop()
        count += 1
        if isinstance(item, dict):
            yield item
            if depth < 25:
                stack.extend((v, depth + 1) for v in reversed(list(item.values())))
        elif isinstance(item, list) and depth < 25:
            stack.extend((v, depth + 1) for v in reversed(item))


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_plain(value)] if _plain(value) else []
    if isinstance(value, list):
        return [part for item in value[:300] for part in _strings(item)]
    return []


def _detail_application_urls(nodes: list[dict]) -> list[str]:
    """Prefer explicit apply fields; original-posting fields are a fallback.

    More than one distinct destination stays ambiguous. Canonical Lever paths
    make a posting/apply suffix or tracking query the same destination.
    """
    groups = (
        ("applyUrl", "applyURL", "apply_url", "applicationUrl", "applicationURL", "application_url"),
        ("externalJobUrl", "originalJobUrl", "jobUrl"),
    )
    for keys in groups:
        urls = []
        for node in nodes:
            for key in keys:
                safe = _public_application_url(node.get(key))
                if safe:
                    safe = canonical_lever_application_url(safe) or safe
                    if safe not in urls:
                        urls.append(safe)
        if urls:
            return urls
    return []


def parse_detail(document: str, detail_url: str = "") -> dict:
    parser = _PublicHTML()
    parser.feed(document)
    result: dict = {"description": "", "description_source": None, "apply_url": None,
                    "ats": "unsupported", "requirements": [], "required_skills": [],
                    "preferred_skills": [], "closed": None, "fetch_error": None,
                    "destination_provenance": None}
    job_id = urlsplit(detail_url).path.rstrip("/").split("/")[-1]
    nodes: list[dict] = []
    for script in parser.scripts[:30]:
        try:
            nodes.extend(_walk(json.loads(script)))
        except (ValueError, TypeError, RecursionError):
            continue
    primary = [node for node in nodes if node.get("jobId") and job_id and str(node["jobId"]) == job_id]
    structured = [node for node in nodes if node.get("@type") == "JobPosting"
                  or (isinstance(node.get("@type"), list) and "JobPosting" in node["@type"])]
    # Do not mix a matching job with recommended JSON-LD roles on the same page.
    # An exact page URL can select JSON-LD for display, but only an exact jobId
    # supplies automatic-application provenance below.
    bound_structured = []
    canonical = canonical_detail_url(detail_url)
    if canonical:
        for node in structured:
            identity_urls = [node.get("url"), node.get("@id")]
            page = node.get("mainEntityOfPage")
            identity_urls.append(page.get("@id") if isinstance(page, dict) else page)
            if any(isinstance(value, str) and canonical_detail_url(value) == canonical for value in identity_urls):
                bound_structured.append(node)
    candidates = primary or bound_structured or (structured if len(structured) == 1 else [])
    application_urls = _detail_application_urls(candidates)
    destination_error = None
    if len(application_urls) == 1:
        result["apply_url"] = application_urls[0]
        if primary and canonical:
            result["destination_provenance"] = destination_provenance(
                application_urls[0], "jobright_detail", canonical,
            )
    elif len(application_urls) > 1:
        destination_error = "Conflicting application URLs in the job data; the destination needs review"
    elif not candidates and len(structured) > 1:
        destination_error = "Multiple unbound jobs appear in the page data; the application destination needs review"
    parts: list[str] = []
    for node in candidates[:5]:
        for key in ("description", "jobDescription", "jobSummary", "coreResponsibilities", "skillSummaries", "educationSummaries", "benefitsSummaries"):
            parts.extend(_strings(node.get(key)))
        qualifications = node.get("qualifications")
        if isinstance(qualifications, dict):
            result["requirements"].extend(_strings(qualifications.get("mustHave")))
            parts.extend(_strings(qualifications.get("mustHave")))
            parts.extend(_strings(qualifications.get("preferredHave")))
        details = node.get("detailQualifications")
        if isinstance(details, dict):
            for group, key in (("mustHave", "required_skills"), ("preferredHave", "preferred_skills")):
                section = details.get(group)
                if isinstance(section, dict) and isinstance(section.get("hardSkill"), list):
                    result[key].extend(_plain(s.get("skill")) for s in section["hardSkill"][:100]
                                       if isinstance(s, dict) and s.get("skill"))
        if isinstance(node.get("isDeleted"), bool):
            result["closed"] = node["isDeleted"]
    description = "\n".join(dict.fromkeys(part for part in parts if part))
    if len(description) >= 80:
        result["description"] = description[:MAX_DESCRIPTION_CHARS]
        result["description_source"] = "Public Jobright page data"
    else:
        readable = re.sub(r"\n\s*\n+", "\n", _plain(" ".join(parser.text)))
        start = re.search(r"\b(?:Responsibilities|Qualifications|What you(?:'|’)ll do|Requirements)\b", readable, re.I)
        blocked = re.search(r"access denied|captcha|verify (?:that )?you are human|checking your browser", readable, re.I)
        if start and not blocked and len(readable[start.start():]) >= 120:
            result["description"] = readable[start.start():][:MAX_DESCRIPTION_CHARS]
            result["description_source"] = "Public Jobright readable text"
    if not result["apply_url"] and destination_error is None:
        anchor_urls = []
        for href, label in parser.links:
            safe = _public_application_url(href)
            if safe and re.search(r"\bapply\b", label, re.I):
                safe = canonical_lever_application_url(safe) or safe
                if safe not in anchor_urls:
                    anchor_urls.append(safe)
        if len(anchor_urls) == 1:
            # A generic Apply link is useful to open manually, but its label
            # alone cannot prove that it belongs to the primary job.
            result["apply_url"] = anchor_urls[0]
        elif len(anchor_urls) > 1:
            destination_error = "Multiple Apply links appear on the page; the application destination needs review"
    result["ats"] = classify_ats(result["apply_url"])
    for key in ("requirements", "required_skills", "preferred_skills"):
        result[key] = list(dict.fromkeys(result[key]))
    missing = []
    if not result["description"]:
        missing.append("Full description is unavailable in the public page")
    if destination_error:
        missing.append(destination_error)
    elif not result["apply_url"]:
        missing.append("Employer application URL is unavailable in the public page")
    result["fetch_error"] = "; ".join(missing) or None
    return result


def _error_message(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"Public source returned HTTP {error.response.status_code}; posting availability is unknown"
    if isinstance(error, httpx.TimeoutException):
        return "Public source request timed out; posting availability is unknown"
    if isinstance(error, httpx.RequestError):
        return "Public source could not be reached; posting availability is unknown"
    return str(error)[:240] or "Public-source parsing failed; posting availability is unknown"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0),
                             limits=httpx.Limits(max_connections=3, max_keepalive_connections=3),
                             follow_redirects=False, trust_env=False,
                             headers={"User-Agent": "JobRadar/1.0 (public job metadata reader)"})


async def fetch_jobright_jobs(limit: int = 40) -> list[dict]:
    """Fetch at most 100 current rows and resolve details with three workers.

    A repository failure raises RuntimeError (there are no metadata rows to return).
    Per-job failures are retained as fetch_error and never interpreted as closed.
    """
    limit = max(0, min(int(limit), MAX_JOBS))
    if not limit:
        return []
    async with _client() as client:
        try:
            markdown = await asyncio.wait_for(
                _fetch_text(client, SOURCE_URL, MAX_SOURCE_BYTES),
                timeout=SOURCE_READ_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as error:
            raise RuntimeError("Public repository read timed out; saved jobs are unchanged") from error
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError(_error_message(error)) from error
        jobs = parse_markdown(markdown, limit)
        semaphore = asyncio.Semaphore(3)
        completed: set[str] = set()

        async def resolve(job: dict) -> None:
            async with semaphore:
                try:
                    document = await asyncio.wait_for(
                        _fetch_text(client, job["detail_url"], MAX_DETAIL_BYTES),
                        timeout=DETAIL_TIMEOUT_SECONDS,
                    )
                    job.update(parse_detail(document, job["detail_url"]))
                except asyncio.TimeoutError:
                    job["fetch_error"] = (
                        f"Public detail request exceeded {DETAIL_TIMEOUT_SECONDS:g} seconds; "
                        "metadata retained and posting availability is unknown"
                    )
                except (httpx.HTTPError, ValueError, TypeError, RecursionError) as error:
                    job["fetch_error"] = _error_message(error)
            completed.add(job["id"])

        try:
            # wait_for cancels and joins the gather and its unfinished children.
            # No detail task survives the response or keeps fetching in the background.
            await asyncio.wait_for(
                asyncio.gather(*(resolve(job) for job in jobs)),
                timeout=DETAIL_RESOLUTION_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            for job in jobs:
                if job["id"] not in completed:
                    job["fetch_error"] = (
                        f"Description-resolution budget of {DETAIL_RESOLUTION_BUDGET_SECONDS:g} seconds expired; "
                        "metadata retained and posting availability is unknown"
                    )
        return jobs


def _normalized(value: Any) -> str:
    # Keep language punctuation: Engineer C++, Engineer C#, and Engineer C
    # are different titles and must never resolve to each other's applications.
    return re.sub(r"[^a-z0-9+#]+", " ", str(value or "").casefold()).strip()


def _compatible_location(repository_location: str, posting: dict) -> bool:
    categories = posting.get("categories") or {}
    locations = [categories.get("location", "")]
    if isinstance(categories.get("allLocations"), list):
        locations.extend(categories["allLocations"])
    wanted = _normalized(repository_location)
    if not wanted:
        return False
    for location in locations:
        actual = _normalized(location)
        if actual and (actual == wanted or actual in wanted or wanted in actual):
            return True
    return False


def match_lever_posting(job: dict, postings: list[dict], board: str) -> dict | None:
    """Require a unique exact title plus compatible published location."""
    matches = []
    for posting in postings[:5000]:
        if not isinstance(posting, dict) or _normalized(posting.get("text")) != _normalized(job.get("title")):
            continue
        if not _compatible_location(job.get("location", ""), posting):
            continue
        safe = canonical_lever_application_url(_public_application_url(posting.get("applyUrl")))
        if not safe or classify_ats(safe) != "lever":
            continue
        path = urlsplit(safe).path.strip("/").split("/")
        if len(path) < 2 or path[0].casefold() != board.casefold():
            continue
        matches.append(posting)
    return matches[0] if len(matches) == 1 else None


async def enrich_lever_jobs(jobs: list[dict], company_boards: dict[str, str]) -> list[dict]:
    """Resolve missing links against at most ten explicitly configured boards.

    Board slugs are user configuration, never guessed from employer names. Only
    an unambiguous exact role/location match supplies the published applyUrl.
    """
    configured = {_normalized(company): board for company, board in list(company_boards.items())[:10]
                  if isinstance(board, str) and _BOARD.fullmatch(board)}
    groups: dict[str, list[dict]] = {}
    for job in jobs[:MAX_JOBS]:
        board = configured.get(_normalized(job.get("company")))
        if board and not job.get("apply_url"):
            groups.setdefault(board, []).append(job)
    semaphore = asyncio.Semaphore(3)
    async with _client() as client:
        async def resolve_board(board: str, candidates: list[dict]) -> None:
            async with semaphore:
                try:
                    raw = await _fetch_text(client, f"https://api.lever.co/v0/postings/{board}?mode=json", MAX_LEVER_BYTES)
                    postings = json.loads(raw)
                    if not isinstance(postings, list):
                        raise ValueError("Lever public board did not return a job list")
                    for job in candidates:
                        posting = match_lever_posting(job, postings, board)
                        if not posting:
                            job["lever_resolution"] = "No unique exact title and compatible location match on the configured board"
                            continue
                        parts = _strings(posting.get("descriptionPlain"))
                        for section in (posting.get("lists") or [])[:100]:
                            if isinstance(section, dict):
                                parts.extend(_strings(section.get("text")))
                                parts.extend(_strings(section.get("content")))
                        parts.extend(_strings(posting.get("additionalPlain")))
                        description = "\n".join(parts)
                        job["apply_url"] = canonical_lever_application_url(posting["applyUrl"])
                        resolved_board = urlsplit(job["apply_url"]).path.strip("/").split("/")[0]
                        job["destination_provenance"] = destination_provenance(
                            job["apply_url"], "lever_board",
                            f"https://api.lever.co/v0/postings/{resolved_board}?mode=json")
                        job["ats"] = "lever"
                        job["closed"] = False
                        job["lever_resolution"] = "Unique exact title and compatible location on the configured public board"
                        if len(description) >= 80:
                            job["description"] = description[:MAX_DESCRIPTION_CHARS]
                            job["description_source"] = "Official Lever public board"
                            # Recompute requirements from this original description rather than Jobright summaries.
                            job["requirements"] = []
                            job["required_skills"] = []
                            job["preferred_skills"] = []
                        job["fetch_error"] = None if job.get("description") else "Full description is unavailable"
                except (httpx.HTTPError, ValueError, TypeError, RecursionError) as error:
                    for job in candidates:
                        job["lever_resolution"] = _error_message(error)
        await asyncio.gather(*(resolve_board(board, candidates) for board, candidates in groups.items()))
    return jobs


async def fetch_jobs(limit: int = 100) -> list[dict]:
    """Prefer direct source-published employer links, with a bounded fallback."""
    from .sources import fetch_public_jobs
    return await fetch_public_jobs(limit=limit)
