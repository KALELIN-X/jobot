"""Bounded Workday candidate browser, with explicit human account handoff.

Transport paths are grounded in Workday's public 2026 candidate-experience
apply-flow asset. Unknown tenant variants pause; no employer integration API,
password store, blanket POST allowance or automatic account creation is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .autofill import plan_form, _DECLARATION, _CONFIRMATION
from .form_adapters import SNAPSHOT_JS, choose_combobox, enrich_comboboxes
from .question_intents import protected_question
from .question_lookup import _public_resource
from .workday_adapter import parse_workday_destination, public_workday_detail_url, extract_workday_posting


_ID = r"(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})"
_SECTIONS = frozenset({"name", "address", "contact", "phone", "phones", "email", "emails",
    "webaddresses", "workexperiences", "educations", "certifications", "languages", "skills",
    "source", "previousworker", "selfidentify", "personalinfous", "nonuspersonalinfo",
    "questionnaireresponse", "questionnaireanswers", "resumeattachments", "availability",
    "terms", "termslockmoment", "initialize"})
_NEXT = re.compile(r"^(?:save and continue|next|continue)$", re.I)
_SUBMIT = re.compile(r"^(?:submit|submit application|submit your application)$", re.I)
# Both publicly advertised Workday bundles build these email/password account
# requests from location.origin + /{locale}/{siteId}. Verification-link and
# external SSO transports are not inferred from a synthetic form.
_ACCOUNT_POSTS = frozenset({"login", "register", "resendactivationinfo", "initiatereset"})

_RECORDS_JS = r"""() => {
 const visible=e=>!!(e.getClientRects().length&&getComputedStyle(e).display!=='none');
 const result=[];
 for(const [kind,ids] of [['employment',['workExperience','workExperienceSection']],['education',['education','educationSection']]]) {
  const sections=[...new Set(ids.flatMap(id=>[...document.querySelectorAll('[data-automation-id="'+id+'"]')]))].filter(visible);
  for(const section of sections) {
   const records=[...section.querySelectorAll('fieldset,[data-automation-id="panel"]')].filter(visible).filter(e=>!e.parentElement.closest('fieldset,[data-automation-id="panel"]')||e.parentElement.closest('fieldset,[data-automation-id="panel"]')===section);
   records.forEach((record,index)=>{const ids=[...record.querySelectorAll('[data-jobradar-field]')].map(e=>e.getAttribute('data-jobradar-field'));result.push({kind,index,ids});});
  }
 }
 return result;
}"""


class WorkdayApplication:
    def __init__(self, job, resume_path, *, fixture=False):
        self.job, self.fixture = dict(job), fixture
        self.identity = parse_workday_destination(job.get("apply_url"), fixture=fixture)
        if not self.identity:
            raise ValueError("A strict Workday tenant/site/requisition URL is required.")
        self.url = self.identity["apply_url"]
        self.resume_path = Path(resume_path).resolve() if resume_path else None
        self.resume_bytes = self.resume_path.read_bytes() if self.resume_path else None
        self.resume_hash = hashlib.sha256(self.resume_bytes).hexdigest() if self.resume_bytes is not None else None
        self.posting = None
        self._posting_ready = asyncio.Event()
        self.application_id = None
        self._create_sent = False
        self._final_allowed = False
        self._final_sent = False
        self._final_receipt = False
        self._terminal = None
        self._transport_error = False
        self._control = None
        self._paused_signature = None
        self._paused_kind = None
        self._manual_fields = set()
        self._upload_done = asyncio.Event()
        self._upload_ids = set()
        self._observed_site_paths = {self.identity["site_path"]}

    def _parts(self, url):
        try:
            parts = urlsplit(url)
            initial = urlsplit(self.url)
            if (parts.scheme, parts.netloc) != (initial.scheme, initial.netloc) or parts.username or parts.password or parts.fragment:
                return None
            if re.search(r"%2f|%5c|%25", parts.path, re.I):
                return None
            return parts
        except (TypeError, ValueError):
            return None

    def _document_allowed(self, url, *, human=False):
        current = parse_workday_destination(url, fixture=self.fixture)
        if current and current["application_key"] == self.identity["application_key"]:
            self._observed_site_paths.add(current["site_path"])
            return True
        parts = self._parts(url)
        return bool(human and parts and parts.path in {
            prefix + "/" + endpoint for prefix in self._observed_site_paths
            for endpoint in {"login", "register"}} and self._account_redirect_allowed(parts))

    def _account_redirect_allowed(self, parts):
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
            if not query:
                return True
            if set(query) != {"redirect"} or len(query["redirect"]) != 1:
                return False
            redirect = query["redirect"][0]
            target = urlsplit(redirect)
            if not redirect.startswith("/") or redirect.startswith("//") or target.scheme or target.netloc or target.query or target.fragment:
                return False
            current = parse_workday_destination(self.identity["origin"] + redirect, fixture=self.fixture)
            return bool(current and current["application_key"] == self.identity["application_key"]
                        and current["site_path"] in self._observed_site_paths)
        except (ValueError, TypeError):
            return False

    def _account_path(self, path):
        return path in {prefix + "/" + endpoint for prefix in self._observed_site_paths for endpoint in _ACCOUNT_POSTS}

    def _app_resource(self, path):
        if not self.application_id:
            return None
        tenant, app = re.escape(self.identity["tenant"]), re.escape(self.application_id)
        match = re.fullmatch(rf"/wday/(?:cxs/{tenant}|calypso/cxs/jobapplication/{tenant})/(?:jobapplication|package)/{app}/([a-z]+)(?:/({_ID}))?", path)
        return (match[1], match[2]) if match else None

    def _create_path(self, path):
        if not self.posting:
            return False
        # Public Workday builds use the route slug or the verified internal
        # posting ID. Both are bound by the exact CXS detail response.
        prefix = f"/wday/cxs/{self.identity['tenant']}/jobpostings/"
        return path in {prefix + self.posting["id"] + "/jobapplications",
                        prefix + self.identity["posting_slug"] + "/jobapplications"}

    def is_final_request(self, request):
        parts = self._parts(request.url)
        return bool(parts and self.application_id and request.method == "POST" and not parts.query
                    and parts.path == f"/wday/cxs/{self.identity['tenant']}/jobapplication/{self.application_id}/finalize")

    def _upload_url(self, parts):
        if not parts or not self.application_id or parts.query:
            return False
        tenant = self.identity["tenant"]
        resource = self._app_resource(parts.path)
        return bool(resource and resource[0] == "resume" or parts.path in {
            f"/wday/calypso/cxs/common/{tenant}/attachments", f"/wday/cxs/{tenant}/attachments"})

    def ignore_request(self, request):
        try:
            parts = urlsplit(request.url)
            return (parts.hostname in {"www.google-analytics.com", "analytics.google.com", "www.googletagmanager.com"}
                    or bool(re.fullmatch(r"wd\d{1,2}\.myworkday\.com", parts.hostname or "") and parts.path.startswith("/wday/microscope/")))
        except ValueError:
            return False

    async def request_allowed(self, request, phase):
        if request.method in {"GET", "HEAD"}:
            if request.is_navigation_request():
                return self._document_allowed(request.url, human=phase == "human")
            return _public_resource(request.url, self.url, self.fixture)
        parts = self._parts(request.url)
        if not parts or self._terminal or request.method not in {"POST", "PUT"}:
            return False
        # Human account actions are confined to this tenant's observed site
        # routes. Credentials are neither read nor retained by this provider.
        if phase == "human" and self._paused_kind in {"login", "verification"} and request.method == "POST":
            if self._account_path(parts.path):
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                return content_type == "application/x-www-form-urlencoded" and self._account_redirect_allowed(parts)
        if not self.posting and parts.path.startswith(f"/wday/cxs/{self.identity['tenant']}/jobpostings/") and parts.path.endswith("/jobapplications"):
            try:
                await asyncio.wait_for(self._posting_ready.wait(), 3)
            except TimeoutError:
                return False
        if self._terminal or not self.posting or self.posting["closed"]:
            return False
        if self._create_path(parts.path) and request.method == "POST" and not parts.query:
            if phase not in {"opening", "next", "human"} or self._create_sent or self.application_id:
                return False
            if request.post_data not in {None, "", "{}"}:
                return False
            self._create_sent = True
            return True
        if self.is_final_request(request):
            if phase != "submitting" or not self._final_allowed or self._final_sent:
                return False
            self._final_sent = True
            return self._json_body(request) is not None
        if self._upload_url(parts) and phase == "uploading" and request.method == "POST":
            return self._resume_body(request) is not None
        resource = self._app_resource(parts.path)
        if not resource:
            return False
        section, child = resource
        if section == "validate" and phase in {"next", "submitting"} and request.method == "PUT" and not parts.query:
            return request.post_data in {None, "", "{}"}
        if section not in _SECTIONS or phase not in {"opening", "filling", "next", "uploading", "submitting", "human"}:
            return False
        if child and request.method != "PUT":
            return False
        if set(parse_qs(parts.query)) - {"type"}:
            return False
        value = self._json_body(request)
        if section == "resumeattachments" and isinstance(value, dict) and "attachments" in value:
            try:
                await asyncio.wait_for(self._upload_done.wait(), 15)
            except TimeoutError:
                return False
            attachments = value["attachments"]
            return (isinstance(attachments, list) and len(attachments) == 1 and isinstance(attachments[0], dict)
                    and attachments[0].get("file") in self._upload_ids)
        return value is not None

    @staticmethod
    def _json_body(request):
        try:
            text = request.post_data or "{}"
            value = json.loads(text) if len(text) <= 2_000_000 else None
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    def _resume_body(self, request):
        if not self.resume_path or self.resume_bytes is None:
            return None
        content = request.post_data_buffer
        header = request.headers.get("content-type", "")
        if not isinstance(content, bytes) or len(content) > 6_000_000 or not header.startswith("multipart/form-data;"):
            return None
        try:
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + header + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + content)
            parts = list(message.iter_parts())
            if len(parts) != 1 or parts[0].get_param("name", header="content-disposition") != "body" or parts[0].get_filename() != self.resume_path.name:
                return None
            raw = parts[0].get_payload(decode=True)
            if raw == self.resume_bytes:
                return content
            # Chromium can omit file-backed bytes from an intercepted body.
            # Restore only the single exact selected file, never other fields.
            if raw != b"":
                return None
            boundary = message.get_boundary().encode()
            chunks = content.split(b"--" + boundary)
            files = [index for index, chunk in enumerate(chunks) if b"filename=" in chunk]
            if len(files) != 1:
                return None
            index = files[0]
            headers, separator, payload = chunks[index].partition(b"\r\n\r\n")
            if not separator or payload != b"\r\n":
                return None
            chunks[index] = headers + separator + self.resume_bytes + b"\r\n"
            return (b"--" + boundary).join(chunks)
        except Exception:
            return None

    async def prepare_request(self, request, phase):
        parts = self._parts(request.url)
        if phase == "uploading" and self._upload_url(parts):
            content = self._resume_body(request)
            if content is None:
                raise ValueError("The Workday resume body did not match the supplied file.")
            return {"post_data": content}
        return None

    async def observe_response(self, response):
        try:
            request, parts = response.request, self._parts(response.url)
            if not parts:
                return
            if request.method == "POST" and self._account_path(parts.path) and self._account_redirect_allowed(parts):
                # The site's visible account UI owns retryable sign-in errors.
                # Never read its response or treat a human password typo as an
                # application-write failure that would prevent a later resume.
                return
            expected = public_workday_detail_url(self.url, fixture=self.fixture)
            if request.method == "GET" and unquote(response.url.split("?", 1)[0]) == unquote(expected):
                if response.status != 200:
                    self._transport_error = True
                    self._posting_ready.set()
                    return
                body = await response.body()
                value = extract_workday_posting(self.url, json.loads(body), fixture=self.fixture) if len(body) <= 2_000_000 else None
                if value is None:
                    self._transport_error = True
                else:
                    self.posting = value
                self._posting_ready.set()
            if request.method == "POST" and self._create_path(parts.path):
                body = await response.body()
                value = json.loads(body) if response.status == 200 and len(body) <= 100000 else {}
                candidate = value.get("id") if isinstance(value, dict) else None
                if isinstance(candidate, str) and re.fullmatch(_ID, candidate) and self._create_sent and not self.application_id:
                    self.application_id = candidate
                else:
                    self._transport_error = True
            if request.method == "POST" and self._upload_url(parts):
                body = await response.body()
                value = json.loads(body) if 200 <= response.status < 300 and len(body) <= 100000 else None
                if isinstance(value, dict) and isinstance(value.get("id"), str) and re.fullmatch(_ID, value["id"]):
                    self._upload_ids.add(value["id"])
                elif not self._app_resource(parts.path):
                    self._transport_error = True
                self._upload_done.set()
            if self.is_final_request(request):
                body = await response.body()
                value = json.loads(body) if response.status == 200 and len(body) <= 100000 else {}
                self._final_receipt = bool(self._final_sent and isinstance(value, dict) and value.get("id") == self.application_id and not value.get("errors"))
            elif request.method not in {"GET", "HEAD"} and response.status >= 400:
                self._transport_error = True
        except Exception:
            self._transport_error = True

    @staticmethod
    def _signature(snapshot):
        return hashlib.sha256(json.dumps([(item.get("name"), item.get("label"), item.get("type")) for item in snapshot["fields"] if item.get("visible") and item.get("type") != "hidden"], sort_keys=True).encode()).hexdigest()

    def _pause(self, control, kind, message, snapshot=None, pending=None):
        self._paused_kind = kind
        self._paused_signature = self._signature(snapshot) if snapshot else None
        self._manual_fields = {(item.get("name"), item.get("label"), item.get("type")) for item in (snapshot or {}).get("fields", []) if item.get("visible")}
        return control.pause(kind, message, pending_questions=pending or [])

    async def _snapshot(self, page, profile):
        state = await page.evaluate(SNAPSHOT_JS, uuid.uuid4().hex)
        # Workday button-backed prompts are not ordinary select elements. Reuse
        # existing visible-choice handling only where ARIA exposes a combobox.
        if not state.get("login") and not state.get("captcha"):
            from .autofill import _answer
            state = await enrich_comboboxes(page, state, lambda item: _answer(item, profile, self.job))
        state["records"] = await page.evaluate(_RECORDS_JS)
        return state

    async def _load_posting(self, page):
        url = public_workday_detail_url(self.url, fixture=self.fixture)
        try:
            value = await page.evaluate("""async url => {
              const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),12000);
              try { const response=await fetch(url,{method:'GET',signal:controller.signal});
                const body=await response.text();
                return response.ok&&body.length<=2000000 ? JSON.parse(body) : null;
              } finally {clearTimeout(timer)}
            }""", url)
            self.posting = extract_workday_posting(self.url, value, fixture=self.fixture)
        except Exception:
            self.posting = None
        finally:
            self._posting_ready.set()

    async def _prepare_records(self, control, state):
        """Add only explicit typed records inside recognized visible sections."""
        page = control.page
        for kind, names in (("employment", ("workExperience", "workExperienceSection")), ("education", ("education", "educationSection"))):
            sections = page.locator(",".join(f'[data-automation-id="{name}"]' for name in names)).filter(visible=True)
            if await sections.count() == 0:
                continue
            if await sections.count() != 1:
                return self._pause(control, "review", "Repeated Workday sections are ambiguous. Review the records in this browser.", state)
            records = (control.profile.get("application_profile") or {}).get(kind, [])
            expected = len(records)
            actual = len([group for group in state["records"] if group["kind"] == kind])
            if expected > 40:
                return self._pause(control, "review", "The confirmed Workday records exceed the bounded form limit.", state)
            while actual < expected:
                add = sections.get_by_role("button", name=re.compile(r"^add(?: another)?$", re.I)).filter(visible=True)
                if await add.count() != 1:
                    return self._pause(control, "review", "Workday needs more education/employment records, but no unique Add control is available.", state)
                await control.verify()
                control.phase = "filling"
                await add.click()
                await page.wait_for_timeout(100)
                state = await self._snapshot(page, control.profile)
                fresh = len([group for group in state["records"] if group["kind"] == kind])
                if fresh != actual + 1:
                    return self._pause(control, "review", "Workday did not add one identifiable record. Review the current section.", state)
                actual = fresh
            for group in [item for item in state["records"] if item["kind"] == kind]:
                if group["index"] >= expected:
                    continue
                record = records[group["index"]]
                if kind == "employment" and record.get("current") is True:
                    checkbox = next((item for item in state["fields"] if item["id"] in group["ids"] and item.get("type") == "checkbox"
                        and re.sub(r"[^a-z ]", "", item.get("label", "").casefold()).strip() in {"currently work here", "i currently work here"}), None)
                    if checkbox and not checkbox.get("checked"):
                        await control.verify()
                        control.phase = "filling"
                        await page.locator(f'[data-jobradar-field="{checkbox["id"]}"]').set_checked(True)
                        state = await self._snapshot(page, control.profile)
        return state

    @staticmethod
    def _record_profile(profile, kind, record, fields):
        derived = dict(profile)
        app = dict(profile.get("application_profile") or {})
        app[kind] = [{**record, "primary_for_applications": True}]
        derived["application_profile"] = app
        answers = dict(profile.get("answers") or {})
        names = {"company": "company", "company name": "company", "employer": "company", "job title": "title", "title": "title", "school": "institution", "school or university": "institution", "institution": "institution", "degree": "degree", "field of study": "discipline", "discipline": "discipline", "gpa": "gpa", "start date": "start_month", "from": "start_month", "end date": "end_month", "to": "end_month", "i currently work here": "current", "currently work here": "current"}
        for field in fields:
            label = re.sub(r"\s*[*✱]\s*$", "", field.get("label", "")).strip()
            key = names.get(label.casefold())
            if key and record.get(key) is not None:
                value = record[key]
                if key in {"start_month", "end_month"} and field.get("type") == "date":
                    continue  # A known month never establishes an exact day.
                answers[label] = str(value) if type(value) in {int, float} else value
        derived["answers"] = answers
        return derived

    async def _fill(self, control, state, manual):
        groups, grouped = [], set()
        for group in state["records"]:
            records = (control.profile.get("application_profile") or {}).get(group["kind"], [])
            if group["index"] >= len(records):
                fields = [item for item in state["fields"] if item["id"] in group["ids"]]
                if manual and all(not item.get("required") or item.get("value") or item.get("checked") or item.get("has_file") for item in fields):
                    grouped.update(group["ids"])
                    continue
                return self._pause(control, "review", "A repeated Workday education/employment record has no matching confirmed profile record. Review it in this browser.", state)
            fields = [item for item in state["fields"] if item["id"] in group["ids"]]
            grouped.update(group["ids"])
            groups.append((fields, self._record_profile(control.profile, group["kind"], records[group["index"]], fields)))
        groups.append(([item for item in state["fields"] if item["id"] not in grouped], control.profile))
        for fields, profile in groups:
            if manual:
                fields = [item for item in fields if not ((item.get("name"), item.get("label"), item.get("type")) in self._manual_fields and
                    (item.get("value") or item.get("checked") or item.get("has_file")) and item.get("type") not in {"password", "hidden"})]
            plan = plan_form(fields, profile, self.resume_path, job=self.job)
            if plan.blockers:
                legal = any(protected_question(item.get("label", "")) for item in fields if item.get("required"))
                return self._pause(control, "agreement" if legal else "question", " ".join(plan.blockers)[:1200], state, plan.pending_questions)
            for action in plan.actions:
                await control.verify()
                control.phase = "uploading" if action.kind == "file" else "filling"
                await control.emit("uploading_resume" if action.kind == "file" else "filling_fields", field_count=len(plan.actions))
                item = control.page.locator(f'[data-jobradar-field="{action.field_id}"]')
                if action.kind == "fill":
                    await item.fill(action.value)
                elif action.kind == "check":
                    await item.set_checked(action.value)
                elif action.kind == "select":
                    await item.select_option(value=action.value)
                elif action.kind == "combobox":
                    await choose_combobox(control.page, action.field_id, action.value)
                elif action.kind == "file":
                    self._upload_done.clear()
                    await item.set_input_files(action.value)
                    try:
                        await asyncio.wait_for(self._upload_done.wait(), 15)
                    except TimeoutError:
                        return self._pause(control, "transport", "The Workday resume upload did not return a bounded receipt. Review it before advancing.", state)
                await control.page.wait_for_timeout(80)
                if self._transport_error or control.blocked_requests:
                    return self._pause(control, "transport", "This Workday tenant used an unrecognized or failed data transport. Review the form; no final application was submitted.", state)
        return None

    async def drive(self, control):
        self._control = control
        if self._terminal:
            return dict(self._terminal)
        await control.verify()
        if getattr(control, "reserved", False) or getattr(control, "final_request_count", 0):
            return control.result("uncertain", "A Workday submission attempt already exists. Verify its outcome; do not retry.")
        if not control.started:
            control.phase = "opening"
            await control.emit("opening_form")
            await control.page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
            control.started = True
            await self._load_posting(control.page)
            await control.page.wait_for_timeout(250)
        acknowledged = control.human_acknowledged is True
        control.human_acknowledged = False
        for _ in range(16):
            state = await self._snapshot(control.page, control.profile)
            if state.get("login"):
                return self._pause(control, "login", "Sign in or create the candidate account yourself in this Workday browser. Complete any email verification, then choose Resume in Jobot. Jobot does not enter or store passwords.", state)
            if state.get("captcha") or re.search(r"\b(?:verification code|one.time code|verify your email)\b", state.get("body", ""), re.I):
                return self._pause(control, "verification", "Complete Workday verification yourself in this same browser, then choose Resume in Jobot.", state)
            if not self._document_allowed(control.page.url):
                return self._pause(control, "review", "The Workday page is no longer bound to this exact tenant, site and requisition.", state)
            if control.blocked_requests:
                return self._pause(control, "transport", "A Workday request left the recognized transport boundary. Review the browser before any further application action.", state)
            if not self.posting or self._transport_error or self.posting["closed"]:
                return self._pause(control, "transport", "The current Workday public metadata could not verify this exact available requisition, or its transport changed. No applicant fields were entered.", state)
            if re.search(_CONFIRMATION, state.get("body", ""), re.I):
                return self._pause(control, "review", "Workday already displays an application receipt. Verify existing history rather than applying again.", state)
            manual = acknowledged and self._paused_signature == self._signature(state) and self._paused_kind in {"question", "agreement", "review"}
            if _DECLARATION.search(state.get("body", "")) and not (manual and self._paused_kind == "agreement"):
                return self._pause(control, "agreement", "Review the displayed Workday declaration in this browser, then explicitly confirm Resume if you agree to proceed.", state)
            prepared = await self._prepare_records(control, state)
            if "status" in prepared:
                return prepared
            state = prepared
            held = await self._fill(control, state, manual)
            if held:
                return held
            if not await control.page.locator("form").evaluate_all("forms=>forms.filter(f=>f.getClientRects().length).every(f=>f.checkValidity())"):
                return self._pause(control, "question", "Workday reports an invalid or missing field. Complete it in this browser, then Resume.", state)
            buttons = control.page.get_by_role("button", name=_NEXT).filter(visible=True)
            submit = control.page.get_by_role("button", name=_SUBMIT).filter(visible=True)
            if await submit.count() == 1 and await buttons.count() == 0:
                if not self.application_id:
                    return self._pause(control, "transport", "The Workday application draft was not bound to the verified requisition; final submission is disabled.", state)
                await control.verify()
                await control.reserve()
                self._final_allowed = True
                control.phase = "submitting"
                await control.emit("submitting")
                await submit.click()
                control.phase = "confirmation"
                await control.emit("awaiting_confirmation")
                for _ in range(50):
                    if self._final_receipt:
                        body = await control.page.locator("body").inner_text()
                        if re.search(_CONFIRMATION, body, re.I) and not control.blocked_requests:
                            self._terminal = control.result("submitted", "Workday confirmed this exact application after one reserved final request.", confirmation="Verified Workday application receipt.")
                            return dict(self._terminal)
                    await control.page.wait_for_timeout(100)
                self._terminal = control.result("uncertain", "The Workday final request has no verified application receipt. Verify its outcome; automatic retry is disabled.")
                return dict(self._terminal)
            if await buttons.count() != 1 or await submit.count():
                return self._pause(control, "review", "This Workday step does not have one recognized Next or final Submit action. Review it manually.", state)
            prior = self._signature(state)
            await control.verify()
            control.phase = "next"
            await buttons.click()
            await control.page.wait_for_timeout(250)
            if self._transport_error or control.blocked_requests:
                return self._pause(control, "transport", "The Workday step could not save through its bounded transport. Review the current browser step.", state)
            fresh = await self._snapshot(control.page, control.profile)
            if self._signature(fresh) == prior:
                return self._pause(control, "review", "Workday did not advance after Next. Review its validation messages in this browser.", fresh)
            acknowledged = False
        return self._pause(control, "review", "The Workday application exceeded the bounded number of steps. Continue manually in this browser.")
