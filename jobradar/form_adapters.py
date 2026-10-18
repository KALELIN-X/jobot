"""Bounded DOM support for ordinary and Greenhouse React-select forms.

No React internals, hidden answer stores, private API calls, or fabricated
coordinates are used. Custom choices are read from the visible listbox and
selected by a real click; HTML validity remains authoritative.
"""

from __future__ import annotations

import uuid
import asyncio
import hashlib
import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .destinations import parse_destination


class GreenhouseTransport:
    """Capabilities advertised by this exact public hosted form, kept in memory.

    The hosted renderer uses boards.greenhouse.io for its posting-bound form
    POST, and presigned upload fields for one Greenhouse-owned S3 endpoint.
    This never calls the employer-authenticated boards-api submission API.
    """

    US_UPLOAD_HOST = "grnhse-prod-jben-us-east-1.s3.amazonaws.com"

    def __init__(self, initial_url: str, resume_path: Path | None, *, local_fixture=None):
        parsed = parse_destination(initial_url)
        self.enabled = bool(parsed and parsed["ats"] == "greenhouse") or local_fixture is not None
        self.initial_url = initial_url
        self.fixture = local_fixture is not None
        if local_fixture is not None:
            self.submit_url = local_fixture["submit_url"]
            self.fixture_upload_url = local_fixture["upload_url"]
            for value in (self.submit_url, self.fixture_upload_url):
                target = urlsplit(value)
                if target.scheme != "http" or target.hostname != "127.0.0.1" or target.port is None or target.username or target.password or target.query or target.fragment:
                    raise ValueError("Local transport fixtures require exact loopback endpoints.")
            if not re.fullmatch(r"/[A-Za-z0-9_-]+/jobs/[1-9][0-9]*", urlsplit(self.submit_url).path) or urlsplit(self.fixture_upload_url).path != "/upload":
                raise ValueError("Local transport fixture paths are invalid.")
        elif self.enabled:
            region = "boards.eu.greenhouse.io" if ".eu." in parsed["host"] else "boards.greenhouse.io"
            self.submit_url = f"https://{region}/{parsed['board']}/jobs/{parsed['posting_id']}"
            self.fixture_upload_url = None
        else:
            self.submit_url = ""
            self.fixture_upload_url = None
        target = urlsplit(self.submit_url)
        self.metadata_url = f"{target.scheme}://{target.netloc}/uncacheable_attributes/presigned_fields" if self.enabled else ""
        self.form_verified = False
        self.document_ready = asyncio.Event()
        self.upload_ready = asyncio.Event()
        self.upload_done = asyncio.Event()
        self.upload_active = False
        self.upload_success = False
        self.upload_fields = None
        self.upload_key = None
        self.upload_url = None
        self.resume_path = Path(resume_path) if resume_path else None
        self.resume_sha = hashlib.sha256(self.resume_path.read_bytes()).hexdigest() if self.resume_path else None

    def verify_document(self, html: str) -> bool:
        paths = set(re.findall(r'\\?"submitPath\\?"\s*:\s*\\?"([^"\\]+)\\?"', html))
        self.form_verified = self.enabled and len(html) <= 3_000_000 and paths == {self.submit_url}
        self.document_ready.set()
        return self.form_verified

    def accept_upload_fields(self, value: dict) -> bool:
        if not self.form_verified or not isinstance(value, dict):
            return False
        endpoint = value.get("url")
        resume = value.get("resume")
        if not isinstance(endpoint, str) or not isinstance(resume, dict):
            return False
        target = urlsplit(endpoint)
        valid_endpoint = endpoint.rstrip("/") == self.fixture_upload_url if self.fixture else (
            target.scheme == "https" and target.hostname == self.US_UPLOAD_HOST and target.port in (None, 443)
            and target.path in {"", "/"} and not target.query and not target.fragment and not target.username and not target.password)
        fields, key = resume.get("fields"), resume.get("key")
        if not valid_endpoint or not isinstance(fields, dict) or not fields or len(fields) > 30 or not all(isinstance(k, str) and isinstance(v, str) and len(k) < 100 and len(v) < 20000 for k, v in fields.items()):
            return False
        if not isinstance(key, str) or not key or len(key) > 1000 or key.startswith("/") or ".." in key.split("/"):
            return False
        self.upload_fields, self.upload_key, self.upload_url = dict(fields), key, endpoint.rstrip("/")
        self.upload_ready.set()
        return True

    async def observe(self, response):
        if not self.enabled:
            return
        try:
            request = response.request
            if request.is_navigation_request() and response.url == self.initial_url and response.status == 200:
                self.verify_document(await response.text())
            target = urlsplit(response.url)
            base = f"{target.scheme}://{target.netloc}{target.path}"
            if request.method == "GET" and base == self.metadata_url and response.status == 200:
                await asyncio.wait_for(self.document_ready.wait(), 3)
                queries = parse_qs(target.query)
                if set(queries) == {"fields[]"} and "resume" in queries["fields[]"] and set(queries["fields[]"]) <= {"resume", "cover_letter"}:
                    body = await response.body()
                    if len(body) <= 128000:
                        self.accept_upload_fields(json.loads(body))
            if self.upload_active and self.upload_url and response.url.rstrip("/") == self.upload_url and request.method == "POST":
                self.upload_success = 200 <= response.status < 300
                self.upload_done.set()
        except Exception:
            # Unavailable/malformed capability metadata never broadens transport.
            return

    def is_submit_url(self, url: str) -> bool:
        return self.form_verified and url.rstrip("/") == self.submit_url

    def permits_upload(self, url: str, method: str, headers: dict, content: bytes | None) -> bool:
        if not (self.upload_active and self.form_verified and self.upload_url and self.resume_path and method == "POST" and url.rstrip("/") == self.upload_url and isinstance(content, bytes) and len(content) <= 8_000_000):
            return False
        content_type = headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data;"):
            return False
        try:
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + content)
            values, files = {}, []
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "file":
                    files.append(part)
                elif name and name not in values:
                    values[name] = part.get_payload(decode=True).decode("utf-8")
                else:
                    return False
            if len(files) != 1 or files[0].get_filename() != self.resume_path.name or hashlib.sha256(files[0].get_payload(decode=True)).hexdigest() != self.resume_sha:
                return False
            expected = self.upload_fields
            if not expected or any(values.get(name) != value for name, value in expected.items()):
                return False
            extras = set(values) - set(expected)
            if not extras <= {"key", "utf8", "authenticity_token", "Content-Type"}:
                return False
            fixed = {"utf8": "✓", "authenticity_token": "1234", "Content-Type": "application/octet-stream"}
            if any(name in values and values[name] != value for name, value in fixed.items()):
                return False
            key_pattern = re.escape(self.upload_key).replace(re.escape("{timestamp}"), r"\d{10,16}").replace(re.escape("{unique_id}"), "[a-z0-9]{1,20}")
            return bool(re.fullmatch(key_pattern, values.get("key", "")))
        except Exception:
            return False

    def prepare_upload(self, url: str, method: str, headers: dict, content: bytes | None) -> bytes | None:
        """Validate the actual browser body, restoring its selected file if CDP omitted it.

        Chromium's interception protocol intentionally omits file-backed multipart
        bytes. Only during our selected-resume stage may that empty, correctly
        named part be restored from the exact supplied file; the complete rebuilt
        body must still pass every endpoint, field, and file-hash constraint.
        """
        if self.permits_upload(url, method, headers, content):
            return content
        if not (self.upload_active and self.form_verified and self.resume_path and isinstance(content, bytes) and len(content) <= 8_000_000):
            return None
        content_type = headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data;"):
            return None
        try:
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + content)
            files = [part for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "file"]
            if len(files) != 1 or files[0].get_filename() != self.resume_path.name or files[0].get_payload(decode=True) != b"":
                return None
            boundary = message.get_boundary()
            if not boundary or len(boundary) > 200 or any(ord(char) < 32 or ord(char) > 126 for char in boundary):
                return None
            chunks = []
            for part in message.iter_parts():
                if part.get("Content-Transfer-Encoding", "").lower() not in {"", "binary", "8bit"}:
                    return None
                header = part.as_bytes(policy=policy.SMTP).split(b"\r\n\r\n", 1)[0]
                payload = self.resume_path.read_bytes() if part is files[0] else part.get_payload(decode=True)
                # Keep arbitrary PDF/DOCX bytes unchanged. MIME generators may
                # normalize newlines when serializing a binary payload.
                chunks.append(b"--" + boundary.encode("ascii") + b"\r\n" + header + b"\r\n\r\n" + payload + b"\r\n")
            rebuilt = b"".join(chunks) + b"--" + boundary.encode("ascii") + b"--\r\n"
            return rebuilt if self.permits_upload(url, method, headers, rebuilt) else None
        except Exception:
            return None

    async def begin_upload(self):
        if not self.enabled:
            return
        await asyncio.wait_for(self.upload_ready.wait(), 5)
        self.upload_success = False
        self.upload_done.clear()
        self.upload_active = True

    async def finish_upload(self):
        try:
            await asyncio.wait_for(self.upload_done.wait(), 20)
            if not self.upload_success:
                raise ValueError("The advertised resume upload did not succeed.")
        finally:
            self.upload_active = False


