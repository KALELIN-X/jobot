"""Resumable hosted Ashby applications with render-bound write capabilities.

The browser drives the public renderer. This module never invokes Ashby's
employer-authenticated API. Every field mutation is pinned to the public form's
render, definition, path and approved value; uploads are pinned to advertised
presigned fields and the exact supplied file. Unknown renderer operations stop
for review rather than granting a general GraphQL/POST exception.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .ashby_adapter import ASHBY_SNAPSHOT_JS, ashby_document_allowed, choose_ashby_yesno, parse_ashby_destination, verify_ashby_document
from .autofill import _DECLARATION, _answer, _label, _PRIVATE_CHALLENGE, plan_form
from .form_adapters import enrich_comboboxes, read_combobox
from .question_intents import protected_question


_OPERATIONS = {
    "ApiJobPosting": ("query", "jobPosting"),
    "ApiJobPostingAppData": ("query", "jobPosting"),
    "ApiOrganizationFromHostedJobsPageName": ("query", "organizationFromHostedJobsPageName"),
    "ApiAutocompleteGeoLocation": ("query", "autocompleteGeoLocation"),
    "ApiSearchSchoolByCanonicalName": ("query", "searchCanonicalSchools"),
    "ApiSetFormValue": ("mutation", "setFormValue"),
    "ApiSetFormValueToFile": ("mutation", "setFormValueToFile"),
    "ApiCreateFileUploadHandle": ("mutation", "createFileUploadHandle"),
    "ApiSubmitSingleApplicationFormAction": ("mutation", "submitSingleApplicationFormAction"),
    "ApiSubmitApplicationFormAction": ("mutation", "submitApplicationFormAction"),
    "ApiSubmitMultipleFormsAction": ("mutation", "submitMultipleFormsAction"),
}
_FINALS = frozenset(name for name in _OPERATIONS if name.startswith("ApiSubmit"))
_ARGS = {
    "ApiJobPosting": {"organizationHostedJobsPageName", "jobPostingId"},
    "ApiJobPostingAppData": {"organizationHostedJobsPageName", "jobPostingId", "excludeForms"},
    "ApiOrganizationFromHostedJobsPageName": {"organizationHostedJobsPageName", "searchContext"},
    "ApiAutocompleteGeoLocation": {"text", "locationTypes"},
    "ApiSearchSchoolByCanonicalName": {"name", "organizationHostedJobsPageName"},
    "ApiSetFormValue": {"organizationHostedJobsPageName", "formRenderIdentifier", "path", "value", "formDefinitionIdentifier"},
    "ApiSetFormValueToFile": {"organizationHostedJobsPageName", "formRenderIdentifier", "path", "fileHandle", "formDefinitionIdentifier"},
    "ApiCreateFileUploadHandle": {"organizationHostedJobsPageName", "fileUploadContext", "filename", "contentType", "contentLength"},
}
for _name in _FINALS:
    _ARGS[_name] = {"organizationHostedJobsPageName", "jobPostingId", "recaptchaToken", "sourceAttributionCode",
                    "viewedAutomatedProcessingLegalNoticeRuleId", "deviceFingerprint", "applicationRequestId"} | (
        {"applicationFormRenderIdentifier", "applicationFormActionIdentifier", "applicationFormDefinitionIdentifier", "surveyIdentifiers"}
        if _name == "ApiSubmitMultipleFormsAction" else {"formRenderIdentifier", "formDefinitionIdentifier", "actionIdentifier"})
_TOKEN = re.compile(r'\s+|#[^\n\r]*|\.\.\.|"(?:\\.|[^"\\])*"|[_A-Za-z][_0-9A-Za-z]*|[-0-9.]+|[!$():=@\[\]{|},]')
_STATIC_HOSTS = frozenset({"jobs.ashbyhq.com", "cdn.ashbyprd.com", "cdn.ashbyhq.com", "www.google.com", "www.gstatic.com", "www.recaptcha.net", "recaptcha.google.com"})
_CHALLENGE_HOSTS = frozenset({"www.google.com", "www.recaptcha.net", "recaptcha.google.com"})


def _json_object(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def _same_value(left, right):
    """JSON equality that never treats a boolean answer as the number 0/1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is bool and type(right) is bool and left is right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return left == right and (type(left) is type(right) or isinstance(left, (int, float)) and isinstance(right, (int, float)))


