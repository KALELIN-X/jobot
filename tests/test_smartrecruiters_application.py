"""Real Chromium / loopback fixtures for the bounded public OneClick provider.

Fixture operation paths and payload shapes follow the public bundle contract;
the pages do not claim to reproduce every live SmartRecruiters customization.
No employer is contacted and no production database is opened.
"""

import asyncio
import hashlib
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from jobradar.smartrecruiters_application import SNAPSHOT, SmartRecruitersApplication, _bootstrap, _gate_stamp, public_smartrecruiters_url


PUBLICATION = "2105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3"
BASE = f"/oneclick-ui/company/Example/publication/{PUBLICATION}"
SUBMIT = f"/oneclick-ui/api/company/Example/publication/{PUBLICATION}/application"
UPLOAD = "/oneclick-ui/api/company/company-123/attachment/resume"
PROFILE = {"first_name": "Alex", "last_name": "Example", "email": "alex@example.test", "phone": "+15555550123",
           "work_authorized": True, "needs_sponsorship": False, "answers": {"Preferred shift?": "Day"}}


def spa(variant):
    context = {"company": {"id": "company-123", "companyIdentifier": "Example"}, "publicationUuid": PUBLICATION,
               "job": {"generalApplication": False}, "hasInlineAssessments": False,
               "screeningConfiguration": {"questions": [{"id": "shift-question", "label": "Preferred shift?", "repeatable": False,
                    "fields": [{"id": "value", "type": "SINGLE_SELECT", "required": True,
                                "values": [{"id": "day", "label": "Day"}, {"id": "night", "label": "Night"}]}]}]}}
    if variant == "mismatched_bootstrap":
        context["publicationUuid"] = "7105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3"
    if variant == "legal_screening":
        context["screeningConfiguration"]["questions"].append({"id":"legal-screening","label":"Do you acknowledge the candidate agreement?","repeatable":False,"fields":[{"id":"confirm","type":"CHECKBOX","required":True,"values":[{"id":"confirmed","label":"I acknowledge"}]}]})
    return ("""<!doctype html><html><body><main id='app'></main><script>
    window.__OC_CONTEXT__=BOOTSTRAP_JSON;
    const variant=VARIANT;
    let resume=null, saved={}, locationValue=null;
    function first(){
      app.innerHTML=`<h1>Personal information</h1>
      <label for='first'>First name</label><input id='first' required>
      <label for='last'>Last name</label><input id='last' required>
      <label for='email'>Email</label><input id='email' type='email' required>
      <label for='phone'>Phone</label><input id='phone' type='tel'>
      <oc-resume-upload><label for='resume'>Resume</label><input id='resume' type='file' required></oc-resume-upload>
      <button id='next' type='button'>Next</button>`;
      document.getElementById('resume').onchange=async event=>{
        const data=new FormData();data.append('file',event.target.files[0]);
        const endpoint=variant==='wrong_upload'?'/oneclick-ui/api/company/other-company/attachment/resume':UPLOAD;
        const response=await fetch(endpoint+'?filename='+encodeURIComponent(event.target.files[0].name),{method:'POST',body:data});
        if(response.ok)resume=await response.json();
      };
      document.getElementById('next').onclick=()=>{
        if(!Array.from(document.querySelectorAll('input')).every(input=>input.checkValidity()))return;
        const first=document.getElementById('first')||document.querySelector('spl-input')?.shadowRoot.getElementById('first');
        saved={firstName:first.value,lastName:document.getElementById('last').value,email:document.getElementById('email').value,phoneNumber:document.getElementById('phone').value,...locationValue?{location:locationValue}:{}};
        history.pushState({},'',BASE+'/screening');second();
      };
      if(variant==='captcha')app.insertAdjacentHTML('afterbegin',`<div id='challenge' class='g-recaptcha'>Complete the CAPTCHA</div>`);
      if(variant==='network_captcha'){
        app.insertAdjacentHTML('afterbegin',`<div id='challenge' class='g-recaptcha'><iframe title='Synthetic verification' src='/captcha/frame'></iframe></div>`);
        window.addEventListener('message',event=>{if(event.origin===location.origin&&event.data==='verified')document.getElementById('challenge')?.remove()});
      }
      if(variant==='login')app.insertAdjacentHTML('afterbegin',`<div id='login'><label for='password'>Password</label><input id='password' type='password' value='private-fixture-password'></div>`);
      if(variant==='unknown')app.insertAdjacentHTML('beforeend',`<label for='unknown'>Which editor do you prefer?</label><input id='unknown' required>`);
      if(variant==='shadow'){
        const label=document.querySelector('label[for=first]'),input=document.getElementById('first');
        const wrapper=document.createElement('spl-input'); wrapper.setAttribute('label','First name'); wrapper.attachShadow({mode:'open'});wrapper.shadowRoot.append(label,input);app.prepend(wrapper);
      }
      if(variant==='city'){
        const wrapper=document.createElement('spl-autocomplete');wrapper.setAttribute('label','City');wrapper.attachShadow({mode:'open'});
        wrapper.shadowRoot.innerHTML=`<input id='city' role='combobox' aria-label='City' aria-controls='cities' aria-autocomplete='list' aria-required='true'><div id='cities' role='listbox' hidden></div>`;
        const input=wrapper.shadowRoot.getElementById('city'),menu=wrapper.shadowRoot.getElementById('cities');
        const open=()=>{menu.hidden=false;menu.innerHTML=`<div role='option'>Boston</div><div role='option' aria-disabled='true'>Remote</div>`;menu.firstChild.onclick=async()=>{locationValue=await(await fetch('/oneclick-ui/api/location/details/boston')).json();wrapper.setAttribute('data-selected-value','Boston');input.value='';menu.hidden=true}};
        input.onclick=open;input.oninput=open;input.onkeydown=event=>{if(event.key==='Escape')menu.hidden=true};app.prepend(wrapper);
      }
    }
    function second(){
      app.innerHTML=`<h1>Screening questions</h1><label for='shift'>Preferred shift?</label><select id='shift' required><option value=''>Choose</option><option value='day'>Day</option><option value='night'>Night</option></select>
      <label for='SMART_RECRUIT'>I agree to the candidate Privacy Notice</label><input id='SMART_RECRUIT' name='SMART_RECRUIT' type='checkbox' required>
      <a href='/privacy'>Privacy notice</a><button id='submit'>Submit application</button>`;
      if(variant==='legal_screening')app.insertAdjacentHTML('beforeend',`<label for='legalAck'>Do you acknowledge the candidate agreement?</label><input id='legalAck' name='legal-screening.confirm' type='checkbox' required>`);
      document.getElementById('submit').onclick=async()=>{
        if(!document.getElementById('shift').checkValidity()||!document.getElementById('SMART_RECRUIT').checked)return;
        const body={profile:{...saved,experience:[],education:[],web:{},resume},metadata:{internalApplication:false,dataFeed:'FEED_DEFAULT'},answers:[{id:'shift-question',records:[{fields:{value:[document.getElementById('shift').value]}}]}],consent:true,consentDecision:{changingConsentAvailable:true,confirmedByScope:{SMART_RECRUIT:true}},seekToken:'',creator:null,messageToEmployer:''};
        if(variant==='legal_screening')body.answers.push({id:'legal-screening',records:[{fields:{confirm:['confirmed']}}]});
        if(variant==='injected_profile')body.profile.email='different@example.test';
        const endpoint=variant==='wrong_posting'?SUBMIT.replace(PUBLICATION,'7105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3'):SUBMIT;
        try{const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if(variant==='duplicate')fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).catch(()=>{});
        await response.text();history.pushState({},'',BASE+'/success');app.innerHTML='<h1>Thank you for applying</h1><p>Your application was submitted.</p>';}catch{app.innerHTML+='<p>Request blocked</p>'}
      };
    }
    first();</script></body></html>""".replace("BOOTSTRAP_JSON", json.dumps(context)).replace("VARIANT", json.dumps(variant))
        .replace("UPLOAD", json.dumps(UPLOAD)).replace("BASE", json.dumps(BASE)).replace("SUBMIT", json.dumps(SUBMIT)).replace("PUBLICATION", json.dumps(PUBLICATION)))


