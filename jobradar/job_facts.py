"""Compact, reproducible posting facts; never estimates or applicant eligibility.

Uses the already fetched employer text and optional public salary fields. The
full bounded description stays available to the worker/ranker; these excerpts
are for a smaller dashboard response, not a replacement for eligibility checks.
"""
from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation

from .ranking import _description_units, _experience_requirement

MAX_TEXT = 60_000
MAX_SUMMARY = 190
MAX_EVIDENCE = 180
MAX_PAY_RANGES = 3

_DEGREE = re.compile(
    r"\bbachelor(?:['’]s)?\b|\bmaster(?:['’]s)?\b|\bph\.?d\.?\b|\bdoctorate\b"
    r"|\bassociate(?:['’]s)?\s+degree\b|\b(?:B\.?S\.?|M\.?S\.?|B\.?A\.?|M\.?A\.?)\s+(?:degree|in|or)\b"
    r"|\b(?:undergraduate|graduate|university|college|technical)\s+degree\b", re.I)
_GRADUATION = re.compile(r"\bgraduat(?:e|es|ed|ion|ing)\b|\b(?:new|recent)[ -]+grads?\b|\bclass\s+of\s+20\d{2}\b", re.I)
_PERIODS = (
    ("hour", r"\b(?:hourly|per[ -]hour)\b|/\s*(?:hr|hour)\b"),
    ("year", r"\b(?:annual(?:ly)?|per[ -]year|yearly|per[ -]annum)\b|/\s*(?:yr|year)\b"),
    ("month", r"\b(?:monthly|per[ -]month)\b|/\s*(?:mo|month)\b"),
    ("week", r"\b(?:weekly|per[ -]week)\b|/\s*(?:wk|week)\b"),
    ("day", r"\b(?:daily|per[ -]day)\b|/\s*day\b"),
)
_CURRENCY = r"(?:(?:USD|CAD|AUD|NZD|SGD|HKD)\s*\$|US\$|CA\$|C\$|AU\$|A\$|NZ\$|S\$|HK\$|USD|CAD|AUD|NZD|SGD|HKD|GBP|EUR|\$|£|€)"
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{1,9}(?:\.\d{1,2})?)[kK]?"
_MONEY = re.compile(
    rf"(?<![\w])(?P<currency>{_CURRENCY})\s*(?P<minimum>{_NUMBER})"
    rf"(?:\s*(?:[-–—]|to)\s*(?P<upper_currency>{_CURRENCY})?\s*(?P<maximum>{_NUMBER}))?"
    rf"(?:\s*(?P<suffix>USD|CAD|AUD|NZD|SGD|HKD|GBP|EUR))?(?![\w,])", re.I)
_CURRENCIES = {"US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD",
               "AU$": "AUD", "A$": "AUD", "AUD": "AUD", "NZ$": "NZD", "NZD": "NZD",
               "S$": "SGD", "SGD": "SGD", "HK$": "HKD", "HKD": "HKD", "£": "GBP", "GBP": "GBP",
               "€": "EUR", "EUR": "EUR"}
_CURRENCIES.update({currency + "$": currency for currency in ("USD", "CAD", "AUD", "NZD", "SGD", "HKD")})


def _text(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return ""
    value = html.unescape(value[:limit])
    value = re.sub(r"<(?:br\s*/?|/p|/li|/div|/h[1-6])\s*>", "\n", value, flags=re.I)
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]*>", " ", value)).strip()[:limit]


def _short(value, limit=MAX_SUMMARY):
    value = re.sub(r"\s+", " ", _text(value)).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _evidence(values):
    return list(dict.fromkeys(_short(value, MAX_EVIDENCE) for value in values if isinstance(value, str) and value.strip()))[:2]


def _fact(label, status, summary, evidence=(), **extra):
    return {"label": label, "status": status, "summary": _short(summary), "evidence": _evidence(evidence), **extra}


def _amount(value, *, divisor=1):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        text = str(value).replace(",", "").strip()
        multiplier = 1000 if text.lower().endswith("k") else 1
        number = Decimal(text[:-1] if multiplier == 1000 else text) * multiplier / divisor
        if not number.is_finite() or number <= 0 or number > 1_000_000_000:
            return None
        return int(number) if number == number.to_integral_value() else float(number)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _period(text):
    matches = [name for name, pattern in _PERIODS if re.search(pattern, text, re.I)]
    return matches[0] if len(matches) == 1 else None