def _graphql_shape(query, operation, variables):
    """A narrow GraphQL envelope parser, not a general executor.

    Require one named operation and one known root field. Every root argument
    must reference its identically named variable (the public excludeForms true
    exception is read-only). Fragments may supply nested return fields only.
    """
    if not isinstance(query, str) or len(query) > 500_000:
        return False
    tokens, position = [], 0
    for match in _TOKEN.finditer(query):
        if match.start() != position:
            return False
        position = match.end()
        token = match[0]
        if not token.isspace() and not token.startswith("#") and token != ",":
            tokens.append(token)
    if position != len(query) or len(tokens) > 100_000:
        return False
    try:
        kind, root = _OPERATIONS[operation]
        if tokens[:2] != [kind, operation]:
            return False
        def closing(index, left, right):
            depth = 0
            for offset in range(index, len(tokens)):
                depth += tokens[offset] == left
                depth -= tokens[offset] == right
                if depth == 0:
                    return offset
            raise ValueError("Unbalanced GraphQL")
        index = 2
        if tokens[index] == "(":
            index = closing(index, "(", ")") + 1
        if tokens[index] != "{":
            return False
        index += 1
        name = tokens[index]
        index += 1
        if tokens[index] == ":":
            index += 1
            name, index = tokens[index], index + 1
        if name != root or tokens[index] != "(":
            return False
        end = closing(index, "(", ")")
        args, index = {}, index + 1
        while index < end:
            arg = tokens[index]
            if tokens[index + 1] != ":" or arg in args:
                return False
            index += 2
            if tokens[index] == "$":
                if tokens[index + 1] != arg:
                    return False
                args[arg], index = arg, index + 2
            elif operation == "ApiJobPostingAppData" and arg == "excludeForms" and tokens[index] == "true":
                args[arg], index = True, index + 1
            else:
                return False
        if set(variables) - set(args) or set(args) - _ARGS[operation]:
            return False
        index = end + 1
        if tokens[index] != "{":
            return False
        index = closing(index, "{", "}") + 1
        if tokens[index] != "}":
            return False  # Reject second root fields, aliases and root spreads.
        index += 1
        while index < len(tokens):
            if tokens[index] != "fragment" or tokens[index + 2] != "on" or tokens[index + 3] in {"Query", "Mutation"} or tokens[index + 4] != "{":
                return False
            index = closing(index + 4, "{", "}") + 1
        return True
    except (IndexError, KeyError, ValueError, TypeError):
        return False


def _safe_url(url):
    try:
        target = urlsplit(url)
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.port not in (None, 443) or target.fragment or "\\" in url
                or any(char.isspace() for char in url)):
            return None
        return target
    except (TypeError, ValueError):
        return None


def _request_info(request):
    try:
        target = _safe_url(request.url)
        if (not target or target.hostname != "jobs.ashbyhq.com" or target.path != "/api/non-user-graphql"
                or request.method != "POST" or not request.headers.get("content-type", "").startswith("application/json")):
            return None
        raw = request.post_data_buffer
        if not isinstance(raw, bytes) or len(raw) > 1_000_000:
            return None
        body = _json_object(raw)
        if not isinstance(body, dict) or set(body) - {"operationName", "variables", "query"}:
            return None
        operation, variables = body.get("operationName"), body.get("variables")
        if (operation not in _OPERATIONS or parse_qs(target.query) != {"op": [operation]}
                or not isinstance(variables, dict) or not _graphql_shape(body.get("query"), operation, variables)):
            return None
        return operation, variables, hashlib.sha256(raw).hexdigest()
    except (TypeError, ValueError, AttributeError, RecursionError):
        return None


_APPLICATION_SNAPSHOT = r"""token => {
 const snapshot=(__BASE__)(token);
 for(const field of snapshot.fields){
  const element=document.querySelector('[data-jobradar-field="'+field.id+'"]');
  const entry=element?.matches('[data-field-entry-id]')?element:element?.closest('[data-field-entry-id]') || element?.querySelector('[data-field-entry-id]');
  field.entry_id=entry?.getAttribute('data-field-entry-id') || '';
  field.path=entry?.getAttribute('data-field-path') || '';
  const title=entry?.querySelector('.ashby-application-form-question-title');
  if(title)field.label=(title.innerText || title.textContent || '').replace(/\s+/g,' ').trim();
 }
 const visible=el=>!!(el.getClientRects().length && getComputedStyle(el).display!=='none' && getComputedStyle(el).visibility!=='hidden');
 snapshot.visible_challenge=[...document.querySelectorAll('iframe[title*="challenge"],iframe[title*="Challenge"],.h-captcha')].some(visible);
 return snapshot;
}""".replace("__BASE__", ASHBY_SNAPSHOT_JS)