@pytest.fixture
def employer():
    receipts, uploads, reads, verifications = [], [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            reads.append(self.path)
            if self.path == '/captcha/frame':
                raw=b'''<!doctype html><form method="post" action="/captcha/check"><button id="verify" name="response" value="human">Complete verification</button></form><script>document.querySelector('form').onsubmit=async event=>{event.preventDefault();const response=await fetch(event.target.action,{method:'POST',body:new URLSearchParams({response:'human'})});if(response.ok)parent.postMessage('verified',location.origin)};</script>'''
                self.send_response(200)
                self.send_header('Content-Type','text/html')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == '/oneclick-ui/api/location/details/boston':
                raw=json.dumps({'city':'Boston','country':'United States'}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            variant = parse_qs(urlsplit(self.path).query).get("variant", ["success"])[0]
            if self.path.startswith("/Example/"):
                body = (f"<script>window.ONECLICKDATA = {{cident: 'Example', pid: 744000156836234, puuid: '{PUBLICATION}'}};</script>"
                        f"<button id='st-apply' onclick=\"location.href='{BASE}?variant={variant}'\">I'm interested</button>")
            else:
                body = spa(variant)
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html;charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == '/captcha/check':
                verifications.append(raw)
                value,status={'verified':True},200
            elif urlsplit(self.path).path == UPLOAD:
                parsed = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
                uploads.append([(part.get_param("name", header="content-disposition"),part.get_filename(),part.get_payload(decode=True)) for part in parsed.iter_parts()])
                value = {"attachmentType": "RESUME", "fileId": "synthetic-resume-id", "fileName": "synthetic-resume.txt"}
                status = 200
            else:
                body = json.loads(raw)
                receipts.append(body)
                value = {"applicationId": "synthetic-application-id"}
                status = 500 if body["profile"].get("firstName") == "ServerError" else 200
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", receipts, uploads, reads, verifications
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class Control:
    def __init__(self, page, provider, profile):
        self.page, self.provider, self.profile = page, provider, profile
        self.job, self.resume_path = provider.job, provider.resume_path
        self.started, self.phase, self.human_acknowledged = False, "opening", False
        self.reserved, self.final_request_count, self.reservations = False, 0, 0
        self.blocked_requests, self.events = [], []
        self.permitted = True
    async def verify(self):
        assert self.permitted
        assert self.provider.document_allowed(self.page.url)
        if self.resume_path:
            assert hashlib.sha256(self.resume_path.read_bytes()).hexdigest() == self.provider.resume_sha
    async def reserve(self):
        await self.verify()
        self.reservations += 1
        if self.reserved:
            return False
        self.reserved = True
        return True
    async def emit(self, stage, **details):
        self.events.append(stage)
    def pause(self, kind, message, *, pending_questions=None):
        self.phase = "human"
        return {"status": "paused", "kind": kind, "message": message, "pending_questions": pending_questions or []}
    def result(self, status, message, **extra):
        return {"status": status, "message": message, **extra}


async def setup_fixture(employer, tmp_path, variant="success", profile=None, posting=False):
    origin = employer[0]
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_bytes(b"Synthetic resume bytes. Python backend engineering.\r\n")
    url = origin + ("/Example/744000156836234" if posting else BASE) + "?variant=" + variant
    provider = SmartRecruitersApplication({"apply_url": url, "demo": True}, resume, fixture=True)
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(service_workers="block")
    page = await context.new_page()
    control = Control(page, provider, profile or dict(PROFILE))
    async def guard(route):
        request = route.request
        if provider.ignore_request(request):
            await route.abort()
            return
        if not await provider.request_allowed(request, control.phase):
            control.blocked_requests.append({"method": request.method, "phase": control.phase})
            await route.abort()
            return
        if provider.is_final_request(request):
            assert control.reserved and control.final_request_count == 0
            control.final_request_count += 1
        options = await provider.prepare_request(request, control.phase)
        await route.continue_(**(options or {}))
    await context.route("**/*", guard)
    page.on("response", provider.observe_response)
    return playwright, browser, page, provider, control


async def complete_gate(page, control):
    await page.locator("#SMART_RECRUIT").check()
    if await page.locator('#legalAck').count():
        await page.locator('#legalAck').check()
    control.human_acknowledged = True


def test_chromium_spa_uploads_exact_file_pauses_for_agreement_and_submits_once(employer, tmp_path):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path)
        try:
            result = await provider.drive(control)
            assert result["status"] == "paused" and result["kind"] == "agreement", result
            assert not employer[1] and len(employer[2]) == 1 and control.reservations == 0
            await complete_gate(page, control)
            result = await provider.drive(control)
            assert result["status"] == "submitted", result
            assert control.reservations == control.final_request_count == len(employer[1]) == 1
            assert employer[1][0]["profile"]["email"] == PROFILE["email"]
            assert employer[1][0]["answers"][0]["records"] == [{"fields": {"value": ["day"]}}]
            assert employer[2] == [[("file", "synthetic-resume.txt", provider.resume_path.read_bytes())]]
            result = await provider.drive(control)
            assert result["status"] == "uncertain" and len(employer[1]) == 1
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


@pytest.mark.parametrize("variant,kind", [("captcha", "verification"), ("login", "login"), ("unknown", "question"), ("mismatched_bootstrap", "transport")])
def test_unknown_or_gated_pages_send_no_applicant_mutations(employer, tmp_path, variant, kind):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path, variant)
        try:
            result = await provider.drive(control)
            assert result["status"] == "paused" and result["kind"] == kind, result
            assert not employer[1] and not employer[2] and not control.reservations
            if variant == "unknown":
                assert any(q["question"] == "Which editor do you prefer?" for q in result["pending_questions"])
            if variant == "login":
                observed=await page.evaluate(SNAPSHOT,'credential-regression')
                password=next(field for field in observed['fields'] if field['type']=='password')
                assert password['value']=='' and 'private-fixture-password' not in json.dumps(observed['fields'])
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_same_browser_can_resume_after_manual_captcha_without_reopening(employer, tmp_path):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path, "captcha")
        try:
            first = await provider.drive(control)
            assert first["kind"] == "verification"
            await page.locator("#challenge").evaluate("el=>el.remove()")
            control.human_acknowledged = True
            second = await provider.drive(control)
            assert second["kind"] == "agreement"
            assert len(employer[3]) == 1 and len(employer[2]) == 1 and not employer[1]
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_shadow_combobox_selects_an_actual_owned_choice_and_keeps_location(employer,tmp_path):
    async def run():
        profile={**PROFILE,'application_profile':{'facts':{'current_city':'Boston'}}}
        playwright,browser,page,provider,control=await setup_fixture(employer,tmp_path,'city',profile)
        try:
            result=await provider.drive(control)
            assert result['status']=='paused' and result['kind']=='agreement',result
            await complete_gate(page,control)
            result=await provider.drive(control)
            assert result['status']=='submitted',result
            assert employer[1][0]['profile']['location']=={'city':'Boston','country':'United States'}
        finally:
            await browser.close();await playwright.stop()
    asyncio.run(run())


