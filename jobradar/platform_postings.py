"""Parse bounded public employer posting responses, never application APIs."""

from .destinations import parse_destination, public_detail_url


def platform_metadata(job, payload):
    from .sources import _plain_content, _stamp_source, us_location
    from .ingest import MAX_DESCRIPTION_CHARS
    d = parse_destination(job.get("apply_url"))
    if not d or not isinstance(payload, dict):
        raise ValueError("Invalid public posting")
    pay, parts, closed = [], [], False
    if d["ats"] == "ashby":
        from .ashby_adapter import verified_public_ashby_posting
        row = verified_public_ashby_posting(job["apply_url"], payload)
        if not row:
            raise ValueError("Ashby posting identity mismatch")
        title, location = row.get("title"), row.get("location")
        parts = [row.get("descriptionPlain") or row.get("descriptionHtml")]
        compensation = row.get("compensation")
        if isinstance(compensation, dict):
            # Preserve stated text; no invented normalization or annualization.
            pay_text = compensation.get("compensationTierSummary") or compensation.get("scrapeableCompensationSalarySummary")
        else:
            pay_text = ""
    elif d["ats"] == "smartrecruiters":
        if str(payload.get("id")) != d["posting_id"] or str((payload.get("company") or {}).get("identifier", "")).casefold() != d["board"].casefold():
            raise ValueError("SmartRecruiters posting identity mismatch")
        title = payload.get("name")
        loc = payload.get("location") or {}
        location = ", ".join(str(loc.get(k)) for k in ("city", "region", "country") if loc.get(k))
        sections = (payload.get("jobAd") or {}).get("sections") or {}
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            item = sections.get(key)
            if isinstance(item, dict):
                parts.append(item.get("text"))
        pay_text = ""
    elif d["ats"] == "workday":
        from .workday_adapter import extract_workday_posting
        row = extract_workday_posting(job["apply_url"], payload)
        if not row:
            raise ValueError("Workday requisition identity unavailable or closed")
        closed = row.get("closed") is True
        title, location = row.get("title"), row.get("location")
        parts, pay_text = [row.get("description")], ""
    else:
        raise ValueError("Unsupported posting adapter")
    title, description = _plain_content(title), "\n".join(_plain_content(p) for p in parts if p)
    if not title or not isinstance(location, str) or not location.strip() or len(description.strip()) < 80:
        raise ValueError("Incomplete employer description")
    result = dict(job)
    result.update(title=title, location=location.strip()[:1000], description=description[:MAX_DESCRIPTION_CHARS],
                  description_source=f"Official {d['ats']} public posting", closed=closed, fetch_error=None,
                  published_pay=pay, pay_description=_plain_content(pay_text)[:4000],
                  country_code="US" if us_location(location) else None,
                  location_evidence=f"Official employer posting: {location}",
                  employer_verified={"source_url": public_detail_url(d), "application_key": d["application_key"], "posting_id": d["posting_id"]})
    _stamp_source(result)
    return result