SNAPSHOT_JS = r"""(token) => {
  const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none' && getComputedStyle(el).opacity !== '0');
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const labelled = el => (el?.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => text(document.getElementById(id))).filter(Boolean).join(' ');
  const all = Array.from(document.querySelectorAll('input,select,textarea,[role="combobox"]')).filter((el,i,items) => items.indexOf(el) === i);
  // Greenhouse/React-select uses an aria-hidden required proxy next to its
  // visible combobox. Do not answer that proxy; native checkValidity must still
  // validate it after the real option click.
  const proxy = el => el.getAttribute('aria-hidden') === 'true' && el.tabIndex === -1 && /requiredInput/.test(el.className || '') && !!el.parentElement?.querySelector('[role="combobox"]');
  const controls = all.filter(el => !proxy(el));
  const fields = controls.map((el, i) => {
    el.setAttribute('data-jobradar-field', token + '-' + i);
    const box = el.closest('.application-question,.form-field,.field,.form-group,.field-wrapper,.select__container,fieldset');
    const group = el.closest('[role="group"][aria-labelledby],fieldset');
    const associated = Array.from(el.labels || []).map(text).filter(Boolean).join(' ');
    const question = box && box.querySelector('legend,.application-label,.application-question-label,.field-label');
    const groupLabel = labelled(group) || text(group?.querySelector('legend'));
    const isCombo = el.getAttribute('role') === 'combobox';
    const comboRoot = isCombo ? el.closest('.select__container,[class*="select-container"],.react-select,.form-field,.field-wrapper') || el.parentElement?.parentElement : null;
    const selected = comboRoot?.querySelector('.select__single-value,[class*="singleValue"],[data-selected-value]');
    const label = el.type === 'file' ? (groupLabel || associated || labelled(el) || el.id) :
      el.type === 'radio' ? (groupLabel || text(question) || labelled(el) || el.getAttribute('aria-label') || associated) :
      (associated || labelled(el) || el.getAttribute('aria-label') || text(question));
    return {id: token + '-' + i, name: el.name || el.id || '', label: label || '',
      option_label: associated, type: isCombo ? 'select' : el.type || el.tagName.toLowerCase(), tag: el.tagName.toLowerCase(),
      combobox: isCombo, selected_label: isCombo ? text(selected) : '',
      required: !!(el.required || el.getAttribute('aria-required') === 'true' ||
        /[*✱∗]\s*$|\(required\)\s*$/i.test(label || '') ||
        (el.type === 'file' && group?.getAttribute('aria-required') === 'true') ||
        (box && (box.classList.contains('required') || !!box.querySelector('.required')))),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'), visible: visible(el),
      value: el.value || '', checked: !!el.checked, has_file: !!(el.files && el.files.length),
      multiple: !!(el.multiple || el.getAttribute('aria-multiselectable') === 'true'),
      options: el.options ? Array.from(el.options).slice(0,300).map(o => ({value: o.value, label: text(o), disabled: o.disabled})) : []};
  });
  const forms = Array.from(document.forms).filter(visible).map(f => ({
    action: f.action || location.href, method: (f.method || 'get').toLowerCase(),
    fields: controls.filter(c => c.form === f || f.contains(c)).map(c => c.getAttribute('data-jobradar-field'))
  }));
  const candidates = Array.from(document.querySelectorAll('button,input[type=submit]')).filter(el => {
    if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (forms.length && !(el.form || el.closest('form'))) return false;
    return /^(?:submit(?: application)?|submit your application|apply|send application)$/i.test((text(el) || el.value || '').trim());
  });
  candidates.forEach((el, i) => el.setAttribute('data-jobradar-submit', token + '-' + i));
  return {fields, forms, buttons: candidates.map((el, i) => ({id: token + '-' + i,
    action: el.formAction || (el.form && el.form.action) || location.href,
    form_fields: el.form ? controls.filter(c => c.form === el.form || el.form.contains(c)).map(c => c.getAttribute('data-jobradar-field')) : []})),
    captcha: !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[title*="CAPTCHA"],iframe[title*="captcha"],.g-recaptcha,.h-captcha,[data-sitekey]'),
    login: controls.some(el => el.type === 'password' && visible(el)) || Array.from(document.querySelectorAll('h1,h2')).some(el => visible(el) && /^(?:log in|login|sign in)(?: to .*)?$/i.test(text(el))),
    body: document.body ? document.body.innerText : ''};
}"""