def test_manual_verification_can_send_its_exact_advertised_network_form_in_same_browser(employer,tmp_path):
    async def run():
        playwright,browser,page,provider,control=await setup_fixture(employer,tmp_path,'network_captcha')
        try:
            result=await provider.drive(control)
            assert result['status']=='paused' and result['kind']=='verification',result
            assert not employer[1] and not employer[2] and not employer[4]
            frame=page.frame_locator('iframe[title="Synthetic verification"]')
            await frame.locator('#verify').wait_for()
            assert provider._challenge_frames
            # This click represents the human action in the retained browser.
            # The provider only permits the frame's exact advertised form POST.
            await frame.locator('#verify').click()
            await page.locator('#challenge').wait_for(state='detached')
            assert employer[4]==[b'response=human'] and not control.blocked_requests
            control.human_acknowledged=True
            result=await provider.drive(control)
            assert result['kind']=='agreement',result
            await complete_gate(page,control)
            result=await provider.drive(control)
            assert result['status']=='submitted',result
            assert len(employer[1])==control.reservations==control.final_request_count==1
        finally:
            await browser.close();await playwright.stop()
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["shadow", "success", "legal_screening"])
def test_posting_binds_publication_and_shadow_controls(employer, tmp_path, variant):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path, variant, posting=True)
        try:
            result = await provider.drive(control)
            assert result["status"] == "paused" and result["kind"] == "agreement", result
            assert provider.publication == PUBLICATION and len(employer[3]) == 2
            await complete_gate(page, control)
            result = await provider.drive(control)
            assert result["status"] == "submitted", result
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_new_agreement_text_requires_new_acknowledgment(employer, tmp_path):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path)
        try:
            assert (await provider.drive(control))["kind"] == "agreement"
            await complete_gate(page, control)
            await page.locator("label[for=SMART_RECRUIT]").evaluate("el=>el.textContent='I agree to a different candidate Arbitration Agreement'")
            result = await provider.drive(control)
            assert result["kind"] == "agreement" and not employer[1] and not control.reservations
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_500_with_success_words_is_uncertain_and_never_retried(employer, tmp_path):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path, profile={**PROFILE, "first_name": "ServerError"})
        try:
            assert (await provider.drive(control))["kind"] == "agreement"
            await complete_gate(page, control)
            result = await provider.drive(control)
            assert result["status"] == "uncertain", result
            assert len(employer[1]) == 1 and control.final_request_count == 1
            assert (await provider.drive(control))["status"] == "uncertain"
            assert len(employer[1]) == 1
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_stale_approval_during_resume_does_not_submit(employer, tmp_path):
    async def run():
        playwright, browser, page, provider, control = await setup_fixture(employer, tmp_path)
        try:
            assert (await provider.drive(control))["kind"] == "agreement"
            await complete_gate(page, control)
            control.permitted = False
            result = await provider.drive(control)
            assert result["status"] == "paused" and not employer[1] and control.reservations == 0
        finally:
            await browser.close()
            await playwright.stop()
    asyncio.run(run())


