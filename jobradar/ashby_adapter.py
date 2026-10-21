"""Read-only Ashby preflight and visible-control helpers.

This module does not grant automatic submission support. Ashby's hosted form
persists answers through render-bound mutations while the applicant types;
those writes need a dedicated transport rather than a generic POST exception.
No authenticated Ashby API, React internals, or candidate data is read here.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .form_adapters import SNAPSHOT_JS


ASHBY_HOST = "jobs.ashbyhq.com"
_BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_POSTING = re.compile(rf"^/({_BOARD})/({_UUID})(?:/application)?/?$")
MAX_PUBLIC_RESPONSE_BYTES = 5_000_000


def parse_ashby_destination(url: str) -> dict | None:
    """Identify one HTTPS hosted posting, preserving its actual board casing."""
    if (not isinstance(url, str) or not url or len(url) > 4000
            or any(char.isspace() for char in url) or "\\" in url):
        return None
    try:
        target = urlsplit(url)
        if (target.scheme != "https" or target.hostname != ASHBY_HOST
                or target.port not in (None, 443) or target.username or target.password
                or target.fragment or unquote(target.path) != target.path):
            return None
        match = _POSTING.fullmatch(target.path)
        if not match:
            return None
        board, posting = match[1], match[2].lower()
        origin = f"https://{ASHBY_HOST}"
        canonical = f"{origin}/{board}/{posting}"
        return {"ats": "ashby", "host": ASHBY_HOST, "origin": origin,
                "board": board, "posting_id": posting, "canonical_url": canonical,
                "apply_url": canonical + "/application",
                "application_key": f"ashby:{board.casefold()}/{posting}"}
    except (TypeError, ValueError):
        return None


def public_ashby_board_url(url: str) -> str | None:
    """Derive only the documented public, read-only job board endpoint."""
    posting = parse_ashby_destination(url)
    if not posting:
        return None
    return (f"https://api.ashbyhq.com/posting-api/job-board/{posting['board']}"
            "?includeCompensation=true")


def ashby_document_allowed(url: str, initial_url: str) -> bool:
    """Allow the description/application of the exact posting, never another."""
    initial = parse_ashby_destination(initial_url)
    current = parse_ashby_destination(url)
    return bool(initial and current and current["application_key"] == initial["application_key"]
                and current["board"] == initial["board"])


def verified_public_ashby_posting(url: str, payload: dict) -> dict | None:
    """Return one matching public listing, failing closed on conflicting rows.

    The source is Ashby's documented public Job Postings API, not an employer
    API key. A matching ID alone is insufficient: both hosted URLs must bind to
    the same exact board and posting. This is discovery evidence, not approval
    to transmit a résumé or submit an application.
    """
    target = parse_ashby_destination(url)
    if (not target or not isinstance(payload, dict) or payload.get("apiVersion") != "1"
            or not isinstance(payload.get("jobs"), list) or len(payload["jobs"]) > 5000):
        return None
    matches = []
    for row in payload["jobs"]:
        if not isinstance(row, dict):
            continue
        job_url, apply_url = row.get("jobUrl"), row.get("applyUrl")
        posting, application = parse_ashby_destination(job_url), parse_ashby_destination(apply_url)
        if not posting or posting["application_key"] != target["application_key"]:
            continue
        if (not application or application["application_key"] != target["application_key"]
                or posting["board"] != target["board"] or application["board"] != target["board"]
                or row.get("isListed") is not True or not isinstance(row.get("title"), str)
                or not row["title"].strip() or len(row["title"]) > 1000):
            return None
        matches.append(row)
    return dict(matches[0]) if len(matches) == 1 else None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.scripts = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.current = []

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append("".join(self.current))
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.current.append(data)


def verify_ashby_document(url: str, html: str) -> bool:
    """Check public JSON page identity without executing embedded JavaScript."""
    target = parse_ashby_destination(url)
    if not target or not isinstance(html, str) or len(html.encode("utf-8")) > MAX_PUBLIC_RESPONSE_BYTES:
        return False
    parser = _Scripts()
    try:
        parser.feed(html)
        values = []
        decoder = json.JSONDecoder()
        for script in parser.scripts:
            match = re.match(r"\s*window\.__appData\s*=\s*", script)
            if not match:
                continue
            value, end = decoder.raw_decode(script[match.end():])
            if not script[match.end() + end:].lstrip().startswith(";"):
                return False
            values.append(value)
        if len(values) != 1 or not isinstance(values[0], dict):
            return False
        organization, posting = values[0].get("organization"), values[0].get("posting")
        return bool(isinstance(organization, dict) and isinstance(posting, dict)
                    and organization.get("hostedJobsPageSlug") == target["board"]
                    and isinstance(posting.get("id"), str)
                    and posting["id"].lower() == target["posting_id"]
                    and posting.get("isListed") is True
                    and isinstance(posting.get("title"), str) and posting["title"].strip()
                    and not values[0].get("maintenanceMode"))
    except (TypeError, ValueError, RecursionError):
        return False


# This inspects only the visible DOM. Ashby's Yes/No uses aria-pressed buttons
# and a hidden checkbox; treating an unchecked proxy as a confirmed No loses
# the difference between No and unanswered. Keep the group as one choice field.
ASHBY_SNAPSHOT_JS = r"""(token) => {
  const snapshot = (__BASE__)(token);
  const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none' && getComputedStyle(el).opacity !== '0');
  const text = el => (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
  for (const field of snapshot.fields) {
    const element = document.querySelector('[data-jobradar-field="'+field.id+'"]');
    const entry = element?.closest('.ashby-application-form-field-entry');
    const title = entry?.querySelector('.ashby-application-form-question-title');
    if (title) {
      field.label = text(title);
      field.required = field.required || /[*✱∗]\s*$|\(required\)\s*$/i.test(field.label);
      if (field.type==='file' && visible(entry) && visible(title)) field.visible = true;
    }
  }
  const proxies = new Set();
  document.querySelectorAll('.ashby-application-form-field-entry').forEach((entry,index) => {
    const buttons = [...entry.querySelectorAll('button[data-option][aria-pressed]')].filter(visible);
    if (buttons.length !== 2 || buttons.filter(button => button.dataset.option==='yes').length !== 1 || buttons.filter(button => button.dataset.option==='no').length !== 1) return;
    const checkbox = entry.querySelector('input[type=checkbox][tabindex="-1"]');
    if (!checkbox) return;
    const label = text(entry.querySelector('.ashby-application-form-question-title'));
    if (!label) return;
    const id = token+'-ashby-yesno-'+index;
    const selected = buttons.filter(button => button.getAttribute('aria-pressed')==='true');
    proxies.add(checkbox.getAttribute('data-jobradar-field'));
    entry.setAttribute('data-jobradar-field',id);
    snapshot.fields.push({id,name:checkbox.name || '',label,option_label:'',type:'select',tag:'button',
      combobox:false,ashby_yesno:true,selected_label:selected.length===1 ? text(selected[0]) : '',
      searchable:false,lookup_required:false,required:/[*✱∗]\s*$|\(required\)\s*$/i.test(label),
      disabled:buttons.every(button=>button.disabled || button.getAttribute('aria-disabled')==='true'),
      visible:true,value:selected.length===1 ? text(selected[0]) : '',multiple:false,
      options:buttons.map(button=>({value:text(button),label:text(button),disabled:button.disabled || button.getAttribute('aria-disabled')==='true'}))});
  });
  snapshot.fields = snapshot.fields.filter(field=>!proxies.has(field.id));
  return snapshot;
}""".replace("__BASE__", SNAPSHOT_JS)


async def choose_ashby_yesno(page, field_id: str, label: str) -> None:
    """Click a unique, visible Yes/No button and verify the committed state.

    This helper grants no network capability. A live Ashby caller must first
    implement an exact render/field-bound mutation guard before using it.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,150}", field_id) or label not in {"Yes", "No"}:
        raise ValueError("The Ashby Yes/No answer is not supported.")
    entry = page.locator(f'[data-jobradar-field="{field_id}"]')
    options = entry.locator(f'button[data-option="{label.lower()}"][aria-pressed]')
    if await entry.count() != 1 or await options.count() != 1 or not await options.is_visible():
        raise ValueError("The Ashby choice is missing or ambiguous.")
    if not await options.is_enabled() or await options.get_attribute("aria-disabled") == "true":
        raise ValueError("The Ashby choice is disabled.")
    await options.click(timeout=3000)
    await page.wait_for_function("""({fieldId, option}) => {
      const entry=document.querySelector('[data-jobradar-field="'+fieldId+'"]');
      const buttons=[...(entry?.querySelectorAll('button[data-option][aria-pressed]') || [])];
      const chosen=buttons.filter(button=>button.getAttribute('aria-pressed')==='true');
      return chosen.length===1 && chosen[0].dataset.option===option;
    }""", arg={"fieldId": field_id, "option": label.lower()}, timeout=3000)


