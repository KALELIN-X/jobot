"""Bounded new-grad discovery with source-published employer links.

The public SimplifyJobs README is smaller than its historical JSON database.
Only its software-engineering table is read; individual official employer GET
endpoints supply descriptions. This module never submits application data.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from . import ingest
from .destinations import SIMPLIFY_SOURCE_URL, listing_binding, parse_destination, public_detail_url
from .locations import country_is_us
from .job_facts import published_pay

MAX_JOBS = 100
MAX_SOURCE_BYTES = 2_000_000
MAX_EMPLOYER_BYTES = 1_500_000
SOURCE_TIMEOUT_SECONDS = 15.0
DETAIL_TIMEOUT_SECONDS = 12.0
DETAIL_BUDGET_SECONDS = 45.0


def us_location(location: str) -> bool:
    """Require an explicit US country, city/state pair, or unambiguous city alias."""
    text = str(location or "").strip()
    for part in re.split(r"[;\n]", text):
        if (part.strip().casefold() in {"nyc", "sf", "new york city", "san francisco", "washington, d.c."}
                or country_is_us({"location": part})):
            return True
    return False


class _TableRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = {"text": [], "links": []}
        elif tag == "a" and self.cell is not None:
            self.anchor = {"url": attrs.get("href", ""), "label": []}
        elif tag == "img" and self.anchor is not None:
            self.anchor["label"].append(attrs.get("alt", ""))
        elif tag in {"br", "summary"} and self.cell is not None:
            self.cell["text"].append("; ")

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None and self.cell is not None:
            self.cell["links"].append({"url": self.anchor["url"], "label": " ".join(self.anchor["label"])})
            self.anchor = None
        elif tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = re.sub(r"\s+", " ", "".join(self.cell["text"])).strip(" ;")
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if len(self.rows) < 10_000:
                self.rows.append(self.row)
            self.row = None
        elif tag in {"br", "summary"} and self.cell is not None:
            self.cell["text"].append("; ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)
        if self.anchor is not None:
            self.anchor["label"].append(data)


def _age_date(age):
    matched = re.fullmatch(r"(\d{1,4})d", age.strip())
    if matched:
        return (datetime.now(timezone.utc).date() - timedelta(days=int(matched[1]))).isoformat()
    return ingest.normalize_posted_date(age)


def _stamp_source(job):
    parsed = parse_destination(job.get("apply_url"))
    job["destination_provenance"] = ({"kind": "simplify_feed", "source_url": SIMPLIFY_SOURCE_URL,
                                     "application_key": parsed["application_key"], "listing_binding": listing_binding(job)}
                                    if parsed else None)


def parse_simplify_readme(document: str, limit: int = 100) -> list[dict]:
    limit = max(0, min(int(limit), MAX_JOBS))
    if not limit:
        return []
    heading = re.search(r"^##\s+[^\n]*Software Engineering New Grad Roles[^\n]*$", document, re.I | re.M)
    if not heading:
        raise ValueError("The public source no longer contains its software-engineering table")
    section = document[heading.end():]
    end = re.search(r"^##\s+", section, re.M)
    if end:
        section = section[:end.start()]
    parser = _TableRows()
    parser.feed(section)
    supported, manual, seen = [], [], set()
    company = ""
    for row in parser.rows:
        if len(row) != 5:
            continue
        company_cell, title, location, application, age = row
        if company_cell["text"] not in {"↳", "↪", "→", ""}:
            company = company_cell["text"]
            for marker in ("🔥", "🛂", "🇺🇸", "🎓", "🔒"):
                company = company.replace(marker, "").strip()
        location_text = re.sub(r"(?:^|;)\s*\d+\s+locations?\s*;?", "", location["text"], flags=re.I).strip(" ;")
        if not company or not title["text"] or not us_location(location_text) or "🔒" in application["text"]:
            continue
        urls = []
        for link in application["links"]:
            if not re.search(r"\bapply\b", link["label"], re.I):
                continue
            safe = ingest._public_application_url(link["url"])
            if safe and urlsplit(safe).hostname not in {"simplify.jobs", "www.simplify.jobs"} and safe not in urls:
                urls.append(safe)
        if len(urls) != 1:
            continue
        parsed = parse_destination(urls[0])
        identity = parsed["application_key"] if parsed else urls[0]
        if identity in seen:
            continue
        seen.add(identity)
        job = {"id": hashlib.sha256(identity.encode()).hexdigest(), "company": company, "title": title["text"],
               "location": location_text, "work_model": "Remote" if re.search(r"\bremote\b", location_text, re.I) else "",
               "country_code": "US", "location_evidence": f"Public source row: {location_text}",
               "source_new_grad": True, "source_category": "Software Engineering New Grad Roles",
               "source": "SimplifyJobs", "source_url": SIMPLIFY_SOURCE_URL,
               "source_listing": {"company": company, "title": title["text"], "location": location_text},
               "detail_url": parsed["canonical_url"] if parsed else urls[0],
               "apply_url": parsed["apply_url"] if parsed else urls[0], "ats": parsed["ats"] if parsed else "unsupported",
               "date_posted": _age_date(age["text"]), "date_posted_raw": age["text"],
               "description": "", "description_source": None, "requirements": [], "required_skills": [], "preferred_skills": [],
               "closed": None, "employer_verified": None, "fetch_error": None}
        _stamp_source(job)
        (supported if parsed else manual).append(job)
    return (supported + manual)[:limit]


def _plain_content(value):
    return ingest._plain(html.unescape(str(value or "")))


def employer_metadata(job: dict, payload: dict) -> dict:
    """Check the exact posting before trusting an official description."""
    destination = parse_destination(job.get("apply_url"))
    if not destination or not isinstance(payload, dict) or str(payload.get("id")) != destination["posting_id"]:
        raise ValueError("The employer response did not match the requested posting ID")
    if destination["ats"] == "lever":
        published_links = [payload.get("applyUrl"), payload.get("hostedUrl")]
        title = _plain_content(payload.get("text"))
        categories = payload.get("categories") or {}
        locations = [categories.get("location", "")]
        if isinstance(categories.get("allLocations"), list):
            locations.extend(categories["allLocations"])
        location = "; ".join(dict.fromkeys(value for value in locations if isinstance(value, str) and value.strip()))
        parts = [_plain_content(payload.get("descriptionPlain") or payload.get("description"))]
        for group in (payload.get("lists") or [])[:100]:
            if isinstance(group, dict):
                parts.extend([_plain_content(group.get("text")), _plain_content(group.get("content"))])
        parts.append(_plain_content(payload.get("additionalPlain") or payload.get("additional")))
        description = "\n".join(part for part in parts if part)
    else:
        published_links = [payload.get("absolute_url")]
        title = _plain_content(payload.get("title"))
        location = (payload.get("location") or {}).get("name", "")
        description = _plain_content(payload.get("content"))
    # A known hosted destination that names a different posting is contradictory.
    # Custom employer career URLs can be returned by Greenhouse, but do not change
    # the source-published official destination or the API endpoint we requested.
    for link in published_links:
        published = parse_destination(link)
        if published and published["application_key"] != destination["application_key"]:
            raise ValueError("The employer response changed the application destination")
    if not title or len(description.strip()) < 80:
        raise ValueError("A full published employer description is unavailable")
    result = dict(job)
    result.update(title=title, description=description[:ingest.MAX_DESCRIPTION_CHARS],
                  description_source=f"Official {destination['ats'].title()} public posting", closed=False, fetch_error=None)
    # Preserve only optional public pay fields, not the whole API response.
    # Replacing on refresh prevents a removed range from surviving as stale data.
    result["published_pay"] = published_pay(payload, destination["ats"])
    result["pay_description"] = (_plain_content(payload.get("salaryDescriptionPlain") or payload.get("salaryDescription"))[:4000]
                                 if destination["ats"] == "lever" else "")
    if destination["ats"] == "lever" and payload.get("workplaceType") in {"on-site", "remote", "hybrid"}:
        result["work_model"] = payload["workplaceType"]
    if isinstance(location, str) and location.strip():
        result["location"] = location.strip()[:1000]
        if us_location(location):
            result.update(country_code="US", location_evidence=f"Official employer posting: {location}")
        elif location.strip().casefold() != "remote":
            result.update(country_code=None, location_evidence=f"Official employer posting does not establish a US location: {location}")
    result["employer_verified"] = {"source_url": public_detail_url(destination), "application_key": destination["application_key"],
                                   "posting_id": destination["posting_id"]}
    _stamp_source(result)
    return result


async def enrich_employer_details(jobs: list[dict]) -> list[dict]:
    jobs = jobs[:MAX_JOBS]
    semaphore = asyncio.Semaphore(3)
    finished = set()
    async with ingest._client() as client:
        async def resolve(index, job):
            endpoint = public_detail_url(job.get("apply_url"))
            if not endpoint:
                job["fetch_error"] = "This published application link needs manual review; no supported public description adapter is available."
                finished.add(index)
                return
            async with semaphore:
                try:
                    raw = await asyncio.wait_for(ingest._fetch_text(client, endpoint, MAX_EMPLOYER_BYTES), DETAIL_TIMEOUT_SECONDS)
                    jobs[index] = employer_metadata(job, json.loads(raw))
                except asyncio.TimeoutError:
                    job["fetch_error"] = "The employer's public description request exceeded its time limit."
                except httpx.HTTPStatusError as error:
                    job["fetch_error"] = ingest._error_message(error)
                    if error.response.status_code in {404, 410}:
                        job["closed"] = True
                except (httpx.HTTPError, ValueError, TypeError, RecursionError, AttributeError) as error:
                    job["fetch_error"] = ingest._error_message(error)
            finished.add(index)
        tasks = [asyncio.create_task(resolve(index, job)) for index, job in enumerate(jobs)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), DETAIL_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            for index, job in enumerate(jobs):
                if index not in finished:
                    job["fetch_error"] = "The bounded employer-description refresh budget was reached. Fetch again later to retry."
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return jobs


async def fetch_public_jobs(limit: int = 100) -> list[dict]:
    limit = max(0, min(int(limit), MAX_JOBS))
    if not limit:
        return []
    source_error = None
    try:
        async with ingest._client() as client:
            document = await asyncio.wait_for(ingest._fetch_text(client, SIMPLIFY_SOURCE_URL, MAX_SOURCE_BYTES), SOURCE_TIMEOUT_SECONDS)
        jobs = parse_simplify_readme(document, limit)
        if jobs:
            return await enrich_employer_details(jobs)
        source_error = "The direct-link source had no US software-engineering rows."
    except (httpx.HTTPError, ValueError, TypeError, asyncio.TimeoutError) as error:
        source_error = f"The direct-link source was unavailable ({type(error).__name__})."
    jobs = await ingest.fetch_jobright_jobs(limit=limit)
    result = []
    seen = set()
    for job in jobs:
        if not us_location(job.get("location", "")):
            continue
        destination = parse_destination(job.get("apply_url"))
        identity = destination["application_key"] if destination else job["id"]
        if identity in seen:
            continue
        seen.add(identity)
        if destination:
            job["id"] = hashlib.sha256(identity.encode()).hexdigest()
        job.update(country_code="US", location_evidence=f"Public source row: {job.get('location', '')}", discovery_note=source_error)
        result.append(job)
    return result[:limit]