def test_public_read_endpoint_and_bootstrap_are_strict():
    assert public_smartrecruiters_url("https://jobs.smartrecruiters.com/Example/744000156836234-role") == "https://api.smartrecruiters.com/v1/companies/Example/postings/744000156836234"
    assert public_smartrecruiters_url("https://jobs.smartrecruiters.com" + BASE) is None
    assert public_smartrecruiters_url("https://jobs.smartrecruiters.com.evil.test/Example/744000156836234") is None
    assert _bootstrap("<script>window.__OC_CONTEXT__=JSON.parse('bad')</script>")[0] is None
    assert _bootstrap('<script>window.__OC_CONTEXT__={"company":{}}</script><script>window.__OC_CONTEXT__={}</script>')[0] is None


def test_explicit_records_do_not_gain_invented_days_or_unlisted_employers(tmp_path):
    provider = SmartRecruitersApplication({"apply_url": "https://jobs.smartrecruiters.com" + BASE}, None)
    provider._control = SimpleNamespace(profile={"application_profile": {"employment": [{"company": "Example", "title": "Engineer", "start_month": "2025-03", "current": True}], "education": []}})
    source = [{"company": "Example", "title": "Engineer", "current": True, "startDate": {"year": 2025, "month": 3, "day": None}, "endDate": None}]
    assert provider._records_match(source, "employment")
    assert not provider._records_match([{**source[0], "company": "Unlisted"}], "employment")
    assert not provider._records_match([{**source[0], "startDate": {"year": 2025, "month": 3, "day": 1}}], "employment")