def _interval(value):
    if not isinstance(value, str):
        return None
    return {"per-hour": "hour", "per-year": "year", "per-month": "month", "per-week": "week",
            "per-day": "day", "hour": "hour", "year": "year", "month": "month", "week": "week", "day": "day"}.get(value)


def published_pay(payload: dict, ats: str) -> list[dict]:
    """Keep only documented, bounded salary fields from the exact official GET.

    Lever: public postings `salaryRange`; Greenhouse: `pay_input_ranges` when
    already present. Greenhouse supplies cents and no universal pay period, so
    a period must appear explicitly in its range title/blurb. The same bounded
    public GET can request pay transparency; no extra request is needed.
    """
    if not isinstance(payload, dict):
        return []
    if ats == "lever":
        raw = payload.get("salaryRange")
        if not isinstance(raw, dict):
            return []
        minimum, maximum = _amount(raw.get("min")), _amount(raw.get("max"))
        currency = raw.get("currency")
        currency = currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else None
        if minimum is None or maximum is None or minimum > maximum:
            return []
        return [{"minimum": minimum, "maximum": maximum, "currency": currency,
                 "currency_symbol": None, "period": _interval(raw.get("interval")), "kind": "pay",
                 "scope": "", "source": "Official Lever salaryRange"}]
    ranges = payload.get("pay_input_ranges")
    if ats != "greenhouse" or not isinstance(ranges, list):
        return []
    result = []
    for raw in ranges[:MAX_PAY_RANGES]:
        if not isinstance(raw, dict):
            continue
        currency = raw.get("currency_type")
        # Only these currencies have the two-decimal minor unit this adapter
        # converts. Other currencies remain in the posting, without guessing.
        if not isinstance(currency, str) or currency not in {"USD", "CAD", "AUD", "NZD", "SGD", "HKD", "EUR", "GBP"}:
            continue
        if type(raw.get("min_cents")) is not int or type(raw.get("max_cents")) is not int:
            continue
        minimum, maximum = _amount(raw.get("min_cents"), divisor=100), _amount(raw.get("max_cents"), divisor=100)
        if minimum is None or maximum is None or minimum > maximum:
            continue
        scope = _short(raw.get("title"), 80)
        detail = scope + " " + _short(raw.get("blurb"), 1000)
        result.append({"minimum": minimum, "maximum": maximum, "currency": currency, "currency_symbol": None,
                       "period": _period(detail), "kind": "pay", "scope": scope, "source": "Official Greenhouse pay_input_ranges"})
    return result


def _unit_excerpt(text, start, end):
    before = max(text.rfind("\n", 0, start), text.rfind(";", 0, start), text.rfind(". ", 0, start))
    after = [position for marker in ("\n", ";", ". ") if (position := text.find(marker, end)) >= 0]
    return _short(text[max(before + 1, start - 110):min(min(after) if after else len(text), end + 110)], MAX_EVIDENCE)


