"""A bounded, resumable adapter for the public SmartRecruiters OneClick SPA.

The operation paths below come from the public OneClick bundles advertised on
October 21, 2026: main.aab7b4f525d780af, runtime.bc6267fe0e8d281e,
403.f9dd0082506316f0 (application URL), 421.cb4719ed55be0f4e (resume
endpoint), and 286.386635b3fa30e481 (multipart/request payload). This is not
the authenticated partner Application API. New bundles/layouts fail closed.

Only the exact selected resume and one posting-bound application can be sent.
No general application, partner lead, resume parsing, social import, assessment,
arbitrary same-origin POST or autonomous legal agreement is permitted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .autofill import _answer, _option_match, plan_form
from .question_intents import protected_question
from .smartrecruiters_adapter import parse_smartrecruiters_url


_MAIN = "https://js-www.smartrecruiters.com/oneclick-ui/static/main.aab7b4f525d780af.js"
_RUNTIME = "https://js-www.smartrecruiters.com/oneclick-ui/static/runtime.bc6267fe0e8d281e.js"
_CONFIRMATION = re.compile(r"application (?:has been |was )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|your application)", re.I)
_DECLARATION = re.compile(r"by (?:clicking|submitting|applying)[\s\S]{0,300}(?:agree|consent|certify|acknowledge)|\bprivacy (?:notice|policy)\b", re.I)
_CHALLENGE = re.compile(r"verify (?:that )?you are human|please enable js and disable any ad blocker|complete (?:the |a )?captcha", re.I)


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources, self.inline, self.active = set(), [], False
    def handle_starttag(self, tag, attrs):
        if tag == "script":
            src = dict(attrs).get("src")
            if src:
                self.sources.add(src)
            self.active = not src
    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False
    def handle_data(self, data):
        if self.active:
            self.inline.append(data)


def public_smartrecruiters_url(url: str) -> str | None:
    parsed = parse_smartrecruiters_url(url)
    if not parsed or parsed["page_type"] != "posting":
        return None
    return f"https://api.smartrecruiters.com/v1/companies/{parsed['company']}/postings/{parsed['posting_id']}"


def _bootstrap(html: str) -> tuple[dict | None, set[str]]:
    if not isinstance(html, str) or len(html) > 3_000_000:
        return None, set()
    parser = _Scripts()
    parser.feed(html)
    found = []
    for script in parser.inline:
        for marker in re.finditer(r"(?:window\.)?__OC_CONTEXT__\s*=\s*", script):
            try:
                value, _ = json.JSONDecoder().raw_decode(script[marker.end():].lstrip())
                if isinstance(value, dict):
                    found.append(value)
            except ValueError:
                continue
    return (found[0] if len(found) == 1 else None), parser.sources


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _gate_stamp(snapshot):
    # Answers/checked states are excluded: the person must be able to act on the
    # same gate. New labels, links, options, documents or URL create a new gate.
    return hashlib.sha256(_canonical({
        "url": snapshot.get("url"), "links": snapshot.get("links", []),
        "body": snapshot.get("body", ""),
        "fields": [{k: f.get(k) for k in ("name", "label", "type", "required", "options")}
                   for f in snapshot.get("fields", [])],
    }).encode()).hexdigest()


# The SPL web components use open shadow roots. Only DOM controls, labels,
# options and public links are read; no Angular/component state is inspected.
SNAPSHOT = r"""token => {
  const roots=[document]; const all=[];
  for(let i=0;i<roots.length && i<500;i++) for(const node of roots[i].querySelectorAll('*')) {
    all.push(node); if(node.shadowRoot) roots.push(node.shadowRoot);
  }
  const visible=el=>!!(el.getClientRects().length && getComputedStyle(el).visibility!=='hidden' && getComputedStyle(el).display!=='none' && getComputedStyle(el).opacity!=='0');
  const text=el=>(el?.innerText||el?.textContent||'').replace(/\s+/g,' ').trim();
  const outer=el=>el.parentElement || el.getRootNode()?.host;
  const ancestor=(el,selector)=>{for(let n=el;n;n=outer(n))if(n.matches?.(selector))return n;return null};
  const host=el=>{for(let n=el;n;n=outer(n))if(n.tagName?.startsWith('SPL-'))return n;return el};
  const label=el=>{
    const root=el.getRootNode(); const labelled=(el.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>text(root.getElementById?.(id)||document.getElementById(id))).filter(Boolean).join(' ');
    const associated=Array.from(el.labels||[]).map(text).filter(Boolean).join(' ');
    const wrapper=host(el); const group=ancestor(el,'fieldset,[role=group],oc-yes-no-question');
    return (el.type==='radio' && (text(group?.querySelector('legend')) || group?.getAttribute('aria-label'))) || associated || labelled || el.getAttribute('aria-label') || wrapper.getAttribute('label') || text(wrapper.querySelector('label,[slot=label]')) || '';
  };
  const controls=all.filter(el=>el.matches('input,select,textarea') && !el.matches('input[type=hidden],input[type=button],input[type=submit],input[type=reset]'));
  const fields=controls.slice(0,400).map((el,i)=>{
    const wrapper=host(el); const combo=el.getAttribute('role')==='combobox';
    const id=token+'-'+i; el.setAttribute('data-jobradar-field',id);
    const required=!!(el.required||el.getAttribute('aria-required')==='true'||wrapper.hasAttribute('required'));
    const associated=Array.from(el.labels||[]).map(text).filter(Boolean).join(' ');
    const optionNodes=combo ? all.filter(o=>o.getAttribute('role')==='option' && visible(o) && (el.getAttribute('aria-controls')||'').split(/\s+/).some(id=>o.closest('[id="'+id+'"]'))) : [];
    return {id,name:el.name||el.id||'',label:label(el),option_label:associated||el.getAttribute('aria-label')||(el.type==='radio'?el.value:''),
      type:combo?'select':el.tagName==='SELECT'?'select':el.type||el.tagName.toLowerCase(),tag:el.tagName.toLowerCase(),
      visible:visible(el)||['file','checkbox','radio'].includes(el.type)&&wrapper!==el&&visible(wrapper),disabled:!!(el.disabled||wrapper.hasAttribute('disabled')),
      required,value:el.type==='password'?'':el.value||'',checked:!!el.checked,has_file:!!el.files?.length,multiple:!!el.multiple,combobox:combo,
      selected_label:combo?(wrapper.getAttribute('data-selected-value')||el.value||''):'',
      searchable:combo&&!el.readOnly,lookup_required:combo&&!el.readOnly,
      options:el.options?Array.from(el.options).map(o=>({value:o.value,label:text(o),disabled:o.disabled})):optionNodes.map(o=>({label:text(o),value:o.getAttribute('data-value')||text(o),disabled:o.getAttribute('aria-disabled')==='true'}))};
  });
  const buttons=all.filter(el=>el.matches('button,spl-button,input[type=submit],#st-apply')&&visible(el)&&!el.disabled&&!el.hasAttribute('disabled'))
    .filter(el=>!(el.matches('button')&&ancestor(outer(el),'spl-button'))).map((el,i)=>{
      el.setAttribute('data-jobradar-step',token+'-b'+i);return{id:token+'-b'+i,label:text(el)||el.value||el.getAttribute('aria-label')||''};
    });
  return {url:location.href,fields,buttons,body:(document.body.innerText||'').slice(0,160000),
    links:all.filter(el=>el.tagName==='A'&&visible(el)&&el.href).map(el=>({label:text(el),href:el.href})).slice(0,100),
    captcha:all.some(el=>el.matches('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="captcha-delivery"],.g-recaptcha,.h-captcha,[data-sitekey]')),
    login:fields.some(f=>f.visible&&f.type==='password'),
    invalid:controls.filter(el=>visible(el)&&!el.disabled&&el.willValidate&&!el.checkValidity()).map(el=>el.getAttribute('data-jobradar-field'))};
}"""


DEEP_CHOICES = r"""({id,token}) => {
  const roots=[document],all=[];
  for(let i=0;i<roots.length&&i<500;i++)for(const node of roots[i].querySelectorAll('*')){all.push(node);if(node.shadowRoot)roots.push(node.shadowRoot)}
  const visible=el=>!!(el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden'&&getComputedStyle(el).display!=='none'&&getComputedStyle(el).opacity!=='0');
  const input=all.find(node=>node.getAttribute('data-jobradar-field')===id);if(!input)return[];
  const outer=node=>node?.parentElement||node?.getRootNode()?.host;
  const within=(node,ancestor)=>{for(let n=node;n;n=outer(n))if(n===ancestor)return true;return false};
  const ids=((input.getAttribute('aria-controls')||'')+' '+(input.getAttribute('aria-owns')||'')).split(/\s+/).filter(Boolean);
  let host=input;for(let n=outer(input);n;n=outer(n))if(n.matches?.('spl-autocomplete,spl-select')){host=n;break}
  const boxes=all.filter(node=>node.getAttribute('role')==='listbox'&&visible(node)&&(ids.includes(node.id)||host!==input&&within(node,host)));
  if(boxes.length!==1)return[];
  return all.filter(node=>node.getAttribute('role')==='option'&&visible(node)&&within(node,boxes[0])).slice(0,300).map((node,i)=>{
    node.setAttribute('data-jobradar-choice',token+'-'+i);
    const label=(node.innerText||node.textContent||'').replace(/\s+/g,' ').trim();
    return{label,value:node.getAttribute('data-value')||label,disabled:node.getAttribute('aria-disabled')==='true',choice_id:token+'-'+i};
  });
}"""


class SmartRecruitersApplication:
    def __init__(self, job: dict, resume_path: Path | None, *, fixture=False):
        self.url = job.get("apply_url") or job.get("application_url") or job.get("url")
        self.initial = parse_smartrecruiters_url(self.url, fixture=fixture)
        if not self.initial:
            raise ValueError("A strict SmartRecruiters posting or OneClick URL is required.")
        self.fixture, self.job = fixture, job
        self.resume_path = Path(resume_path).resolve() if resume_path else None
        self.resume_sha = hashlib.sha256(self.resume_path.read_bytes()).hexdigest() if self.resume_path else None
        self.application_url = self.url if self.initial["page_type"] == "application" else None
        self.publication = self.initial["posting_id"] if self.application_url else None
        self.company_id = None
        self.context = None
        self.transport_ready = False
        self.upload_active = False
        self.upload_metadata = None
        self.upload_event, self.final_event = asyncio.Event(), asyncio.Event()
        self.final_armed, self.final_seen = False, False
        self.final_status, self.final_receipt = None, False
        self._control = None
        self._paused_gate, self._approved_gates = None, set()
        self._manual_fields = {}
        self._manual_controls = {}
        self._paused_kind = None
        self._snapshots = []
        self._screening = []
        self._location_details = []
        self._chosen_combos = {}
        self._challenge_frames = set()
        self._challenge_actions = set()
        self._challenge_fields = {}
        self._challenge_assets = set()

    def document_allowed(self, url):
        try:
            if _origin(url) != _origin(self.url):
                return False
            target = urlsplit(url)
            if target.username or target.password or target.fragment or unquote(target.path) != target.path:
                return False
            initial = urlsplit(self.url)
            if target.path.rstrip('/') == initial.path.rstrip('/'):
                return True
            if not self.application_url:
                return False
            path = urlsplit(self.application_url).path.rstrip('/')
            return target.path.rstrip('/') in {path, path + "/screening", path + "/success", path + "/expired"}
        except (TypeError, ValueError):
            return False

    def is_final_request(self, request):
        return bool(self.final_armed and self.transport_ready and request.method == "POST" and request.url == self._submit_url())

    def _submit_url(self):
        if not self.application_url or not self.publication:
            return ""
        p = urlsplit(self.application_url)
        return f"{p.scheme}://{p.netloc}/oneclick-ui/api/company/{self.initial['company']}/publication/{self.publication}/application"

    def _upload_url(self):
        if not self.company_id or not self.resume_path:
            return ""
        p = urlsplit(self.application_url)
        return f"{p.scheme}://{p.netloc}/oneclick-ui/api/company/{self.company_id}/attachment/resume"

    async def request_allowed(self, request, phase):
        try:
            p = urlsplit(request.url)
            if p.username or p.password or p.fragment or unquote(p.path) != p.path:
                return False
            if await self._challenge_request(request, phase):
                return True
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                if request.is_navigation_request():
                    return self.document_allowed(request.url)
                if self.fixture:
                    return _origin(request.url) == _origin(self.url)
                if p.scheme != "https" or p.port not in (None, 443):
                    return False
                if p.hostname in {"js-www.smartrecruiters.com", "c.smartrecruiters.com"}:
                    return p.path.startswith(("/oneclick-ui/static/", "/sr-jobad/static/"))
                if p.hostname == "av-www.smartrecruiters.com":
                    return p.path.startswith(("/oneclick-ui/static/assets/", "/sr-logo/"))
                if _origin(request.url) != _origin(self.url):
                    return False
                # Public application/config/location GETs carry no resume or
                # candidate session import; account/assessment endpoints differ.
                config = re.fullmatch(rf"/oneclick-ui/api/company/[A-Za-z0-9_-]{{1,80}}/publication/{re.escape(self.publication or '')}/config", p.path)
                return bool(config) or p.path.startswith("/oneclick-ui/api/location/") or p.path.startswith("/oneclick-ui/static/")
            if not self.transport_ready or not self._control:
                return False
            if not self.document_allowed(self._control.page.url):
                return False
            if phase == "uploading" and self.upload_active:
                return (await self.prepare_request(request, phase)) is not None
            if phase == "submitting" and self.is_final_request(request) and not self.final_seen:
                if self._valid_application_body(request):
                    self.final_seen = True
                    return True
            return False
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _challenge_origin(self, url):
        try:
            p = urlsplit(url)
            if p.username or p.password or p.fragment or unquote(p.path) != p.path:
                return False
            if self.fixture:
                return _origin(url) == _origin(self.url) and p.path.startswith("/captcha/")
            return (p.scheme == "https" and p.port in (None, 443) and
                    (bool(re.fullmatch(r"[a-z0-9-]+\.captcha-delivery\.com", p.hostname or "")) and p.path.startswith(("/captcha/", "/interstitial/"))
                     or p.hostname == "www.google.com" and p.path.startswith("/recaptcha/")
                     or p.hostname == "newassets.hcaptcha.com" and p.path.startswith("/captcha/v1/")))
        except (TypeError, ValueError):
            return False

    async def _capture_challenges(self):
        """Adopt public iframe/form advertisements, never solve the challenge.

        A provider suffix alone is insufficient. The frame must be embedded by
        the current bound document, and a write must be an exact action actually
        advertised by that frame's DOM. Unadvertised XHR verification transports
        remain blocked until a specific adapter is available.
        """
        if not self._control or not self.document_allowed(self._control.page.url):
            return
        page = self._control.page
        try:
            sources = await page.locator("iframe[src]").evaluate_all("nodes=>nodes.map(node=>node.src)")
            self._challenge_frames = {src for src in sources[:20] if self._challenge_origin(src)}
            self._challenge_actions.clear()
            self._challenge_fields.clear()
            self._challenge_assets.clear()
            scripts = await page.locator("script[src]").evaluate_all("nodes=>nodes.map(node=>node.src)")
            for src in scripts[:100]:
                if src in {"https://ct.captcha-delivery.com/c.js", "https://ct.captcha-delivery.com/i.js"}:
                    self._challenge_assets.add(src)
            for frame in page.frames:
                if frame == page.main_frame or frame.url not in self._challenge_frames:
                    continue
                forms = await frame.locator("form[action]").evaluate_all("nodes=>nodes.map(node=>({action:node.action,method:node.method.toUpperCase(),privateControl:!!node.querySelector('input[type=file],input[type=password],input[type=email]'),fields:Array.from(node.elements).map(field=>field.name).filter(Boolean)}))")
                for form in forms[:20]:
                    action = form.get("action")
                    if not form.get("privateControl") and form.get("method") in {"POST", "GET"} and self._challenge_origin(action) and _origin(action) == _origin(frame.url):
                        key = (frame.url, form["method"], action)
                        self._challenge_actions.add(key)
                        self._challenge_fields[key] = set(form.get("fields", []))
                assets = await frame.locator("script[src],img[src],link[href]").evaluate_all("nodes=>nodes.map(node=>node.src||node.href)")
                for asset in assets[:100]:
                    if _origin(asset) == _origin(frame.url) and self._challenge_origin(asset):
                        self._challenge_assets.add(asset)
        except Exception:
            # An unloaded/cross-origin/closed frame grants no extra capability.
            return

    async def _challenge_request(self, request, phase):
        if not self._control or not self.document_allowed(self._control.page.url):
            return False
        p = urlsplit(request.url)
        relevant = self._challenge_origin(request.url) or p.hostname == "ct.captcha-delivery.com"
        if not relevant:
            return False
        await self._capture_challenges()
        frame = getattr(request, "frame", None)
        if request.method in {"GET", "HEAD"} and request.url in self._challenge_frames | self._challenge_assets:
            if request.url in self._challenge_frames and frame == self._control.page.main_frame:
                return False
            return True
        frame_url = getattr(frame, "url", None)
        if (phase == "human" and frame_url in self._challenge_frames
                and (frame_url, request.method, request.url) in self._challenge_actions
                and request.method in {"GET", "POST"}):
            body = request.post_data_buffer
            if request.method == "GET":
                return body is None
            if not isinstance(body, bytes) or len(body) > 128_000 or not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
                return False
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
            return set(fields) <= self._challenge_fields.get((frame_url, request.method, request.url), set()) and len(fields) <= 30
        return False

    def ignore_request(self, request):
        """Nonessential telemetry may be dropped without granting a capability."""
        try:
            p = urlsplit(request.url)
            return (p.hostname in {"www.google-analytics.com", "www.googletagmanager.com", "stats.g.doubleclick.net", "bat.bing.com"}
                    or _origin(request.url) == _origin(self.url) and p.path in {"/oneclick-ui/api/server-tracking", "/oneclick-ui/api/tracking"})
        except (ValueError, TypeError):
            return False

    async def prepare_request(self, request, phase):
        """Restore only CDP's omitted file bytes, then validate the full body."""
        if not (phase == "uploading" and self.upload_active and self.transport_ready and self.resume_path
                and request.method == "POST" and request.url.split("?", 1)[0] == self._upload_url()):
            return None
        try:
            if parse_qs(urlsplit(request.url).query) != {"filename": [self.resume_path.name]}:
                return None
            data = request.post_data_buffer
            content_type = request.headers.get("content-type", "")
            if not isinstance(data, bytes) or len(data) > 8_000_000 or not content_type.startswith("multipart/form-data;"):
                return None
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + data)
            parts = list(message.iter_parts())
            if len(parts) != 1 or parts[0].get_param("name", header="content-disposition") != "file" or parts[0].get_filename() != self.resume_path.name:
                return None
            payload = parts[0].get_payload(decode=True)
            selected = self.resume_path.read_bytes()
            if hashlib.sha256(selected).hexdigest() != self.resume_sha:
                return None
            if payload == selected:
                return {"post_data": data}
            if payload != b"":
                return None
            boundary = message.get_boundary()
            if not boundary or not re.fullmatch(r"[A-Za-z0-9'()+_,./:=?-]{1,200}", boundary):
                return None
            header = parts[0].as_bytes(policy=policy.SMTP).split(b"\r\n\r\n", 1)[0]
            rebuilt = b"--" + boundary.encode() + b"\r\n" + header + b"\r\n\r\n" + selected + b"\r\n--" + boundary.encode() + b"--\r\n"
            return {"post_data": rebuilt}
        except (ValueError, OSError, TypeError):
            return None

    def _records_match(self, records, kind):
        if not isinstance(records, list) or len(records) > 40:
            return False
        source = self._control.profile.get("application_profile", {}).get(kind, [])
        for item in records:
            if not isinstance(item, dict):
                return False
            keys = {"institution", "degree", "major", "current", "location", "description", "startDate", "endDate"} if kind == "education" else {"company", "title", "description", "location", "current", "startDate", "endDate"}
            if set(item) - keys or type(item.get("current", False)) is not bool:
                return False
            name_key = "institution" if kind == "education" else "company"
            matches = [r for r in source if r.get(name_key) == item.get(name_key)]
            if len(matches) != 1:
                return False
            record = matches[0]
            mapped = {"degree": "degree", "major": "discipline"} if kind == "education" else {"title": "title"}
            if any(item.get(k) not in (None, "", record.get(v)) for k, v in mapped.items()):
                return False
            current = record.get("status") == "current" if kind == "education" else record.get("current") is True
            if item.get("current", False) != current or item.get("description") not in (None, "") or item.get("location") not in (None, "", {}):
                return False
            for key, month in (("startDate", record.get("start_month")), ("endDate", record.get("end_month"))):
                value = item.get(key)
                if value in (None, "", {}):
                    continue
                if not month or not isinstance(value, dict) or set(value) - {"year", "month", "day"}:
                    return False
                if type(value.get("year")) is not int or type(value.get("month")) is not int:
                    return False
                if value.get("year") != int(month[:4]) or value.get("month") != int(month[5:]) or value.get("day") not in (None, ""):
                    return False
        return True

    def _valid_application_body(self, request):
        try:
            data = request.post_data_buffer
            if not isinstance(data, bytes) or len(data) > 500_000 or "application/json" not in request.headers.get("content-type", ""):
                return False
            body = json.loads(data)
            if not isinstance(body, dict) or set(body) - {"profile", "metadata", "messageToEmployer", "answers", "consent", "seekToken", "consentDecision", "creator", "serverTrackingParams", "utmTracking", "seonSessionId", "enableDataDomeIntegration"}:
                return False
            profile = body.get("profile")
            if not isinstance(profile, dict) or set(profile) - {"firstName", "lastName", "email", "phoneNumber", "location", "experience", "education", "web", "resume", "avatar", "attachments"}:
                return False
            approved = self._control.profile
            if any(not isinstance(approved.get(k), str) or not approved[k].strip() for k in ("first_name", "last_name", "email")):
                return False
            if any(profile.get(k) != approved.get(v) for k, v in (("firstName", "first_name"), ("lastName", "last_name"), ("email", "email"))):
                return False
            phone = profile.get("phoneNumber")
            if phone not in (None, "") and (not isinstance(phone, str) or re.sub(r"\D", "", phone) != re.sub(r"\D", "", approved.get("phone", ""))):
                return False
            if profile.get("location") not in (None, "", {}) and profile["location"] not in self._location_details:
                return False
            if profile.get("location") in (None, "", {}) and any(f["required"] and re.fullmatch(r"(?:current )?city(?:\s*\*)?", f["label"], re.I) for s in self._snapshots for f in s["fields"]):
                return False
            if profile.get("avatar") not in (None, "") or profile.get("attachments") not in (None, []):
                return False
            if profile.get("resume") != self.upload_metadata or self.resume_path and not self.upload_metadata:
                return False
            if not self._records_match(profile.get("experience", []), "employment") or not self._records_match(profile.get("education", []), "education"):
                return False
            web = profile.get("web", {})
            allowed_web = {"linkedIn": approved.get("linkedin"), "website": approved.get("portfolio"), "github": approved.get("github"), "facebook": None, "twitter": None}
            if not isinstance(web, dict) or any(key not in allowed_web or value not in (None, "", allowed_web.get(key)) for key, value in web.items()):
                return False
            if body.get("creator") not in (None, "") or body.get("seekToken") not in (None, ""):
                return False
            if body.get("messageToEmployer") not in (None, "") and body["messageToEmployer"] not in self._approved_values():
                return False
            if not self._screening_matches(body.get("answers", [])):
                return False
            if body.get("consent") is not True:
                return False
            if body.get("consentDecision") not in (None, {}) and not self._consent_matches(body["consentDecision"]):
                return False
            metadata = body.get("metadata", {})
            if not isinstance(metadata, dict) or metadata.get("internalApplication") not in (None, False) or any(k not in {"lid", "rsid", "sid", "internalApplication", "utmSource", "dataFeed", "correlationId"} for k in metadata):
                return False
            if metadata.get("dataFeed") not in (None, "FEED_DEFAULT"):
                return False
            queries = parse_qs(urlsplit(self.application_url).query)
            for key, query in (("lid", "lid"), ("rsid", "rsid"), ("sid", "sid"), ("correlationId", "correlation_id")):
                if metadata.get(key) not in (None, "", *queries.get(query, [])):
                    return False
            if metadata.get("utmSource") not in (None, "other", "cp job search", "cp job recommendation", "cp saved jobs", "job ad apply with smartr profile"):
                return False
            if body.get("serverTrackingParams") not in (None, {}):
                return False
            tracking = body.get("utmTracking")
            if tracking is not None:
                if not isinstance(tracking, dict) or set(tracking) - {"utmSource", "utmMedium", "utmCampaign", "utmTerm", "utmContent"}:
                    return False
                for key, query in (("utmSource", "utm_source"), ("utmMedium", "utm_medium"), ("utmCampaign", "utm_campaign"), ("utmTerm", "utm_term"), ("utmContent", "utm_content")):
                    if tracking.get(key) not in (None, "", *queries.get(query, [])):
                        return False
            if body.get("enableDataDomeIntegration") not in (None, True, False) or not isinstance(body.get("seonSessionId", ""), str) or len(body.get("seonSessionId", "")) > 12000:
                return False
            return True
        except (ValueError, TypeError, AttributeError, KeyError):
            return False

    def _approved_values(self):
        values = []
        for snapshot in self._snapshots:
            for field in snapshot["fields"]:
                answer = _answer(field, self._control.profile, self.job)
                if isinstance(answer, str) and answer:
                    values.append(answer)
        values.extend(self._manual_fields.values())
        return values

    def _screening_matches(self, answers):
        if not isinstance(answers, list) or len(answers) > 200:
            return False
        definitions = {q.get("id"): q for q in self._screening if isinstance(q, dict)}
        seen = set()
        for answer in answers:
            if not isinstance(answer, dict) or set(answer) != {"id", "records"} or answer.get("id") in seen:
                return False
            seen.add(answer.get("id"))
            question = definitions.get(answer.get("id"))
            if not question or question.get("repeatable") or len(question.get("fields", [])) != 1:
                return False
            field = question["fields"][0]
            if protected_question(question.get("label", "")):
                # The person, never this adapter, must check a declaration.
                # Bind its observed checkbox to the exact public question/field
                # schema rather than inferring a generic Yes from an approval.
                allowed_names = {question.get("id"), field.get("id"), f"{question.get('id')}.{field.get('id')}"}
                label = " ".join(str(question.get("label", "")).split()).casefold()
                observed = [item for snapshot in self._snapshots if _gate_stamp(snapshot) in self._approved_gates
                            for item in snapshot["fields"] if item["type"] == "checkbox" and item.get("visible") is not False and not item.get("disabled")
                            and item.get("name") in allowed_names
                            and " ".join(item["label"].split()).casefold() == label]
                choices = field.get("values", [])
                if field.get("type") != "CHECKBOX" or len(choices) != 1 or not observed or observed[-1].get("checked") is not True:
                    return False
                expected = choices[0].get("id")
                if not isinstance(expected, str) or answer.get("records") != [{"fields": {field.get("id"): [expected]}}]:
                    return False
                continue
            expected = _answer({"label": question.get("label", ""), "name": "", "type": "text"}, self._control.profile, self.job)
            if expected is None:
                expected = self._manual_fields.get(question.get("label"))
            options = [{"label": o.get("label"), "value": o.get("id")} for o in field.get("values", [])]
            if options:
                selected = _option_match(options, expected)
                if selected is None:
                    return False
                expected = selected["value"]
            if expected is None or answer.get("records") != [{"fields": {field.get("id"): [str(expected)]}}]:
                return False
        return True

    def _consent_matches(self, decision):
        if not isinstance(decision, dict) or set(decision) - {"changingConsentAvailable", "confirmedByScope", "confirmedByCustomScopeId"} or not self._approved_gates:
            return False
        if "changingConsentAvailable" in decision and type(decision["changingConsentAvailable"]) is not bool:
            return False
        consent = (self.context or {}).get("consent", {})
        if not isinstance(consent, dict):
            return False
        if type(consent.get("changingConsentAvailable")) is bool and decision.get("changingConsentAvailable") != consent["changingConsentAvailable"]:
            return False
        configs = consent.get("consentScopeConfigs", [])
        if not isinstance(configs, list):
            return False
        fields = [f for s in self._snapshots if _gate_stamp(s) in self._approved_gates
                  for f in s["fields"] if f["type"] == "checkbox" and f.get("visible") is not False and not f.get("disabled") and protected_question(f["label"])]
        for name in ("confirmedByScope", "confirmedByCustomScopeId"):
            choices = decision.get(name, {})
            if not isinstance(choices, dict) or len(choices) > 20:
                return False
            config_key = "scope" if name == "confirmedByScope" else "customScopeId"
            known = {item[config_key] for item in configs if isinstance(item, dict) and isinstance(item.get(config_key), str)}
            for scope, value in choices.items():
                if not isinstance(scope, str) or type(value) is not bool:
                    return False
                matches = [f for f in fields if f.get("name") == scope]
                if not matches and scope not in known:
                    return False
                # A false decision for an explicitly advertised scope is not an
                # opt-in. A true decision needs an unambiguous observed control.
                if not matches and value is False:
                    continue
                unique_fields = {(f.get("name"), f["label"]): f for f in fields}
                if not matches and known == {scope} and len(choices) == 1 and len(unique_fields) == 1:
                    matches = list(unique_fields.values())
                if not matches or any(f["checked"] != value for f in matches[-1:]):
                    return False
        return True

    async def observe_response(self, response):
        try:
            if response.request.method == "POST" and response.url.split("?", 1)[0] == self._upload_url() and self.upload_active:
                if 200 <= response.status < 300:
                    raw = await response.body()
                    value = json.loads(raw) if len(raw) <= 32000 else None
                    if isinstance(value, dict) and value.get("attachmentType") == "RESUME" and isinstance(value.get("fileId"), str) and value["fileId"]:
                        self.upload_metadata = value
                self.upload_event.set()
            if response.request.method == "POST" and response.url == self._submit_url() and self.final_seen:
                self.final_status = response.status
                raw = await response.body()
                value = json.loads(raw) if len(raw) <= 64000 else None
                # A 2xx plus an actual application identifier is necessary; text
                # success-like words or an HTTP500 never establish a receipt.
                self.final_receipt = 200 <= response.status < 300 and isinstance(value, dict) and any(isinstance(value.get(k), str) and value[k] for k in ("applicationId", "applicationHash"))
                self.final_event.set()
            if response.request.method == "GET" and "/oneclick-ui/api/location/details/" in response.url and 200 <= response.status < 300 and _origin(response.url) == _origin(self.url):
                value = await response.json()
                if isinstance(value, dict) and len(_canonical(value)) < 10000:
                    self._location_details.append(value)
        except (ValueError, TypeError, AttributeError):
            return

    async def _bind(self, page):
        html = await page.content()
        context, scripts = _bootstrap(html)
        if not context or not (self.fixture or {_MAIN, _RUNTIME} <= scripts):
            return False
        company = context.get("company")
        if not isinstance(company, dict) or company.get("companyIdentifier") != self.initial["company"]:
            return False
        publication = context.get("publicationUuid") or context.get("publicationId")
        if publication != self.publication or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(company.get("id", ""))):
            return False
        if context.get("job", {}).get("generalApplication") or context.get("hasInlineAssessments"):
            return False
        self.context, self.company_id = context, company["id"]
        self._screening = context.get("screeningConfiguration", {}).get("questions", [])
        self.transport_ready = True
        return True

    async def _combo_choices(self, control, field, *, query=None, close=True):
        locator = control.page.locator(f'[data-jobradar-field="{field["id"]}"]')
        await self._verify(control)
        control.phase = "filling"
        await locator.click(timeout=3000)
        if isinstance(query, str) and query and field.get("searchable"):
            await self._verify(control)
            await locator.fill(query[:240])
        try:
            await control.page.wait_for_function("() => {const roots=[document];for(let i=0;i<roots.length&&i<500;i++)for(const node of roots[i].querySelectorAll('*')){if(node.shadowRoot)roots.push(node.shadowRoot);if(node.getAttribute('role')==='option'&&node.getClientRects().length)return true}return false}", timeout=3000)
        except Exception:
            pass
        options = await control.page.evaluate(DEEP_CHOICES, {"id": field["id"], "token": uuid.uuid4().hex})
        if close:
            await locator.press("Escape")
        return options

    async def _verify(self, control):
        await control.verify()
        if not self.document_allowed(control.page.url):
            raise ValueError("The application browser left the bound publication.")
        if self.resume_path and hashlib.sha256(self.resume_path.read_bytes()).hexdigest() != self.resume_sha:
            raise ValueError("The selected resume changed during the application.")

    async def _enrich(self, control, snapshot):
        for field in snapshot["fields"]:
            if not (field.get("combobox") and field.get("visible") and not field.get("disabled")):
                continue
            key = (field["name"], field["label"])
            previous = self._chosen_combos.get(key)
            query = _answer(field, control.profile, self.job)
            if previous and field["value"] in ("", previous) and _option_match([{"label": previous, "value": previous}], query):
                field["selected_label"] = previous
                field["options"] = [{"label": previous, "value": previous, "disabled": False}]
                continue
            options = await self._combo_choices(control, field, query=query)
            field["options"] = [{k: o[k] for k in ("label", "value", "disabled")} for o in options]
        return snapshot

    async def _choose_combo(self, control, field, label):
        options = await self._combo_choices(control, field, query=label, close=False)
        matches = [o for o in options if o["label"] == label and not o["disabled"]]
        if len(matches) != 1:
            raise ValueError("No unique live option for the approved choice.")
        await self._verify(control)
        await control.page.locator(f'[data-jobradar-choice="{matches[0]["choice_id"]}"]').click()
        self._chosen_combos[(field["name"], field["label"])] = label

    async def _open_application(self, control):
        if self.initial["page_type"] == "application":
            return True
        html = await control.page.content()
        block = re.search(r"window\.ONECLICKDATA\s*=\s*\{([\s\S]{0,5000}?)\};", html)
        if not block:
            return False
        body = block[1]
        board = re.search(r"\bcident\s*:\s*['\"]([A-Za-z0-9_-]+)['\"]", body)
        posting = re.search(r"\bpid\s*:\s*([1-9][0-9]*)", body)
        publication = re.search(r"\bpuuid\s*:\s*['\"]([a-fA-F0-9-]{36})['\"]", body)
        if not board or not posting or not publication or board[1] != self.initial["company"] or posting[1] != self.initial["posting_id"]:
            return False
        p = urlsplit(self.url)
        self.publication = publication[1].lower()
        self.application_url = f"{p.scheme}://{p.netloc}/oneclick-ui/company/{board[1]}/publication/{self.publication}"
        if not parse_smartrecruiters_url(self.application_url, fixture=self.fixture):
            return False
        button = control.page.locator("#st-apply")
        if await button.count() != 1 or (await button.inner_text()).strip().lower() not in {"i'm interested", "i’m interested", "apply"}:
            return False
        await self._verify(control)
        control.phase = "next"
        await button.click()
        await control.page.wait_for_url(lambda url: str(url).split("?", 1)[0] == self.application_url, timeout=10000)
        return True

    async def drive(self, control):
        self._control = control
        page = control.page
        try:
            if self.final_armed:
                return control.result("uncertain", "This application already reached its single final attempt. Verify the employer outcome; do not retry.")
            if not control.started:
                control.phase = "opening"
                await control.emit("opening_form")
                await page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
                control.started = True
            if not self.document_allowed(page.url):
                return control.pause("destination", "The browser changed to an unbound application destination.")
            first = await page.evaluate(SNAPSHOT, uuid.uuid4().hex)
            if first["captcha"] or _CHALLENGE.search(first["body"]):
                await self._capture_challenges()
                return control.pause("verification", "Complete the visible browser verification, then resume this same application.")
            if first["login"]:
                return control.pause("login", "Sign in manually in the retained employer browser, then resume.")
            if not self.application_url:
                if not await self._open_application(control):
                    return control.pause("transport", "The posting did not advertise an exact matching OneClick publication.")
            if not self.transport_ready and not await self._bind(page):
                return control.pause("transport", "The public OneClick publication or current transport version could not be verified. No applicant data was sent.")
            for _step in range(8):
                await self._verify(control)
                snapshot = await page.evaluate(SNAPSHOT, uuid.uuid4().hex)
                if not self.document_allowed(page.url):
                    return control.pause("destination", "The browser left the bound OneClick publication.")
                if snapshot["captcha"] or _CHALLENGE.search(snapshot["body"]):
                    await self._capture_challenges()
                    return control.pause("verification", "Complete the visible verification, then resume this same browser.")
                if snapshot["login"]:
                    return control.pause("login", "Complete the employer login manually, then resume.")
                if re.search(r"this job has expired|position is no longer available", snapshot["body"], re.I):
                    return control.result("needs_attention", "The employer posting is closed.")
                stamp = _gate_stamp(snapshot)
                if self._paused_gate == stamp and control.human_acknowledged:
                    self._approved_gates.add(stamp)
                    if self._paused_kind == "questions":
                        for item in snapshot["fields"]:
                            if item["visible"] and not item["disabled"] and _answer(item, control.profile, self.job) is None and item["type"] not in {"file", "password", "hidden"}:
                                value = item["value"]
                                if item["type"] in {"checkbox", "radio"}:
                                    if not item["checked"]:
                                        continue
                                    value = item.get("option_label") or item["value"]
                                elif item["type"] == "select":
                                    value = next((o["label"] for o in item["options"] if o.get("value") == item["value"] and not o.get("disabled")), "")
                                if value:
                                    self._manual_fields[item["label"]] = value
                                    self._manual_controls[(stamp, item["name"], item["label"], item["type"])] = (item["value"], item["checked"])
                legal = any(protected_question(f["label"]) for f in snapshot["fields"] if f["visible"])
                if (legal or _DECLARATION.search(snapshot["body"])) and stamp not in self._approved_gates:
                    self._paused_gate = stamp
                    self._paused_kind = "agreement"
                    return control.pause("agreement", "Review and complete this page's agreement or declaration yourself, then confirm and resume. New documents require a new review.")
                fields = []
                for item in snapshot["fields"]:
                    if protected_question(item["label"]) and stamp in self._approved_gates:
                        if item["required"] and item["type"] == "checkbox" and not item["checked"]:
                            return control.pause("agreement", "The required agreement remains unchecked. Complete it yourself before resuming.")
                        continue
                    manual = self._manual_controls.get((stamp, item["name"], item["label"], item["type"]))
                    if manual == (item["value"], item["checked"]):
                        continue
                    if self.upload_metadata and item["type"] == "file" and re.search(r"resume|\bcv\b", item["label"], re.I):
                        item["has_file"] = True
                    fields.append(item)
                await control.emit("inspecting_form", field_count=len(fields))
                current = {**snapshot, "fields": fields}
                current = await self._enrich(control, current)
                plan = plan_form(current["fields"], control.profile, self.resume_path, job=self.job)
                if plan.blockers:
                    self._paused_gate, self._paused_kind = stamp, "questions"
                    return control.pause("question", "The application needs an explicit answer or an exact available choice.", pending_questions=plan.pending_questions)
                if plan.actions:
                    for action in plan.actions:
                        await self._verify(control)
                        locator = page.locator(f'[data-jobradar-field="{action.field_id}"]')
                        control.phase = "uploading" if action.kind == "file" else "filling"
                        await control.emit("uploading_resume" if action.kind == "file" else "filling_fields")
                        if action.kind == "fill":
                            await locator.fill(action.value)
                        elif action.kind == "select":
                            await locator.select_option(value=action.value)
                        elif action.kind == "combobox":
                            field = next(f for f in current["fields"] if f["id"] == action.field_id)
                            await self._choose_combo(control, field, action.value)
                        elif action.kind == "check":
                            await locator.set_checked(action.value)
                        elif action.kind == "file":
                            self.upload_event.clear()
                            self.upload_active = True
                            try:
                                await locator.set_input_files(str(self.resume_path))
                                await asyncio.wait_for(self.upload_event.wait(), 12)
                            finally:
                                self.upload_active = False
                            if self.upload_metadata is None:
                                return control.pause("upload", "The exact selected resume upload was not confirmed. Review the retained form.")
                    continue
                if getattr(control, "blocked_requests", []):
                    return control.pause("transport", "An unapproved request was blocked. Review the retained form before proceeding.")
                self._snapshots.append(snapshot)
                buttons = snapshot["buttons"]
                next_buttons = [b for b in buttons if re.fullmatch(r"next|continue|review(?: application)?", b["label"].strip(), re.I)]
                final_buttons = [b for b in buttons if re.fullmatch(r"submit(?: application)?|send application|apply", b["label"].strip(), re.I)]
                if len(next_buttons) == 1 and not final_buttons:
                    if snapshot["invalid"]:
                        return control.pause("validation", "The employer page still reports invalid or incomplete fields.")
                    await self._verify(control)
                    control.phase = "next"
                    before = page.url
                    await page.locator(f'[data-jobradar-step="{next_buttons[0]["id"]}"]').click()
                    await page.wait_for_function("before => location.href!==before || /submit application|send application/i.test(document.body.innerText)", arg=before, timeout=5000)
                    continue
                if len(final_buttons) != 1 or next_buttons:
                    return control.pause("layout", "No unique final application button was identified. Complete or review the retained employer form.")
                if snapshot["invalid"]:
                    return control.pause("validation", "The employer reports missing or invalid application fields.")
                await self._verify(control)
                if not await control.reserve():
                    return control.pause("approval", "The final approval or single-attempt reservation was refused.")
                checked = await page.evaluate(SNAPSHOT, uuid.uuid4().hex)
                if _gate_stamp(checked) != stamp or _canonical([(f["label"], f["value"], f["checked"]) for f in checked["fields"]]) != _canonical([(f["label"], f["value"], f["checked"]) for f in snapshot["fields"]]):
                    return control.result("uncertain", "The form changed during final review. No automatic retry is allowed.")
                control.phase, self.final_armed = "submitting", True
                await control.emit("submitting")
                final_checked = [b for b in checked["buttons"] if b["label"] == final_buttons[0]["label"]]
                if len(final_checked) != 1:
                    return control.result("uncertain", "The final application button changed during review. Do not retry automatically.")
                await page.locator(f'[data-jobradar-step="{final_checked[0]["id"]}"]').click()
                await asyncio.wait_for(self.final_event.wait(), 15)
                control.phase = "confirmation"
                await control.emit("awaiting_confirmation")
                await page.wait_for_function("() => /thank you for (applying|your application)|application (has been |was )?(successfully )?(submitted|received)/i.test(document.body.innerText)", timeout=8000)
                if not self.document_allowed(page.url) or not self.final_seen or not self.final_receipt or getattr(control, "blocked_requests", []):
                    return control.result("uncertain", "The single application attempt was not reliably confirmed. Verify the employer outcome; do not retry.")
                return control.result("submitted", "The bound OneClick application returned an identifier and displayed confirmation.")
            return control.pause("layout", "The form kept changing or exceeded the bounded step count. Review this retained browser.")
        except Exception:
            if self.final_armed:
                return control.result("uncertain", "The single final application attempt could not be confirmed. Do not retry automatically.")
            return control.pause("form", "The bounded OneClick workflow needs review in the retained browser. No final application was sent.")