def test_real_session_registry_retains_paused_browser_and_reserves_one_final_request(employer,tmp_path):
    from jobradar.application_sessions import ApplicationSessions
    async def run():
        registry=ApplicationSessions(headless=True)
        resume=tmp_path/'synthetic-resume.txt'
        resume.write_bytes(b'Synthetic registry integration resume.')
        job={'id':'smart-fixture-role','ats':'smartrecruiters','demo':True,'apply_url':employer[0]+BASE+'?variant=network_captcha'}
        reservations=[]
        async def verify():return True
        async def reserve():
            assert not employer[1]
            reservations.append('reserved')
            return True
        try:
            result=await registry.start(job,dict(PROFILE),resume,verify=verify,reserve=reserve,fixture=True,binding='test')
            assert result['status']=='paused',result
            session=registry.sessions[job['id']]
            receipt=result['verification']
            assert receipt['kind']=='verification' and registry.available(job['id'],receipt['session_id'])
            assert not employer[1] and not employer[2] and not reservations
            await session.page.frame_locator('iframe[title="Synthetic verification"]').locator('#verify').click()
            await session.page.locator('#challenge').wait_for(state='detached')
            assert employer[4]==[b'response=human'] and not session.blocked_requests
            result=await registry.resume(job['id'],receipt['session_id'],pause_id=receipt['pause_id'],acknowledge=True,profile=dict(PROFILE),binding='test',verify=verify,reserve=reserve)
            assert result['status']=='paused' and result['verification']['kind']=='agreement',result
            assert result['verification']['pause_id']!=receipt['pause_id']
            receipt=result['verification']
            await session.page.locator('#SMART_RECRUIT').check()
            result=await registry.resume(job['id'],receipt['session_id'],pause_id=receipt['pause_id'],acknowledge=True,profile=dict(PROFILE),binding='test',verify=verify,reserve=reserve)
            assert result['status']=='submitted',result
            assert reservations==['reserved'] and session.final_request_count==1 and len(employer[1])==1
            assert job['id'] not in registry.sessions and session.closed
        finally:
            for job_id in list(registry.sessions):await registry.cancel(job_id)
    asyncio.run(run())