def _text_pay(text):
    result = []
    for match in _MONEY.finditer(text):
        if len(result) >= MAX_PAY_RANGES:
            break
        start, end = match.span()
        statement = _unit_excerpt(text, start, end)
        # Context cannot cross into the next sentence: company funding/assets
        # next to a generic salary promise do not establish applicant pay.
        if re.search(r"\b(?:million|billion|trillion|funding|fundrais\w*|revenue|assets?|budget|customer\s+spend)\b", statement, re.I):
            continue
        previous_lines = text[max(0, start - 120):start].rstrip().splitlines()
        heading = previous_lines[-1].strip(" :") if previous_lines and not text[text.rfind("\n", 0, start) + 1:start].strip() else ""
        pay_heading = bool(re.fullmatch(r"(?:(?:base|annual|hourly|monthly|weekly)\s+)?(?:salary|salary range|pay|pay range|compensation|wages?)", heading, re.I))
        if not re.search(r"\b(?:salary|salaries|compensation|wage|wages|pay|hourly)\b", statement, re.I) and not pay_heading:
            continue
        # These amounts are benefits/awards, not a salary range. In a clause
        # that explicitly identifies base pay, later bonus/equity prose does
        # not relabel that base figure.
        prefix = re.split(r"[;\n]|\. |\b(?:plus|and|alongside)\b", text[max(0, start - 100):start], flags=re.I)[-1]
        suffix = re.split(r"[;\n]|\. |\b(?:plus|and|alongside)\b", text[end:min(len(text), end + 80)], flags=re.I)[0]
        base = re.search(r"\bbase\s+(?:salary|pay|compensation)\b", prefix, re.I)
        total = re.search(r"\btotal\s+compensation\b", prefix, re.I)
        if not base and not total and re.search(r"\b(?:bonus|bonuses|equity|stock|shares?|stipend|allowance|reimbursement|wellness|401\s*\(?k)\b", prefix + " " + suffix, re.I):
            continue
        first, second = match.group("minimum"), match.group("maximum")
        minimum, maximum = _amount(first), _amount(second) if second is not None else _amount(first)
        if minimum is None or maximum is None or minimum > maximum:
            continue
        # A suffix on only one bound is ambiguous; don't turn 100-120k into
        # a fabricated 100,000 minimum or an absurd $100 minimum.
        if second and first.lower().endswith("k") != second.lower().endswith("k"):
            continue
        currencies = [re.sub(r"\s+", "", token.upper()) for token in (match.group("currency"), match.group("upper_currency"), match.group("suffix")) if token and token != "$"]
        codes = {_CURRENCIES.get(token) for token in currencies}
        codes.discard(None)
        if len(codes) > 1:
            continue
        currency = next(iter(codes)) if len(codes) == 1 else None
        kind = "base" if base else "total_compensation" if total else "pay"
        value = {"minimum": minimum, "maximum": maximum, "currency": currency,
                 "currency_symbol": ("$" if "$" in match.group("currency") else match.group("currency")) if not currency else None,
                 "period": _period(statement) or (_period(heading) if pay_heading else None), "kind": kind, "scope": "",
                 "source": "Posting text", "evidence": statement}
        if not any(all(previous.get(key) == value.get(key) for key in ("minimum", "maximum", "currency", "period", "kind")) for previous in result):
            result.append(value)
    return result


def _pay_summary(item):
    def number(value):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"
    amount = number(item["minimum"])
    if item["maximum"] != item["minimum"]:
        amount += "–" + number(item["maximum"])
    prefix = "Base pay: " if item.get("kind") == "base" else "Total compensation: " if item.get("kind") == "total_compensation" else ""
    summary = prefix + (item.get("currency") or item.get("currency_symbol") or "Currency not stated") + " " + amount
    summary += "/" + item["period"] if item.get("period") else " · period not stated"
    if not item.get("currency"):
        summary += " · currency not stated"
    if item.get("scope"):
        summary += " (" + item["scope"] + ")"
    return summary


def _pay_fact(job, description):
    raw = job.get("published_pay")
    items = []
    if isinstance(raw, list):
        for value in raw[:MAX_PAY_RANGES]:
            if not isinstance(value, dict):
                continue
            minimum, maximum = _amount(value.get("minimum")), _amount(value.get("maximum"))
            currency, period = value.get("currency"), value.get("period")
            if minimum is None or maximum is None or minimum > maximum:
                continue
            items.append({"minimum": minimum, "maximum": maximum,
                          "currency": currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else None,
                          "currency_symbol": None, "period": period if isinstance(period, str) and period in {"hour", "year", "month", "week", "day"} else None,
                          "kind": "pay", "scope": _short(value.get("scope"), 80), "source": _short(value.get("source"), 60)})
    if not items:
        items = _text_pay(_text(job.get("pay_description"), 4000) + "\n" + description)
    if not items:
        return _fact("Pay", "not_stated", "Pay range not clearly stated", ranges=[])
    evidence = [item.get("evidence") or item.get("source", "Published pay") + ": " + _pay_summary(item) for item in items]
    complete = all(item.get("currency") and item.get("period") for item in items)
    ranges = [{key: value for key, value in item.items() if key not in {"evidence", "source"}} for item in items]
    return _fact("Pay", "stated" if complete else "unclear", "; ".join(_pay_summary(item) for item in items), evidence, ranges=ranges)


def _months(value):
    return value if type(value) is int and 0 <= value <= 1200 else None


