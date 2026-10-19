"""One bounded, read-only employer refresh before a candidate can open a form."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone

import httpx

from . import ingest, sources
from .destinations import parse_destination, public_detail_url, verified_destination

REFRESH_TIMEOUT_SECONDS = 12.0


def _normalized(value):
    return " ".join(str(value or "").casefold().split())


def _marker(status, message):
    return {"status": status, "checked_at": datetime.now(timezone.utc).isoformat(), "message": message}


def _clean_cached_evidence(job):
    result = copy.deepcopy(job)
    result.update(description="", description_source=None, requirements=[], required_skills=[], preferred_skills=[],
                  published_pay=[], pay_description="", employer_verified=None, destination_provenance=None, closed=None, score=None, score_detail={})
    return result


def _hold(job, status, message, *, closed=None):
    job.update(employer_verified=None, destination_provenance=None, closed=closed, fetch_error=message,
               score=None, score_detail={}, candidate_refresh=_marker(status, message))
    return job


async def _read_official_posting(endpoint):
    """Make exactly one streaming GET; redirects and oversized bodies are held."""
    async with ingest._client() as client:
        async with client.stream("GET", endpoint, follow_redirects=False) as response:
            response.raise_for_status()
            size = response.headers.get("content-length", "")
            if size.isdecimal() and int(size) > sources.MAX_EMPLOYER_BYTES:
                raise ValueError("Employer response exceeds the byte bound")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > sources.MAX_EMPLOYER_BYTES:
                    raise ValueError("Employer response exceeds the byte bound")
            return json.loads(bytes(data).decode("utf-8"))


def _has_current_location(destination, payload):
    if not isinstance(payload, dict):
        return False
    if destination["ats"] == "greenhouse":
        location = payload.get("location")
        return isinstance(location, dict) and isinstance(location.get("name"), str) and bool(location["name"].strip())
    categories = payload.get("categories")
    if not isinstance(categories, dict):
        return False
    locations = [categories.get("location")]
    if isinstance(categories.get("allLocations"), list):
        locations.extend(categories["allLocations"])
    return any(isinstance(value, str) and value.strip() for value in locations)


async def refresh_candidate(job: dict) -> dict:
    """Return a fresh payload; only ``candidate_refresh.status == verified`` can run.

    No feed discovery, retries, fallback, database writes or candidate submission
    occur here. A caller must rerank the returned description, save/reload it, and
    recheck configuration, history and eligibility before claiming/opening a form.
    """
    result = _clean_cached_evidence(job)
    destination = parse_destination(job.get("apply_url"))
    endpoint = public_detail_url(destination)
    if not destination or not endpoint or not verified_destination(job):
        return _hold(result, "unverified", "A bound, supported source destination is required before refreshing this candidate.")
    if job.get("closed") is True:
        return _hold(result, "closed", "The saved employer posting is closed; fetch and review it before applying.", closed=True)
    try:
        payload = await asyncio.wait_for(_read_official_posting(endpoint), timeout=REFRESH_TIMEOUT_SECONDS)
        if isinstance(payload, dict) and payload.get("closed") is True:
            return _hold(result, "closed", "The employer explicitly marks this posting closed.", closed=True)
        if not _has_current_location(destination, payload):
            return _hold(result, "unavailable", "The employer's current work location is unavailable; cached location evidence cannot be reused.")
        result = sources.employer_metadata(result, payload)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {404, 410}:
            return _hold(result, "closed", "The employer no longer publishes this posting (404/410).", closed=True)
        return _hold(result, "unavailable", "The employer's current posting could not be verified; review or try again later.")
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return _hold(result, "unavailable", "The candidate's employer refresh exceeded its 12-second time limit.")
    except Exception:
        # Exception text can include remote payloads; only a fixed message is saved.
        return _hold(result, "unavailable", "The employer response was unavailable, malformed, oversized or mismatched; this candidate is held.")
    if job.get("source") == "Jobright":
        # Shared metadata parsing stamps Simplify evidence. Legacy Jobright jobs
        # retain their actual bound primary-detail/board evidence, never a new feed.
        for field in ("source", "source_url", "detail_url", "destination_provenance"):
            result[field] = copy.deepcopy(job.get(field))
    if any(_normalized(result.get(field)) != _normalized(job.get(field)) for field in ("title", "location")):
        return _hold(result, "changed", "The employer's title or location changed. Refresh the source and review this posting before applying.")
    if not verified_destination(result):
        return _hold(result, "unverified", "Fresh employer data could not be bound to this source listing; review it before applying.")
    result.update(score=None, score_detail={}, fetch_error=None,
                  candidate_refresh=_marker("verified", "The same employer posting was verified immediately before form preparation; rerank current requirements."))
    return result