class AshbyApplication:
    """One retained browser session for an exact public Ashby application."""
    def __init__(self, job: dict, resume_path: Path | None, *, fixture=False):
        self.job = dict(job)
        self.destination = parse_ashby_destination(job.get("apply_url"))
        if not self.destination:
            raise ValueError("An exact hosted Ashby posting is required.")
        self.url = self.destination["apply_url"]
        self.resume_path = Path(resume_path).resolve() if resume_path else None
        self.resume_sha = hashlib.sha256(self.resume_path.read_bytes()).hexdigest() if self.resume_path else None
        self.fixture = fixture
        self.document_verified = False
        self.document_ready = asyncio.Event()
        self.forms = {}
        self.application_id = None
        self.survey_ids = []
        self.posting = None
        self.metadata = asyncio.Event()
        self.active = None
        self.done = asyncio.Event()
        self.active_success = False
        self.accepted = {}
        self.upload = None
        self.upload_ready = asyncio.Event()
        self.upload_completed = asyncio.Event()
        self.upload_sent = False
        self.upload_success = False
        self.final_sent = False
        self.final_done = asyncio.Event()
        self.final_success = False
        self.error = None
        self.control = None
        self.human_fields = set()
        self.lookup_queries = set()
        self.location_options = {}
        self.school_options = {}
        self.lookup_pending = set()
        self.lookup_observed = asyncio.Event()
        self.pending_gate = None
        self.verification_acknowledged = False
        self.agreement_acknowledged = set()

    def ignore_request(self, request):
        target = _safe_url(request.url)
        return bool(target and request.method == "POST" and re.fullmatch(r"browser-intake(?:-[a-z0-9-]+)?[-.]datadoghq\.(?:com|eu)", target.hostname)
                    and target.path.startswith("/api/v2/"))

    def is_final_request(self, request):
        info = _request_info(request)
        return bool(info and info[0] in _FINALS)

    def _bound_form(self, variables, *, prefix=""):
        render = variables.get(prefix + "formRenderIdentifier")
        form = self.forms.get(render)
        return form if form and variables.get(prefix + "formDefinitionIdentifier") == form["sourceFormDefinitionId"] else None

    async def request_allowed(self, request, phase: str) -> bool:
        target = _safe_url(request.url)
        if not target:
            return False
        if request.is_navigation_request():
            return bool(request.method == "GET" and not target.query and ashby_document_allowed(request.url, self.url))
        if request.method in {"GET", "HEAD"}:
            if target.hostname == "jobs.ashbyhq.com":
                return target.path == "/favicon.ico" and not target.query
            if target.hostname in {"cdn.ashbyprd.com", "cdn.ashbyhq.com"}:
                return target.path.startswith(("/frontend_non_user/", "/cdn_assets/")) and not target.query
            return target.hostname in _STATIC_HOSTS and target.path.startswith("/recaptcha/")
        if (target.hostname in _CHALLENGE_HOSTS and target.path.startswith("/recaptcha/")
                and request.method in {"POST", "OPTIONS"} and phase in {"human", "submitting"}):
            return len(request.post_data_buffer or b"") <= 128_000
        if (phase == "uploading" and self.active and self.active.get("file") and self.upload is None
                and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,100}\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", target.hostname)):
            # The renderer can consume its HTTP response before Playwright's
            # async response observer finishes reading it. Wait for that exact
            # capability; a hostname alone never authorizes this upload.
            try:
                await asyncio.wait_for(self.upload_ready.wait(), timeout=3)
            except TimeoutError:
                return False
        if self.upload and request.url.rstrip("/") == self.upload["url"].rstrip("/"):
            allowed = phase == "uploading" and not self.upload_sent and self._upload_body(request) is not None
            if allowed:
                self.upload_sent = True
            return allowed
        info = _request_info(request)
        if not info:
            return False
        operation, variables, digest = info
        if operation in {"ApiAutocompleteGeoLocation", "ApiSearchSchoolByCanonicalName"}:
            if not self.forms or not self.document_ready.is_set():
                try:
                    await asyncio.wait_for(self.document_ready.wait(), timeout=3)
                    await asyncio.wait_for(self.metadata.wait(), timeout=3)
                except TimeoutError:
                    return False
            allowed = self._lookup_allowed(operation, variables, phase)
            if allowed:
                self.accepted[digest] = operation
                self.lookup_pending.add(digest)
            return allowed
        if variables.get("organizationHostedJobsPageName") != self.destination["board"]:
            return False
        if "jobPostingId" in variables and variables["jobPostingId"] != self.destination["posting_id"]:
            return False
        if operation in {"ApiJobPosting", "ApiJobPostingAppData"}:
            allowed = set(variables) == {"organizationHostedJobsPageName", "jobPostingId"}
        elif operation == "ApiOrganizationFromHostedJobsPageName":
            allowed = set(variables) <= {"organizationHostedJobsPageName", "searchContext"} and variables.get("searchContext") in (None, "JobBoard")
        elif not self.document_verified or not self.forms or not self.control:
            allowed = False
        else:
            try:
                if await self.control.verify() is False:
                    return False
            except Exception:
                return False
            allowed = False
            if operation == "ApiSetFormValue":
                form = self._bound_form(variables)
                entry = form["paths"].get(variables.get("path")) if form else None
                standard = {"organizationHostedJobsPageName", "formRenderIdentifier", "path", "value", "formDefinitionIdentifier"}
                if set(variables) == standard and entry:
                    if phase == "human":
                        allowed = not _PRIVATE_CHALLENGE.search(entry["field"].get("title", "")) and len(json.dumps(variables["value"])) <= 20_000
                        if entry["field"].get("type") == "Location":
                            key = json.dumps(entry["field"].get("locationTypes"), sort_keys=True)
                            allowed = allowed and await self._await_lookup(lambda: any(_same_value(variables["value"], option) for option in self.location_options.get(key, {}).values()))
                        elif entry["field"].get("type") == "EducationHistory":
                            records = variables["value"]
                            allowed = allowed and isinstance(records, list) and len(records) <= 30 and await self._await_lookup(lambda: all(isinstance(record, dict)
                                and (not record.get("canonicalSchoolId") or self.school_options.get(record["canonicalSchoolId"]) == record.get("schoolName")) for record in records))
                    elif phase == "filling" and self.active and not self.active.get("sent"):
                        allowed = all(_same_value(variables.get(key), self.active.get(key)) for key in ("formRenderIdentifier", "path", "value", "formDefinitionIdentifier"))
            elif operation == "ApiCreateFileUploadHandle" and phase == "uploading" and self.active and self.active.get("file"):
                expected = {"organizationHostedJobsPageName": self.destination["board"], "fileUploadContext": "NonUserFormEngine",
                            "filename": self.resume_path.name, "contentLength": self.resume_path.stat().st_size,
                            "contentType": mimetypes.guess_type(self.resume_path.name)[0] or "application/octet-stream"}
                allowed = _same_value(variables, expected) and self.upload is None
            elif operation == "ApiSetFormValueToFile" and phase == "uploading" and self.active and self.upload_sent:
                try:
                    await asyncio.wait_for(self.upload_completed.wait(), timeout=3)
                except TimeoutError:
                    return False
                if not self.upload_success:
                    return False
                expected = {key: self.active[key] for key in ("formRenderIdentifier", "path", "formDefinitionIdentifier")}
                expected.update(organizationHostedJobsPageName=self.destination["board"], fileHandle=self.upload["handle"])
                allowed = variables == expected and not self.active.get("sent")
            elif operation in _FINALS and phase == "submitting" and self.control.reserved and not self.final_sent:
                allowed = self._final_variables(operation, variables)
                if allowed:
                    self.final_sent = True
            if allowed and operation in {"ApiSetFormValue", "ApiSetFormValueToFile"} and phase != "human":
                self.active["sent"] = True
        if allowed:
            self.accepted[digest] = operation
        return bool(allowed)

    async def _await_lookup(self, predicate):
        deadline = asyncio.get_running_loop().time() + 3
        while not predicate() and self.lookup_pending:
            self.lookup_observed.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.lookup_observed.wait(), timeout=remaining)
            except TimeoutError:
                break
        return bool(predicate())

    def _lookup_allowed(self, operation, variables, phase):
        if not self.document_verified or not self.forms or phase not in {"opening", "filling", "uploading", "next", "human"}:
            return False
        entries = [entry for form in self.forms.values() for entry in form["paths"].values() if not entry.get("hidden")]
        if operation == "ApiAutocompleteGeoLocation":
            text = variables.get("text")
            locations = [entry["field"] for entry in entries if entry["field"].get("type") == "Location"]
            if (set(variables) - {"text", "locationTypes"} or not isinstance(text, str) or len(text) > 240 or not locations
                    or not any(_same_value(variables.get("locationTypes"), field.get("locationTypes")) for field in locations)):
                return False
            return not text or phase == "human" or text in self.lookup_queries
        text = variables.get("name")
        if (set(variables) != {"name", "organizationHostedJobsPageName"} or variables["organizationHostedJobsPageName"] != self.destination["board"]
                or not isinstance(text, str) or len(text) > 240 or not any(entry["field"].get("type") == "EducationHistory" for entry in entries)):
            return False
        schools = ((getattr(self.control, "profile", {}) or {}).get("application_profile") or {}).get("education") or []
        return phase == "human" or not text or text in {item.get("institution") for item in schools if isinstance(item.get("institution"), str)}

    def _final_variables(self, operation, variables):
        form = self.forms.get(self.application_id)
        if not form:
            return False
        expected = {"organizationHostedJobsPageName": self.destination["board"], "jobPostingId": self.destination["posting_id"]}
        controls = [item for item in form.get("formControls", []) if re.fullmatch(r"submit(?: application)?|apply(?: now)?", str(item.get("title", "")), re.I)]
        if len(controls) != 1:
            return False
        if operation == "ApiSubmitMultipleFormsAction":
            expected.update(applicationFormRenderIdentifier=self.application_id,
                            applicationFormActionIdentifier=controls[0]["identifier"],
                            applicationFormDefinitionIdentifier=form["sourceFormDefinitionId"])
            surveys = []
            for identifier in self.survey_ids:
                survey = self.forms[identifier]
                if len(survey.get("formControls", [])) != 1:
                    return False
                surveys.append({"formRenderIdentifier": identifier, "formActionIdentifier": survey["formControls"][0]["identifier"],
                                "formDefinitionIdentifier": survey["sourceFormDefinitionId"]})
            expected["surveyIdentifiers"] = surveys
        else:
            if self.survey_ids:
                return False
            expected.update(formRenderIdentifier=self.application_id, actionIdentifier=controls[0]["identifier"],
                            formDefinitionIdentifier=form["sourceFormDefinitionId"])
        optional = {"recaptchaToken", "sourceAttributionCode", "viewedAutomatedProcessingLegalNoticeRuleId", "deviceFingerprint", "applicationRequestId"}
        if set(variables) - set(expected) - optional or any(variables.get(key) != value for key, value in expected.items()):
            return False
        notice = (self.posting.get("automatedProcessingLegalNotice") or {}).get("automatedProcessingLegalNoticeRuleId")
        if variables.get("viewedAutomatedProcessingLegalNoticeRuleId") not in (None, notice):
            return False
        if variables.get("sourceAttributionCode") is not None or variables.get("applicationRequestId") is not None:
            return False
        return all(variables.get(key) is None or isinstance(variables[key], str) and len(variables[key]) <= 12_000 for key in ("recaptchaToken", "deviceFingerprint"))

    def _upload_body(self, request):
        """Rebuild only CDP's omitted selected file, then validate every byte."""
        if not self.upload or not self.resume_path or request.method != "POST":
            return None
        try:
            raw, content_type = request.post_data_buffer, request.headers.get("content-type", "")
            if not isinstance(raw, bytes) or len(raw) > 8_000_000 or not content_type.startswith("multipart/form-data;"):
                return None
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
            boundary, fields, files, chunks = message.get_boundary(), {}, [], []
            if not boundary or not re.fullmatch(r"[ -~]{1,200}", boundary):
                return None
            for part in message.iter_parts():
                if part.get("Content-Transfer-Encoding", "").lower() not in {"", "binary", "8bit"}:
                    return None
                name = part.get_param("name", header="content-disposition")
                payload = part.get_payload(decode=True)
                if name == "file":
                    files.append(part)
                    if part.get_filename() != self.resume_path.name:
                        return None
                    if payload == b"":
                        payload = self.resume_path.read_bytes()
                    if hashlib.sha256(payload).hexdigest() != self.resume_sha:
                        return None
                elif name and name not in fields:
                    fields[name] = payload.decode("utf-8")
                else:
                    return None
                headers = part.as_bytes(policy=policy.SMTP).split(b"\r\n\r\n", 1)[0]
                chunks.append(b"--" + boundary.encode() + b"\r\n" + headers + b"\r\n\r\n" + payload + b"\r\n")
            expected = {"Content-Type": self.active["contentType"], **self.upload["fields"]}
            if len(files) != 1 or fields != expected:
                return None
            return b"".join(chunks) + b"--" + boundary.encode() + b"--\r\n"
        except (TypeError, ValueError, OSError, AttributeError):
            return None

    async def prepare_request(self, request, phase):
        if phase == "uploading" and self.upload and request.url.rstrip("/") == self.upload["url"].rstrip("/"):
            body = self._upload_body(request)
            return {"post_data": body} if body is not None else None
        return None

    def _accept_form(self, form):
        if not isinstance(form, dict) or not isinstance(form.get("id"), str) or not isinstance(form.get("sourceFormDefinitionId"), str):
            return False
        if len(form["id"]) > 1000 or len(form["sourceFormDefinitionId"]) > 5000:
            return False
        if form["sourceFormDefinitionId"].lstrip().startswith("{"):
            definition = _json_object(form["sourceFormDefinitionId"])
            if not isinstance(definition, dict) or definition.get("jobPostingId", self.destination["posting_id"]) != self.destination["posting_id"]:
                return False
        paths = {}
        for section in form.get("sections", [])[:100]:
            for entry in section.get("fieldEntries", [])[:300]:
                field = entry.get("field")
                field = _json_object(field) if isinstance(field, str) else field
                if not isinstance(field, dict) or not isinstance(field.get("path"), str) or field["path"] in paths:
                    return False
                paths[field["path"]] = {**entry, "field": field, "hidden": bool(entry.get("isHidden") or section.get("isHidden"))}
        previous = self.forms.get(form["id"])
        if previous and previous["sourceFormDefinitionId"] != form["sourceFormDefinitionId"]:
            return False
        self.forms[form["id"]] = {**form, "paths": paths}
        return True

    async def observe_response(self, response):
        try:
            request = response.request
            if request.is_navigation_request() and ashby_document_allowed(response.url, self.url) and response.status == 200:
                self.document_verified = verify_ashby_document(self.url, await response.text())
                self.document_ready.set()
            if self.upload and response.url.rstrip("/") == self.upload["url"].rstrip("/") and request.method == "POST":
                self.upload_success = 200 <= response.status < 300
                self.upload_completed.set()
                return
            info = _request_info(request)
            if not info or info[2] not in self.accepted:
                return
            operation, variables, digest = info
            self.accepted.pop(digest, None)
            body = await response.body()
            if len(body) > 3_000_000:
                return
            payload = _json_object(body)
            data = payload.get("data") or {}
            success = 200 <= response.status < 300 and not payload.get("errors")
            if operation in {"ApiJobPosting", "ApiJobPostingAppData"} and success:
                posting = data.get("jobPosting")
                if not isinstance(posting, dict) or posting.get("id") != self.destination["posting_id"]:
                    self.error = "The public form metadata belongs to a different posting."
                    self.metadata.set()
                    return
                if operation == "ApiJobPosting":
                    forms = [posting.get("applicationForm"), *(posting.get("surveyForms") or [])]
                    if not forms or not all(self._accept_form(form) for form in forms):
                        self.error = "The hosted form metadata was incomplete or conflicting."
                    else:
                        self.posting, self.application_id = posting, forms[0]["id"]
                        self.survey_ids = [form["id"] for form in forms[1:]]
                    self.metadata.set()
            elif operation == "ApiCreateFileUploadHandle" and success and self.active and self.active.get("file"):
                upload = data.get("fileUploadHandle")
                target = _safe_url(upload.get("url")) if isinstance(upload, dict) else None
                if (target and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,100}\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", target.hostname)
                        and target.path in {"", "/"} and not target.query and isinstance(upload.get("fields"), dict)
                        and 1 <= len(upload["fields"]) <= 30 and isinstance(upload.get("handle"), str) and 0 < len(upload["handle"]) <= 4000
                        and upload["fields"].get("acl") in (None, "private")
                        and all(isinstance(key, str) and isinstance(value, str) and len(value) < 30_000 for key, value in upload["fields"].items())):
                    self.upload = upload
                self.upload_ready.set()
            elif operation == "ApiAutocompleteGeoLocation" and success:
                result = data.get("result") or data.get("autocompleteGeoLocation") or {}
                options = result.get("suggestions") if isinstance(result, dict) else []
                matches = {}
                for option in (options if isinstance(options, list) else [])[:100]:
                    if not isinstance(option, dict) or not isinstance(option.get("name"), str):
                        continue
                    path = option.get("geoLocationPath") or []
                    identifier = path[-1].get("providerLocationId") if path and isinstance(path[-1], dict) else None
                    if isinstance(identifier, str) and len(identifier) <= 1000:
                        matches.setdefault(option["name"], []).append({"text": option["name"], "providerLocationId": identifier})
                if variables.get("text"):
                    key = json.dumps(variables.get("locationTypes"), sort_keys=True)
                    self.location_options[key] = {label: values[0] for label, values in matches.items() if len(values) == 1}
                self.lookup_pending.discard(digest)
                self.lookup_observed.set()
            elif operation == "ApiSearchSchoolByCanonicalName" and success:
                result = data.get("result") or data.get("searchCanonicalSchools") or []
                if isinstance(result, list):
                    self.school_options.update({option["id"]: option["name"] for option in result[:100]
                        if isinstance(option, dict) and isinstance(option.get("id"), str) and isinstance(option.get("name"), str)
                        and len(option["id"]) <= 1000 and len(option["name"]) <= 1000})
                self.lookup_pending.discard(digest)
                self.lookup_observed.set()
            elif operation in {"ApiSetFormValue", "ApiSetFormValueToFile"}:
                root = _OPERATIONS[operation][1]
                form = data.get(root)
                valid = success and isinstance(form, dict) and form.get("id") == variables["formRenderIdentifier"] and self._accept_form(form)
                if self.active and all(variables.get(key) == self.active.get(key) for key in ("formRenderIdentifier", "path")):
                    self.active_success = bool(valid and not form.get("formErrors") and not form.get("errorMessages"))
                    self.done.set()
            elif operation in _FINALS:
                value = data.get("submitApplicationFormAction") or data.get("submitMultipleFormsAction") or data.get("submitSingleApplicationFormAction")
                result = value.get("applicationFormResult", value) if isinstance(value, dict) else {}
                surveys = value.get("surveyFormResults", []) if isinstance(value, dict) else []
                self.final_success = bool(success and result.get("__typename") == "FormSubmitSuccess"
                                          and all(item.get("__typename") == "FormSubmitSuccess" for item in surveys))
                self.final_done.set()
        except Exception:
            # Never persist remote response text, field values, tokens or keys.
            return

    async def _snapshot(self, control):
        if not ashby_document_allowed(control.page.url, self.url):
            raise ValueError("The application changed posting.")
        snapshot = await control.page.evaluate(_APPLICATION_SNAPSHOT, uuid.uuid4().hex)
        for field in snapshot["fields"]:
            matches = [(form, entry) for form in self.forms.values() for entry in form["paths"].values()
                       if entry.get("id") == field.get("entry_id") and entry["field"].get("path") == field.get("path")]
            field["binding"] = matches[0] if len(matches) == 1 else None
            if field["binding"]:
                entry = field["binding"][1]
                field["required"] = field.get("required") or entry.get("isRequired") is True and entry["field"].get("type") != "EducationHistory"
                if field.get("combobox") and entry["field"].get("type") == "ValueSelect":
                    value = (entry.get("fieldValue") or {}).get("value")
                    choices = [option for option in entry["field"].get("selectableValues", []) if option.get("value") == value]
                    field["selected_label"] = choices[0]["label"] if len(choices) == 1 and field.get("value") == choices[0]["label"] else ""
                elif field.get("combobox") and entry["field"].get("type") == "Location":
                    value = (entry.get("fieldValue") or {}).get("value")
                    field["selected_label"] = value["text"] if isinstance(value, dict) and value.get("text") == field.get("value") else ""
            if field["binding"] and field["type"] == "file":
                value = field["binding"][1].get("fieldValue")
                field["has_file"] = bool(value and value.get("filename") == (self.resume_path.name if self.resume_path else None))
        return snapshot

    async def _choose_combo(self, page, field, label):
        choices = await read_combobox(page, field, close=False, query=label)
        exact = [option for option in choices.get("options", []) if option.get("label") == label and not option.get("disabled")]
        if not choices.get("supported") or choices.get("loading") or len(exact) != 1:
            raise ValueError("The exact visible option is unavailable.")
        await page.locator(f'[data-jobradar-choice="{exact[0]["id"]}"]').click(timeout=5000)
        await page.wait_for_function("""({id,label})=>{
          const input=document.querySelector('[data-jobradar-field="'+id+'"]');
          return input && input.value===label && input.getAttribute('aria-expanded')!=='true';
        }""", arg={"id": field["id"], "label": label}, timeout=3000)

    def _manual_filled(self, entry):
        value = (entry.get("fieldValue") or {}).get("value")
        if entry["field"].get("type") == "Location":
            options = self.location_options.get(json.dumps(entry["field"].get("locationTypes"), sort_keys=True), {})
            return any(_same_value(value, option) for option in options.values())
        if entry["field"].get("type") == "EducationHistory":
            return bool(isinstance(value, list) and value and all(isinstance(record, dict)
                and (isinstance(record.get("schoolName"), str) and record["schoolName"].strip()
                     or isinstance(record.get("canonicalSchoolId"), str) and record["canonicalSchoolId"] in self.school_options) for record in value))
        return type(value) is bool or bool(value)

    def _manual_complete(self, control, field):
        binding = field.get("binding")
        if not control.human_acknowledged or not binding or (binding[0]["id"], field.get("path")) not in self.human_fields:
            return False
        entry = binding[1]
        return self._manual_filled(entry) and (entry["field"].get("type") in {"Location", "EducationHistory"}
                                              or _answer(field, control.profile, self.job) is None)

    def _gate(self, control, snapshot):
        if snapshot.get("login"):
            return control.pause("login", "Sign in on the retained employer browser; credentials are never stored or filled by Jobot.")
        if snapshot.get("visible_challenge") or snapshot.get("captcha") and not self.verification_acknowledged:
            self.pending_gate = ("verification", None)
            return control.pause("verification", "Complete the employer verification in its retained browser, then resume this application.")
        agreements = [field for field in snapshot["fields"] if protected_question(field.get("label", "")) and field.get("visible")]
        declarations = [match[0] for match in _DECLARATION.finditer(snapshot.get("body", ""))]
        signature = hashlib.sha256(json.dumps({"fields": [(field.get("label"), field["binding"][1].get("descriptionHtml") if field.get("binding") else None) for field in agreements],
                                               "declarations": declarations, "notice": self.posting.get("automatedProcessingLegalNotice")}, sort_keys=True).encode()).hexdigest()
        if (agreements or declarations or self.posting.get("automatedProcessingLegalNotice")) and signature not in self.agreement_acknowledged:
            self.pending_gate = ("agreement", signature)
            return control.pause("agreement", "Review the employer's statements and complete any required agreement controls in the retained browser, then resume.")
        for field in agreements:
            answered = field.get("checked") if field["type"] in {"checkbox", "radio"} else field.get("value")
            if field.get("required") and not answered:
                self.pending_gate = ("agreement", signature)
                return control.pause("agreement", "A required employer agreement still needs your action in the retained browser.")
        return None

    async def drive(self, control):
        self.control = control
        if control.human_acknowledged and self.pending_gate:
            kind, signature = self.pending_gate
            if kind == "verification":
                self.verification_acknowledged = True
            elif kind == "agreement":
                self.agreement_acknowledged.add(signature)
            self.pending_gate = None
        if self.final_sent or control.reserved:
            return control.result("uncertain", "This application already has one reserved submission attempt. Verify its outcome; automatic retry is disabled.")
        try:
            if not control.started:
                control.phase = "opening"
                await control.emit("opening_form")
                await control.page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
                control.started = True
            if not ashby_document_allowed(control.page.url, self.url):
                return control.pause("destination", "The employer page left the exact approved Ashby posting.")
            self.document_verified = self.document_verified or verify_ashby_document(self.url, await control.page.content())
            self.document_ready.set()
            if not self.document_verified:
                return control.pause("destination", "The public page could not be bound to the approved employer posting.")
            try:
                await asyncio.wait_for(self.metadata.wait(), timeout=8)
            except TimeoutError:
                return control.pause("unsupported_form", "The hosted renderer did not expose its exact form transport metadata. No applicant details were sent.")
            if self.error or not self.posting:
                return control.pause("destination", self.error or "The form metadata could not be verified.")
            for _ in range(4):
                snapshot = await self._snapshot(control)
                gate = self._gate(control, snapshot)
                if gate:
                    return gate
                observed_entries = {field.get("entry_id") for field in snapshot["fields"]}
                missing = [(form, entry) for form in self.forms.values() for entry in form["paths"].values()
                           if entry.get("isRequired") and not entry.get("hidden") and entry.get("id") not in observed_entries
                           and not (control.human_acknowledged and (form["id"], entry["field"]["path"]) in self.human_fields and self._manual_filled(entry))]
                if missing:
                    self.human_fields.update((form["id"], entry["field"]["path"]) for form, entry in missing)
                    return control.pause("unsupported_form", "A required employer control was not exposed by the supported visible form adapter. Complete it in the retained browser for review.")
                education = [(form, entry) for form in self.forms.values() for entry in form["paths"].values()
                             if entry.get("isRequired") and not entry.get("hidden") and entry["field"].get("type") == "EducationHistory"
                             and not (control.human_acknowledged and (form["id"], entry["field"]["path"]) in self.human_fields and self._manual_filled(entry))]
                if education:
                    self.human_fields.update((form["id"], entry["field"]["path"]) for form, entry in education)
                    return control.pause("review", "Complete the required education record using the employer's school lookup in the retained browser, then resume. No education facts will be invented.")
                remaining = [field for field in snapshot["fields"] if not self._manual_complete(control, field)]
                self.lookup_queries = {answer for field in remaining if field.get("binding") and field["binding"][1]["field"].get("type") == "Location"
                                       and isinstance((answer := _answer(field, control.profile, self.job)), str) and len(answer) <= 240}
                control.phase = "filling"
                snapshot = await enrich_comboboxes(control.page, {**snapshot, "fields": remaining}, lambda field: _answer(field, control.profile, self.job))
                fields = [field for field in snapshot["fields"] if not protected_question(field.get("label", ""))]
                plan = plan_form(fields, control.profile, self.resume_path, job=self.job)
                if plan.blockers:
                    pending_labels = {item.get("question") for item in plan.pending_questions}
                    self.human_fields = {(field["binding"][0]["id"], field.get("path")) for field in fields
                                         if field.get("binding") and (_label(field.get("label")) in pending_labels or _answer(field, control.profile, self.job) is None)}
                    return control.pause("questions", "The current form needs confirmed answers before continuing.", pending_questions=plan.pending_questions)
                if not plan.actions:
                    break
                for action in plan.actions:
                    if await control.verify() is False:
                        return control.pause("settings_changed", "The saved application settings changed. Review them before continuing.")
                    field = next(item for item in fields if item["id"] == action.field_id)
                    if not field.get("binding"):
                        return control.pause("unsupported_form", "A visible field could not be bound to the verified form definition.")
                    form, entry = field["binding"]
                    value = action.value
                    if field.get("ashby_yesno"):
                        value = {"Yes": True, "No": False}.get(action.value)
                    elif entry["field"].get("type") == "ValueSelect":
                        if field.get("type") == "radio":
                            value = field.get("value")
                        elif field.get("combobox"):
                            choices = [option for option in entry["field"].get("selectableValues", []) if option.get("label") == action.value]
                            if len(choices) != 1:
                                return control.pause("questions", "A dropdown choice no longer matches one employer option.", pending_questions=plan.pending_questions)
                            value = choices[0]["value"]
                    elif entry["field"].get("type") in {"Number", "Score"}:
                        value = float(value)
                    elif entry["field"].get("type") == "Location":
                        key = json.dumps(entry["field"].get("locationTypes"), sort_keys=True)
                        await self._await_lookup(lambda: action.value in self.location_options.get(key, {}))
                        value = self.location_options.get(key, {}).get(action.value)
                        if value is None:
                            return control.pause("questions", "The selected location no longer matches one observed employer suggestion.", pending_questions=plan.pending_questions)
                    self.active = {"formRenderIdentifier": form["id"], "formDefinitionIdentifier": form["sourceFormDefinitionId"],
                                   "path": entry["field"]["path"], "value": value, "sent": False}
                    self.done.clear()
                    self.active_success = False
                    element = control.page.locator(f'[data-jobradar-field="{action.field_id}"]')
                    if action.kind == "file":
                        if entry["field"].get("type") != "File" or not re.search(r"resume|\bcv\b", field.get("label", ""), re.I):
                            return control.pause("upload", "This upload requires a separately supplied document.")
                        self.active.update(file=True, contentType=mimetypes.guess_type(self.resume_path.name)[0] or "application/octet-stream")
                        self.upload, self.upload_sent, self.upload_success = None, False, False
                        self.upload_ready.clear()
                        self.upload_completed.clear()
                        control.phase = "uploading"
                        await control.emit("uploading_resume")
                        await element.set_input_files(str(self.resume_path))
                    else:
                        control.phase = "filling"
                        await control.emit("filling_fields")
                        if field.get("ashby_yesno"):
                            await choose_ashby_yesno(control.page, action.field_id, action.value)
                        elif action.kind == "fill":
                            await element.fill(action.value)
                            await element.blur()
                        elif action.kind == "select":
                            await element.select_option(action.value)
                        elif action.kind == "combobox":
                            await self._choose_combo(control.page, field, action.value)
                        elif action.kind == "check":
                            await element.set_checked(action.value)
                        else:
                            return control.pause("unsupported_form", "This field needs a supported visible-control adapter.")
                    try:
                        await asyncio.wait_for(self.done.wait(), timeout=10)
                    except TimeoutError:
                        return control.pause("transport", "The exact field write or upload was not confirmed by the employer renderer.")
                    if not self.active_success:
                        return control.pause("validation", "The employer did not accept a field value. Review the retained form.")
                    self.active = None
            else:
                return control.pause("dynamic_form", "The employer kept adding fields; review the current form before continuing.")
            if not await control.page.locator("form").evaluate_all("forms=>forms.every(form=>form.checkValidity())"):
                return control.pause("validation", "The employer reports missing or invalid fields.")
            snapshot = await self._snapshot(control)
            if getattr(control, "blocked_requests", []):
                return control.pause("transport", "An unexpected application request was blocked. Review the retained form before continuing.")
            buttons = snapshot.get("buttons", [])
            if len(buttons) != 1:
                return control.pause("unsupported_form", "The final application control was missing or ambiguous.")
            if await control.verify() is False or await control.reserve() is False:
                return control.pause("settings_changed", "Final approval or duplicate-attempt checks refused submission.")
            control.phase = "submitting"
            await control.emit("submitting")
            await control.page.locator(f'[data-jobradar-submit="{buttons[0]["id"]}"]').click(timeout=10_000)
            try:
                await asyncio.wait_for(self.final_done.wait(), timeout=15)
            except TimeoutError:
                return control.result("uncertain", "The single submission attempt has no verified receipt. Verify its outcome; do not retry automatically.")
            control.phase = "confirmation"
            if not self.final_success or getattr(control, "blocked_requests", []):
                return control.result("uncertain", "The employer did not return a successful application receipt. The attempt remains protected from retry.")
            return control.result("submitted", "Ashby returned a successful receipt for the exact application and any included surveys.",
                                  confirmation="FormSubmitSuccess from the pinned hosted submission operation.")
        except Exception:
            if self.final_sent or control.reserved:
                return control.result("uncertain", "The reserved submission could not be confirmed. Verify its outcome; automatic retry is disabled.")
            return control.pause("form", "The guarded Ashby workflow needs review in its retained browser.")
