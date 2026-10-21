"""Read-only compatibility inspection for SmartRecruiters hosted pages.

This module deliberately grants no application capability. SmartRecruiters'
public job advertisement, multi-step OneClick form, authenticated Application
API, and resume transport are distinct surfaces. Inspecting ordinary controls
does not establish a safe submission boundary for that platform.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .form_adapters import SNAPSHOT_JS


SMARTRECRUITERS_HOSTS = frozenset({"jobs.smartrecruiters.com", "www.smartrecruiters.com"})
_BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
_UUID = r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
_POSTING = re.compile(rf"^/({_BOARD})/([1-9][0-9]{{0,19}})(?:-[A-Za-z0-9][A-Za-z0-9_-]{{0,299}})?/?$")
_ONECLICK = re.compile(rf"^/oneclick-ui/company/({_BOARD})/publication/({_UUID})/?$")
_CHALLENGE = re.compile(r"please enable js and disable any ad blocker|verify (?:that )?you are human|security verification|checking your browser|complete (?:the |a )?captcha|captcha verification", re.I)
_CLOSED = re.compile(r"this job has expired|this position is no longer available|job (?:is|has been) closed", re.I)
_AGREEMENT = re.compile(r"\b(?:privacy|consent|agree(?:ment)?|terms|signature|certif(?:y|ication)|acknowledge)\b", re.I)
_NEXT = re.compile(r"^(?:next|continue|save and continue|review(?: application)?|personal information|screening questions)$", re.I)
_ORDINARY_TYPES = frozenset({"text", "email", "tel", "url", "number", "date", "month", "search", "textarea", "select", "radio", "checkbox", "file"})


def parse_smartrecruiters_url(url: str, *, fixture: bool = False) -> dict | None:
    """Identify a hosted posting/form without adding it to automatic support.

    Tracking queries are ignored for identity. Credentials, encoded paths,
    fragments, extra path segments and nonstandard production ports are rejected.
    Fixture URLs must be exact loopback HTTP with the same bounded path shapes.
    """
    if not isinstance(url, str) or not url or len(url) > 4000 or "\\" in url or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.fragment or unquote(parts.path) != parts.path:
            return None
        if fixture:
            if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
                return None
        elif parts.scheme != "https" or parts.hostname not in SMARTRECRUITERS_HOSTS or parts.port not in (None, 443):
            return None
        match = _ONECLICK.fullmatch(parts.path)
        page_type = "application"
        if not match:
            match, page_type = _POSTING.fullmatch(parts.path), "posting"
        if not match:
            return None
        return {"platform": "smartrecruiters", "host": parts.hostname,
                "company": match[1], "page_type": page_type,
                "posting_id": match[2].lower(),
                "canonical_url": f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"}
    except (TypeError, ValueError):
        return None


def smartrecruiters_capability() -> dict:
    """A UI-ready capability summary; no network request or profile needed."""
    return {
        "platform": "smartrecruiters", "mode": "inspection_only",
        "automatic_submission_supported": False,
        "reason": "SmartRecruiters requires a separate multi-step form and upload/consent adapter. Automatic submission is not enabled.",
        "next_action": "Open the employer form to complete it manually; Jobot can retain its queue/history record.",
        "documentation_url": "https://developers.smartrecruiters.com/docs/application-api",
    }


def _text(value, limit=240) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def inspect_smartrecruiters_snapshot(url: str, snapshot: dict, *, status_code: int | None = None, fixture: bool = False) -> dict:
    """Describe observed fields/gates without copying entered answers or body.

    The snapshot uses the existing form adapter's public DOM metadata. Values,
    selected files, request URLs, document text and credentials are never returned.
    Dynamic dropdowns are only reported when their actual options are visible;
    the inspector never opens menus, advances pages, fills or submits a form.
    """
    identity = parse_smartrecruiters_url(url, fixture=fixture)
    if not identity:
        raise ValueError("Not a bounded SmartRecruiters hosted page URL.")
    if not isinstance(snapshot, dict):
        raise ValueError("Form snapshot must be an object.")
    report = {**smartrecruiters_capability(), "page_type": identity["page_type"],
              "stage": identity["page_type"], "gates": [], "fields": [],
              "unsupported_controls": [], "navigation_buttons": []}
    fields = snapshot.get("fields", [])
    if not isinstance(fields, list):
        fields = []
    for field in fields[:500]:
        if not isinstance(field, dict) or field.get("disabled") or field.get("visible") is False:
            continue
        kind = _text(field.get("type"), 30).lower() or "text"
        if field.get("tag") == "select" or kind in {"select-one", "select-multiple"}:
            kind = "select"
        if kind in {"hidden", "submit", "button", "reset", "image"}:
            continue
        label = _text(field.get("label")) or "Unlabelled field"
        options = field.get("options", [])
        if not isinstance(options, list):
            options = []
        public = {"label": label, "type": kind, "required": field.get("required") is True,
                  "multiple": field.get("multiple") is True,
                  "options": [{"label": _text(o.get("label")), "disabled": o.get("disabled") is True}
                              for o in options[:300] if isinstance(o, dict) and _text(o.get("label"))]}
        report["fields"].append(public)
        reasons = []
        if kind not in _ORDINARY_TYPES:
            reasons.append("unsupported_control")
        if public["multiple"]:
            reasons.append("multiple_selection_requires_review")
        if field.get("combobox") and not public["options"]:
            reasons.append("dynamic_choices_not_observed")
        if label == "Unlabelled field":
            reasons.append("missing_accessible_label")
        if reasons:
            report["unsupported_controls"].append({"label": label, "reasons": reasons})
    body = snapshot.get("body", "")
    body = body[:160000] if isinstance(body, str) else ""
    buttons = snapshot.get("navigation_buttons", [])
    if isinstance(buttons, list):
        report["navigation_buttons"] = [_text(b, 80) for b in buttons[:30] if isinstance(b, str) and _text(b, 80)]
    gates = report["gates"]
    if status_code in {401, 403, 429}:
        gates.append("access_or_rate_limit")
    if snapshot.get("captcha") is True or _CHALLENGE.search(body):
        gates.append("anti_bot_challenge")
    if snapshot.get("login") is True or any(f["type"] == "password" for f in report["fields"]):
        gates.append("account_or_login")
    if any(_AGREEMENT.search(f["label"]) for f in report["fields"]):
        gates.append("consent_or_agreement")
    if any(_NEXT.fullmatch(b) for b in report["navigation_buttons"]):
        gates.append("multi_step_flow")
    if _CLOSED.search(body):
        report["stage"] = "closed"
        report["next_action"] = "The posting is closed. Do not attempt an application."
    elif "anti_bot_challenge" in gates:
        report["stage"] = "challenge"
        report["next_action"] = "The public page is blocked by a browser challenge. Complete it manually; the inspector does not bypass it."
    elif "account_or_login" in gates:
        report["stage"] = "login"
        report["next_action"] = "Sign in or register on the employer form manually. Credentials are not read or stored by this inspector."
    elif "access_or_rate_limit" in gates:
        report["stage"] = "unavailable"
        report["next_action"] = "The public page is unavailable or rate limited. Stop inspection and retry manually later."
    elif "consent_or_agreement" in gates:
        report["stage"] = "agreement"
    elif report["fields"]:
        report["stage"] = "application"
    elif identity["page_type"] == "application":
        report["stage"] = "unrecognized"
        report["next_action"] = "No usable form controls were observed. Open the employer form manually."
    return report


_EXTRA_SNAPSHOT_JS = r"""() => {
  const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none' && getComputedStyle(el).opacity !== '0');
  const text = el => (el.innerText || el.value || el.textContent || '').replace(/\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('button,input[type=button],input[type=submit]'))
    .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
    .slice(0,30).map(text);
}"""


async def inspect_smartrecruiters_page(page, url: str, *, status_code: int | None = None, fixture: bool = False) -> dict:
    """Inspect an already-open page. This function does not navigate or act.

    Callers remain responsible for their read-only browser network boundary.
    The supplied URL must identify the exact current page, so a redirect to a
    different posting or account portal cannot masquerade as the requested form.
    """
    expected = parse_smartrecruiters_url(url, fixture=fixture)
    observed = parse_smartrecruiters_url(page.url, fixture=fixture)
    if not expected or observed != expected:
        raise ValueError("SmartRecruiters inspection page changed destination.")
    snapshot = await page.evaluate(SNAPSHOT_JS, "smartrecruiters-inspection")
    snapshot["navigation_buttons"] = await page.evaluate(_EXTRA_SNAPSHOT_JS)
    # Shared snapshots read native select choices. Visible custom listboxes
    # need no click: read only the listbox controlled by each observed input.
    visible_options = await page.evaluate(r"""() => {
      const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none' && getComputedStyle(el).opacity !== '0');
      const result = {};
      document.querySelectorAll('[role=combobox][data-jobradar-field]').forEach(input => {
        const ids = ((input.getAttribute('aria-controls') || '') + ' ' + (input.getAttribute('aria-owns') || '')).split(/\s+/).filter(Boolean);
        const nodes = ids.map(id => document.getElementById(id)).filter(node => node && visible(node));
        const options = nodes.flatMap(node => Array.from(node.querySelectorAll('[role=option]'))).filter(visible);
        result[input.getAttribute('data-jobradar-field')] = options.slice(0,300).map(option => ({
          label:(option.innerText || option.textContent || '').replace(/\s+/g, ' ').trim(),
          disabled:option.getAttribute('aria-disabled') === 'true'}));
      });
      return result;
    }""")
    for field in snapshot.get("fields", []):
        if field.get("combobox"):
            field["options"] = visible_options.get(field["id"], [])
    return inspect_smartrecruiters_snapshot(url, snapshot, status_code=status_code, fixture=fixture)