def ready_provider():
    provider=SmartRecruitersApplication({'apply_url':'https://jobs.smartrecruiters.com'+BASE},None)
    provider.transport_ready=True
    provider._control=SimpleNamespace(profile=dict(PROFILE),page=SimpleNamespace(url='https://jobs.smartrecruiters.com'+BASE))
    provider._screening=[]
    provider._snapshots=[{'fields':[{'label':'I agree to the Privacy Notice','name':'SMART_RECRUIT','type':'checkbox','checked':True,'required':True}]}]
    provider._approved_gates={_gate_stamp(provider._snapshots[0])}
    return provider


def final_request(provider,body):
    return SimpleNamespace(method='POST',url=provider._submit_url(),post_data_buffer=json.dumps(body).encode(),headers={'content-type':'application/json'},is_navigation_request=lambda:False)


def valid_body():
    return {'profile':{'firstName':PROFILE['first_name'],'lastName':PROFILE['last_name'],'email':PROFILE['email'],'phoneNumber':PROFILE['phone'],'resume':None,'education':[],'experience':[],'web':{'facebook':'','twitter':'','linkedIn':'','website':''}},'metadata':{'internalApplication':False,'dataFeed':'FEED_DEFAULT'},'answers':[],'consent':True,'consentDecision':{'changingConsentAvailable':True,'confirmedByScope':{'SMART_RECRUIT':True}}}


@pytest.mark.parametrize('change',[
    lambda b:b['profile'].update(email='different@example.test'),
    lambda b:b['profile'].update(avatar={'fileId':'unselected'}),
    lambda b:b['profile'].update(location={'city':'Unconfirmed'}),
    lambda b:b['profile']['web'].update(linkedIn='https://unconfirmed.example.test'),
    lambda b:b['profile'].update(experience=[{'company':'Unlisted','title':'Engineer'}]),
    lambda b:b['metadata'].update(dataFeed='FEED_CV_PARSING'),
    lambda b:b['metadata'].update(sid='unobserved-private-value'),
    lambda b:b.update(serverTrackingParams={'unexpected':'hidden data'}),
    lambda b:b.update(creator={'employeeId':'unapproved'}),
    lambda b:b.update(secret='unapproved'),
    lambda b:b.update(answers=[{'id':'unknown-question','records':[]}]),
    lambda b:b['consentDecision']['confirmedByScope'].update(SMART_RECRUIT=False),
    lambda b:b['consentDecision'].update(confirmedByScope={'UNADVERTISED_SCOPE':True}),
    lambda b:b['consentDecision'].update(confirmedByScope={'UNADVERTISED_SCOPE':False}),
])
def test_final_payload_cannot_send_unapproved_facts_imports_or_extra_data(change):
    provider=ready_provider()
    body=valid_body()
    assert provider._valid_application_body(final_request(provider,body))
    change(body)
    assert not provider._valid_application_body(final_request(provider,body))