def _experience_fact(job, text, requirements):
    detail = job.get("score_detail")
    requirement = detail.get("experience_requirement") if isinstance(detail, dict) else None
    if not isinstance(requirement, dict) or not isinstance(requirement.get("status"), str) or requirement.get("status") not in {"known", "unknown", "unspecified"}:
        requirement = _experience_requirement(text, {"requirements": requirements})
    minimum, maximum = _months(requirement.get("minimum_months")), _months(requirement.get("maximum_months"))
    evidence = requirement.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    valid_upper = requirement.get("maximum_months") is None or maximum is not None
    if requirement.get("status") == "known" and minimum is not None and valid_upper and (maximum is None or minimum <= maximum):
        def duration(months):
            return f"{months // 12} {'year' if months == 12 else 'years'}" if months and months % 12 == 0 else f"{months} {'month' if months == 1 else 'months'}"
        summary = "No prior experience required" if minimum == 0 and maximum is None else (
            duration(minimum) + "–" + duration(maximum) + " required" if maximum is not None else "At least " + duration(minimum) + " required")
        return _fact("Experience", "stated", summary, evidence, minimum_months=minimum, maximum_months=maximum)
    if requirement.get("status") == "unknown" or evidence or requirement.get("status") == "known":
        return _fact("Experience", "unclear", "Experience requirement needs review", evidence, minimum_months=None, maximum_months=None)
    return _fact("Experience", "not_stated", "Numeric experience requirement not stated", minimum_months=None, maximum_months=None)


def job_facts(job: dict) -> dict:
    """Return five bounded fact entries from this job, without personal answers."""
    job = job if isinstance(job, dict) else {}
    description = _text(job.get("description"))
    title = _text(job.get("title"), 500)
    raw_requirements = job.get("requirements")
    requirements = [_text(value, 500) for value in raw_requirements[:100] if isinstance(value, str)] if isinstance(raw_requirements, list) else []
    units = _description_units(description) + [(value, 3) for value in requirements]
    location = _short(job.get("location"), 140)
    work_model = _short(job.get("work_model"), 35)
    location_summary = location + (" · " + work_model if work_model and work_model.casefold() not in location.casefold() else "")
    location_fact = _fact("Location", "stated" if location else "not_stated", location_summary or "Work location not stated",
                          [job.get("location_evidence") or location_summary])
    degrees = []
    for sentence, section in units:
        if not _DEGREE.search(sentence):
            continue
        if re.search(r"\b(?:our|the)\s+(?:team|founder|ceo|staff|employees|customers)\b", sentence, re.I) and section is None:
            continue
        if section is not None or re.search(r"\b(?:degree|preferred|required|must|minimum|pursuing|enrolled|earned|completed|hold|have|qualifications)\b|\b(?:B\.?S\.?|M\.?S\.?)\s+in\b", sentence, re.I):
            degrees.append(sentence)
    degree = _fact("Degree", "stated" if degrees else "not_stated", _short("; ".join(dict.fromkeys(degrees))) if degrees else "Degree requirement not stated", degrees)
    graduation = []
    for sentence, section in [(title, None)] + units:
        if not _GRADUATION.search(sentence):
            continue
        if re.search(r"\b(?:founders?|founded|established|our\s+(?:company|team|staff|employees)|company\s+history)\b", sentence, re.I):
            continue
        graduation_word = r"(?:graduat(?:e|es|ed|ion|ing)|(?:new|recent)[ -]+grads?|class\s+of)"
        bound_year = re.search(graduation_word + r"[^.;\n]{0,75}\b20\d{2}\b|\b20\d{2}\b[^.;\n]{0,35}" + graduation_word, sentence, re.I)
        relative = re.search(r"\b(?:within|past|last|next|at most|no more than)\s+(?:the\s+)?\d{1,2}\s+(?:months?|years?)\b[^.;\n]{0,45}" + graduation_word
                             + "|" + graduation_word + r"[^.;\n]{0,45}\b(?:within|past|last|next)\s+(?:the\s+)?\d{1,2}\s+(?:months?|years?)\b", sentence, re.I)
        applicant_context = (sentence == title or section is not None or re.search(r"\b(?:you|your|candidate|applicant|must|required|eligible|expected|graduating|between|before|after|since|within)\b|\bclass\s+of\s+20\d{2}\b", sentence, re.I))
        if (bound_year or relative) and applicant_context:
            graduation.append(sentence)
    graduation_fact = _fact("Graduation", "stated" if graduation else "not_stated",
                            "; ".join(dict.fromkeys(graduation)) if graduation else "Graduation window not stated", graduation)
    return {"location": location_fact, "degree": degree, "graduation": graduation_fact,
            "experience": _experience_fact(job, title + "\n" + description, requirements), "pay": _pay_fact(job, description)}