def inspect_ashby_snapshot(url: str, snapshot: dict, *, document_verified: bool = False) -> dict:
    """Return value-free compatibility metadata; never return candidate input.

    The caller must prove the exact public page identity before marking the
    inspection verified. This report deliberately cannot claim fill/submit
    support merely because a field looks familiar.
    """
    destination = parse_ashby_destination(url)
    report = {"platform": "ashby", "stage": "inspection_only", "gates": [],
              "fields": [], "automatic_submission_supported": False,
              "next_action": "Open the employer form to complete this application. Ashby automatic submission is not enabled."}
    if not destination:
        return {**report, "stage": "unsupported_url", "gates": ["unverified_destination"],
                "next_action": "Use an exact jobs.ashbyhq.com posting or application URL."}
    if not document_verified:
        return {**report, "stage": "unverified", "gates": ["unverified_destination"],
                "next_action": "The public page could not be bound to this exact posting. Open it for review."}
    if not isinstance(snapshot, dict):
        return {**report, "stage": "unavailable", "gates": ["form_unavailable"]}
    fields = snapshot.get("fields")
    fields = fields if isinstance(fields, list) else []
    for field in fields[:300]:
        if not isinstance(field, dict) or not field.get("visible"):
            continue
        label = field.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        options = []
        raw_options = field.get("options")
        raw_options = raw_options if isinstance(raw_options, list) else []
        for option in raw_options[:300]:
            if isinstance(option, dict) and isinstance(option.get("label"), str):
                options.append({"label": option["label"][:500], "disabled": bool(option.get("disabled"))})
        report["fields"].append({"label": label[:1000], "type": str(field.get("type", "unknown"))[:40],
                                 "required": bool(field.get("required")), "disabled": bool(field.get("disabled")),
                                 "multiple": bool(field.get("multiple")), "options": options})
    if snapshot.get("login"):
        report["gates"].append("login")
    if snapshot.get("captcha"):
        report["gates"].append("verification")
    legal = re.compile(r"\b(?:agree|agreement|accept|consent|acknowledge|certify|signature|terms|privacy)\b", re.I)
    if any(field["required"] and legal.search(field["label"]) for field in report["fields"]):
        report["gates"].append("agreement_review")
    if not report["fields"]:
        report["stage"] = "unavailable"
        report["gates"].append("form_unavailable")
    elif report["gates"]:
        report["stage"] = "user_action_required"
    return report


async def inspect_ashby_page(page, url: str) -> dict:
    """Inspect an already-open page; this function never navigates or fills it."""
    if not ashby_document_allowed(page.url, url):
        return inspect_ashby_snapshot(url, {}, document_verified=False)
    verified = verify_ashby_document(url, await page.content())
    if not verified:
        return inspect_ashby_snapshot(url, {}, document_verified=False)
    snapshot = await page.evaluate(ASHBY_SNAPSHOT_JS, "ashby-inspect")
    return inspect_ashby_snapshot(url, snapshot, document_verified=True)