CHOICES_JS = r"""({fieldId, token}) => {
  const input = document.querySelector('[data-jobradar-field="' + fieldId + '"]');
  if (!input) return {options:[], supported:false};
  const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
  const ids = (input.getAttribute('aria-controls') || input.getAttribute('aria-owns') || '').split(/\s+/).filter(Boolean);
  const linked = ids.map(id => document.getElementById(id)).filter(el => el && visible(el) && el.getAttribute('role') === 'listbox');
  const boxes = linked.length ? linked : Array.from(document.querySelectorAll('[role="listbox"]')).filter(visible);
  if (boxes.length !== 1 || boxes[0].getAttribute('aria-multiselectable') === 'true') return {options:[], supported:false};
  const choices = Array.from(boxes[0].querySelectorAll('[role="option"]')).filter(visible);
  if (choices.length > 300) return {options:[], supported:false};
  return {supported:true, options:choices.map((el,i) => {
    const label = (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
    const id = token + '-' + i;
    el.setAttribute('data-jobradar-choice',id);
    return {id, label, value:label, disabled:el.getAttribute('aria-disabled') === 'true'};
  })};
}"""


async def read_combobox(page, field: dict, *, close: bool = True, query: str | None = None) -> dict:
    """Read one visible, associated listbox without typing or guessing a value."""
    control = page.locator(f'[data-jobradar-field="{field["id"]}"]')
    original = await control.input_value() if await control.evaluate("el => 'value' in el") else None
    searched = False
    await control.click(timeout=5000)
    try:
        if isinstance(query, str) and query and len(query) <= 240:
            # Search using only the explicit supplied answer. Selecting a result
            # still requires one exact visible choice; typing never commits it.
            first = await page.evaluate(CHOICES_JS, {"fieldId": field["id"], "token": uuid.uuid4().hex})
            if not first["options"]:
                await control.fill(query, timeout=3000)
                searched = True
        await page.locator('[role="listbox"]:visible [role="option"]').first.wait_for(state="visible", timeout=3000)
        return await page.evaluate(CHOICES_JS, {"fieldId": field["id"], "token": uuid.uuid4().hex})
    except Exception:
        return {"options": [], "supported": False}
    finally:
        if close:
            if searched and original is not None:
                await control.fill(original, timeout=3000)
            await control.press("Escape", timeout=3000)


async def enrich_comboboxes(page, snapshot: dict, answer_for=None) -> dict:
    fields = [item for item in snapshot.get("fields", []) if item.get("combobox") and item.get("visible") and not item.get("disabled")]
    if len(fields) > 60:
        return snapshot
    for item in fields:
        answer = answer_for(item) if answer_for else None
        result = await read_combobox(page, item, query=answer if isinstance(answer, str) else None)
        item["options"] = [{key: value for key, value in option.items() if key != "id"} for option in result["options"]]
        item["unsupported_choices"] = not result["supported"]
    return snapshot


async def choose_combobox(page, field_id: str, label: str) -> None:
    """Commit exactly one actual DOM option; text in the search box is not enough."""
    result = await read_combobox(page, {"id": field_id}, close=False, query=label)
    options = [item for item in result["options"] if item["label"] == label and not item["disabled"]]
    if not result["supported"] or len(options) != 1:
        raise ValueError("The exact approved combobox option is no longer available.")
    await page.locator(f'[data-jobradar-choice="{options[0]["id"]}"]').click(timeout=5000)