def test_final_post_is_never_allowed_in_human_phase_or_twice():
    async def run():
        provider=ready_provider()
        provider.final_armed=True
        request=final_request(provider,valid_body())
        assert not await provider.request_allowed(request,'human')
        assert await provider.request_allowed(request,'submitting')
        assert not await provider.request_allowed(request,'submitting')
        wrong=final_request(provider,valid_body())
        wrong.url=wrong.url.replace(PUBLICATION,'7105f8d6-5d54-4b0a-8c72-b0ea7f2abcc3')
        assert not await provider.request_allowed(wrong,'submitting')
    asyncio.run(run())


def test_challenge_handoff_requires_observed_frame_exact_action_human_phase_and_advertised_fields():
    async def run():
        provider=ready_provider()
        frame=SimpleNamespace(url='https://geo.captcha-delivery.com/captcha/?cid=synthetic')
        provider._control.page.main_frame=object()
        endpoint='https://geo.captcha-delivery.com/captcha/check'
        key=(frame.url,'POST',endpoint)
        provider._challenge_frames={frame.url}
        provider._challenge_actions={key}
        provider._challenge_fields={key:{'response'}}
        async def capture():pass
        provider._capture_challenges=capture
        request=SimpleNamespace(method='POST',url=endpoint,frame=frame,headers={'content-type':'application/x-www-form-urlencoded'},post_data_buffer=b'response=synthetic-human',is_navigation_request=lambda:False)
        assert await provider.request_allowed(request,'human')
        for phase in ['opening','filling','next','uploading','submitting']:
            assert not await provider.request_allowed(request,phase)
        request.post_data_buffer=b'email=unapproved%40example.test'
        assert not await provider.request_allowed(request,'human')
        request.post_data_buffer=b'response=synthetic-human'
        request.url=endpoint+'/unadvertised'
        assert not await provider.request_allowed(request,'human')
        request.url=endpoint
        provider._challenge_frames.clear()
        assert not await provider.request_allowed(request,'human')
    asyncio.run(run())


def test_single_checkbox_fallback_requires_an_explicit_public_scope_and_current_gate_ack():
    provider=ready_provider()
    provider.context={'consent':{'changingConsentAvailable':True,'consentScopeConfigs':[{'scope':'SINGLE'}]}}
    provider._snapshots[0]['fields'][0]['name']=''
    provider._approved_gates={_gate_stamp(provider._snapshots[0])}
    assert provider._consent_matches({'changingConsentAvailable':True,'confirmedByScope':{'SINGLE':True}})
    assert not provider._consent_matches({'changingConsentAvailable':True,'confirmedByScope':{'UNADVERTISED_SCOPE':True}})
    assert not provider._consent_matches({'changingConsentAvailable':'true','confirmedByScope':{'SINGLE':True}})
    provider._approved_gates.clear()
    assert not provider._consent_matches({'changingConsentAvailable':True,'confirmedByScope':{'SINGLE':True}})


def test_manual_legal_screening_checkbox_binds_exact_public_question_field_and_value():
    provider=ready_provider()
    label='Do you acknowledge the candidate agreement?'
    provider._screening=[{'id':'legal-screening','label':label,'repeatable':False,'fields':[{'id':'confirm','type':'CHECKBOX','values':[{'id':'confirmed','label':'I acknowledge'}]}]}]
    provider._snapshots=[{'fields':[{'label':label,'name':'legal-screening.confirm','type':'checkbox','checked':True,'required':True}]}]
    provider._approved_gates={_gate_stamp(provider._snapshots[0])}
    answer=[{'id':'legal-screening','records':[{'fields':{'confirm':['confirmed']}}]}]
    assert provider._screening_matches(answer)
    assert not provider._screening_matches([{'id':'unknown-scope','records':[{'fields':{'confirm':['confirmed']}}]}])
    assert not provider._screening_matches([{'id':'legal-screening','records':[{'fields':{'confirm':['unadvertised-value']}}]}])
    provider._approved_gates.clear()
    assert not provider._screening_matches(answer)
