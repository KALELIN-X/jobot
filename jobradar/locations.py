"""Match the owner's location preferences without treating 'remote' as worldwide."""
from __future__ import annotations

import re


_US_COUNTRY = re.compile(r"\b(?:united states(?: of america)?|u\.?s\.?a?\.?)\b", re.I)
_FOREIGN_COUNTRY = re.compile(
    r"\b(?:canada|united kingdom|germany|ireland|france|india|australia|"
    r"new zealand|singapore|netherlands|poland|spain|mexico|brazil|japan|china|"
    r"england|scotland|wales|northern ireland|uk|gb|europe|emea|apac|asia)\b", re.I,
)
_NON_LOCATION_REFERENCE = re.compile(
    r"(?:" + _US_COUNTRY.pattern + r"|" + _FOREIGN_COUNTRY.pattern + r")(?:\s*[-–—]?\s*)(?:(?:business|working?|office|core)\s+)?"
    r"(?:hours?|time[\s-]*zones?|time\s+overlap|customers?|clients?|markets?|holidays?)(?:\b|$)"
    r"|(?:" + _US_COUNTRY.pattern + r"|" + _FOREIGN_COUNTRY.pattern + r")[\s-]+based[\s-]+(?:team|company|employer|clients?|customers?)\b",
    re.I,
)
_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
    "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
    "Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
    "New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
    "Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|"
    "Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia"
)
_QUALIFIED_STATE = re.compile(r",\s*(?:" + "|".join(_STATE_CODES) + r")(?:\s+\d{5}(?:-\d{4})?)?(?=\s*(?:[,;/()|–—-]|$))")
_FULL_STATE = re.compile(r"(?:,\s*|\b)(?:" + _STATE_NAMES + r")(?=\s*(?:[,;/()|–—-]|$))", re.I)


def _normal(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def country_is_us(job: dict) -> bool:
    code = str(job.get("country_code") or "").strip().upper()
    if code:
        return code == "US"
    location = str(job.get("location") or "")
    # Evaluate explicit alternatives separately. A Canadian location's US-hour
    # annotation is not a US work location, while a separate Boston office is.
    parts = re.split(r"[;\n|]|\s+/\s+|\s+or\s+|\s+&\s+", location, flags=re.I)
    for part in parts:
        location_text = _NON_LOCATION_REFERENCE.sub(" ", part)
        if _FOREIGN_COUNTRY.search(location_text):
            continue
        if _US_COUNTRY.search(location_text):
            return True
        if _normal(location_text) == "georgia":
            continue  # Country/state ambiguity needs an explicit city or country.
        # Common international country-code locations must not become U.S. states.
        if re.search(r"\b(?:Berlin|Munich|Hamburg|Toronto|Vancouver|Montreal|Ottawa|Mumbai|Delhi|Bangalore)\s*,", location_text, re.I):
            continue
        if _QUALIFIED_STATE.search(location_text) or _FULL_STATE.search(location_text):
            return True
    return False


def location_matches(job: dict, allowed_locations: list[str]) -> bool:
    if not allowed_locations:
        return True
    actual = _normal(str(job.get("location") or "") + " " + str(job.get("work_model") or ""))
    for location in allowed_locations:
        wanted = _normal(location)
        if not wanted:
            continue
        if wanted in {"us", "u s", "usa", "u s a", "united states", "united states of america"}:
            if country_is_us(job):
                return True
        elif re.search(r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", actual):
            return True
    return False
