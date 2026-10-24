"""Workday candidate browser checks on synthetic Chromium pages only.

Every request is intercepted before network delivery. Public URL/API shapes
reflect the inspected Workday asset; success here is not a live tenant claim.
"""
import asyncio
import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import pytest
from playwright.async_api import async_playwright

from jobradar.workday_adapter import parse_workday_destination, public_workday_detail_url, extract_workday_posting
from jobradar.workday_application import WorkdayApplication
from jobradar.application_sessions import ApplicationSessions


URL = "https://example.wd5.myworkdayjobs.com/en-US/External/job/Boston/Software-Engineer_JR1234567"
POSTING = "a" * 32
APP = "b" * 32
PREFIX = f"/wday/cxs/example/jobapplication/{APP}"
PROFILE = {"name":"Alex Example","first_name":"Alex","last_name":"Example","email":"alex@example.test",
    "work_authorized":True,"needs_sponsorship":False,
    "application_profile":{"education":[
        {"id":"e1","institution":"Example University","degree":"Bachelor's","discipline":"Computer Science","status":"completed","start_month":"2020-09","end_month":"2024-07","primary_for_applications":True},
        {"id":"e2","institution":"Other University","degree":"Master's","discipline":"Computer Science","status":"incoming","start_month":"2027-01","end_month":"2028-05"}],
    "employment":[{"id":"w1","company":"Example Employer","title":"Software Engineer","kind":"employee","start_month":"2025-02","current":True,"primary_for_applications":True}]}}


def metadata(**changes):
    return {"jobPostingInfo":{"id":POSTING,"jobReqId":"JR1234567","jobPostingSiteId":"External",
        "jobPostingId":"Software-Engineer_JR1234567","externalUrl":URL,"title":"Software Engineer",
        "jobDescription":"Build backend services.","location":"Boston, MA, United States","canApply":True,"posted":True,**changes}}


def request(path, method="POST", body="{}", content_type="application/json", navigation=False):
    return SimpleNamespace(url="https://example.wd5.myworkdayjobs.com"+path,method=method,post_data=body,
        post_data_buffer=body.encode() if isinstance(body,str) else body,headers={"content-type":content_type},is_navigation_request=lambda:navigation)


def test_public_identity_binds_tenant_site_requisition_and_observed_encoded_location():
    identity = parse_workday_destination(URL)
    assert identity["application_key"] == "workday:example.wd5.myworkdayjobs.com/external/jr1234567"
    assert identity["apply_url"] == URL+"/apply/applyManually"
    assert public_workday_detail_url(URL) == "https://example.wd5.myworkdayjobs.com/wday/cxs/example/External/job/Boston/Software-Engineer_JR1234567"
    assert parse_workday_destination(URL.replace("Boston","US%2C-CA%2C-Santa-Clara"))["application_key"] == identity["application_key"]
    assert extract_workday_posting(URL,metadata())["id"] == POSTING


@pytest.mark.parametrize("url",[URL.replace("example.wd5","example.wd5.evil"),URL.replace("https:","http:"),URL+"#other",URL.replace("Boston","%2e%2e"),URL.replace("Boston","A%2FB"),URL.replace("Boston","%252e%252e"),URL.replace("Boston","A%5CB"),URL.replace("External/job","Other/External/job"),URL+"/apply/other"])
def test_wrong_or_deceptive_workday_destinations_are_rejected(url):
    assert parse_workday_destination(url) is None


@pytest.mark.parametrize("changes",[{"jobReqId":"JR7654321"},{"jobPostingSiteId":"Other"},{"externalUrl":URL.replace("example.wd5","other.wd5")},{"jobPostingId":"Other_JR1234567"},{"id":"not-an-id"}])
def test_public_metadata_must_match_exact_posting(changes):
    assert extract_workday_posting(URL,metadata(**changes)) is None


def test_transport_is_response_bound_resource_limited_and_final_is_single_use(tmp_path):
    adapter=WorkdayApplication({"apply_url":URL},None)
    adapter.posting=extract_workday_posting(URL,metadata())
    async def scenario():
        create=request(f"/wday/cxs/example/jobpostings/{POSTING}/jobapplications")
        assert await adapter.request_allowed(create,"opening")
        assert not await adapter.request_allowed(create,"opening")
        adapter.application_id=APP
        assert await adapter.request_allowed(request(PREFIX+"/name",body='{"firstName":"Alex"}'),"next")
        assert not await adapter.request_allowed(request(PREFIX+"/deleteCandidate"),"filling")
        assert not await adapter.request_allowed(request(PREFIX.replace(APP,"c"*32)+"/name"),"next")
        assert not await adapter.request_allowed(request(PREFIX+"/finalize"),"filling")
        adapter._final_allowed=True
        assert await adapter.request_allowed(request(PREFIX+"/finalize"),"submitting")
        assert not await adapter.request_allowed(request(PREFIX+"/finalize"),"submitting")
    asyncio.run(scenario())


@pytest.mark.parametrize('endpoint',['login','register','resendactivationinfo','initiatereset'])
def test_observed_workday_account_posts_only_in_explicit_human_account_pause(endpoint):
    adapter=WorkdayApplication({'apply_url':URL},None)
    adapter._paused_kind='login'
    path='/en-US/External/'+endpoint+'?redirect='+quote(urlsplit(adapter.url).path,safe='')
    account=request(path,body='username=synthetic&password=synthetic',content_type='application/x-www-form-urlencoded;charset=UTF-8')
    async def check():
        assert await adapter.request_allowed(account,'human')
        assert not await adapter.request_allowed(account,'filling')
        assert not await adapter.request_allowed(account,'opening')
        adapter._paused_kind='question'
        assert not await adapter.request_allowed(account,'human')
        adapter._paused_kind='login';adapter._terminal={'status':'submitted'}
        assert not await adapter.request_allowed(account,'human')
    asyncio.run(check())


@pytest.mark.parametrize('path',[
    '/en-US/External/verifyAccount','/en-US/External/resendVerificationEmail',
    '/en-US/External/forgotPassword','/en-US/External/resetPassword',
    '/en-US/Other/login','/wday/authgwy/example/login',
    '/en-US/External/login?redirect=https%3A%2F%2Fevil.test%2F',
    '/en-US/External/login?redirect=%2F%2Fevil.test%2F',
    '/en-US/External/login?redirect='+quote(urlsplit(URL.replace('JR1234567','JR7654321')).path,safe=''),
    '/en-US/External/login?redirect=&redirect=',
    '/en-US/External/login?unrecognized',
])
def test_unobserved_or_unbound_account_transport_is_rejected(path):
    adapter=WorkdayApplication({'apply_url':URL},None);adapter._paused_kind='verification'
    assert not asyncio.run(adapter.request_allowed(request(path,content_type='application/x-www-form-urlencoded'),'human'))


def test_account_front_routes_use_only_observed_bound_locale_and_validate_redirect():
    adapter=WorkdayApplication({'apply_url':URL.replace('/en-US/','/')},None)
    account=request('/en-US/External/login',content_type='application/x-www-form-urlencoded')
    adapter._paused_kind='login'
    assert not asyncio.run(adapter.request_allowed(account,'human'))
    assert adapter._document_allowed(URL+'/apply/applyManually')
    assert asyncio.run(adapter.request_allowed(account,'human'))
    assert not asyncio.run(adapter.request_allowed(request('/en-US/External/login'),'human'))
    assert adapter._document_allowed('https://example.wd5.myworkdayjobs.com/en-US/External/login',human=True)
    assert not adapter._document_allowed('https://example.wd5.myworkdayjobs.com/en-US/External/login?redirect=https%3A%2F%2Fevil.test',human=True)


def test_human_account_failure_can_be_retried_without_reading_or_storing_response():
    adapter=WorkdayApplication({'apply_url':URL},None)
    async def must_not_read():
        raise AssertionError('Account response must stay owned by the visible account UI.')
    response=SimpleNamespace(request=request('/en-US/External/login'),url='https://example.wd5.myworkdayjobs.com/en-US/External/login',status=401,body=must_not_read)
    asyncio.run(adapter.observe_response(response))
    assert not adapter._transport_error and adapter.application_id is None


HTML=r'''<!doctype html><html><body><div id="app"></div><script>
let variant=VARIANT,step=0;const root=document.querySelector('#app');
const prefix=PREFIX, appId=APPID;
function field(label,name,type='text',required=true){return `<label for="${name}">${label}</label><input id="${name}" name="${name}" type="${type}" ${required?'required':''}>`}
function work(){return `<fieldset><legend>Work Experience 1</legend>${field('Company name','company')}${field('Job title','title')}${field('Start date','start','month')}<label><input id="current" name="current" type="checkbox">I currently work here</label>${field('End date','end','month',false)}</fieldset>`}
function education(i){return `<fieldset><legend>Education ${i+1}</legend>${field('School',`school${i}`)}${field('Degree',`degree${i}`)}${field('Start date',`schoolstart${i}`,'month')}${field('End date',`schoolend${i}`,'month')}</fieldset>`}
function render(){
 if(variant==='login'&&step===0){root.innerHTML='<h1>Sign In</h1><form>'+field('Email','loginemail','email')+field('Password','password','password')+'<button>Sign In</button></form>';root.querySelector('form').onsubmit=async e=>{e.preventDefault();await login();step=10;render()};return}
 if(step===10){root.innerHTML='<h1>Verify your email</h1><p id="verification">Complete email verification outside this form, then sign in again.</p><button id="verified">Verified email; sign in</button>';root.querySelector('#verified').onclick=async()=>{await login();await draft();step=1;render()};return}
 if(step===0||step===1){root.innerHTML='<h1>My Information</h1><form>'+field('First Name','first')+field('Last Name','last')+field('Email address','email','email')+(variant==='unknown'?field('A required unfamiliar answer','custom'):'')+'<button type="submit">Next</button></form>';root.querySelector('form').onsubmit=async e=>{e.preventDefault();await fetch(prefix+'/name',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});step=2;render()};return}
 if(step===2){root.innerHTML='<h1>My Experience</h1><form><section data-automation-id="workExperience">'+work()+'</section><section data-automation-id="education">'+education(0)+'<button type="button" id="add">Add Another</button></section>'+field('Resume/CV','resume','file')+'<button type="submit">Next</button></form>';document.querySelector('#add').onclick=()=>document.querySelector('#add').insertAdjacentHTML('beforebegin',education(document.querySelectorAll('[data-automation-id=education] fieldset').length));document.querySelector('#resume').onchange=async e=>{const data=new FormData();data.append('body',e.target.files[0]);const endpoint=variant==='common_upload'?'/wday/calypso/cxs/common/example/attachments':prefix+'/resume';const response=await fetch(endpoint,{method:'POST',body:data});if(variant==='common_upload'){const uploaded=await response.json();await fetch(prefix+'/resumeattachments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({attachments:[{file:uploaded.id,fileName:e.target.files[0].name}]})})}};root.querySelector('form').onsubmit=async e=>{e.preventDefault();for(const [path,selector] of [['workexperiences','[data-automation-id=workExperience]'],['educations','[data-automation-id=education]']]){for(const record of document.querySelector(selector).querySelectorAll('fieldset')){const data=Object.fromEntries([...record.querySelectorAll('input')].map(i=>[i.name,i.type==='checkbox'?i.checked:i.value]));await fetch(prefix+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}}step=3;render()};return}
 root.innerHTML='<h1>Review</h1><form><button type="submit">Submit application</button></form>';root.querySelector('form').onsubmit=async e=>{e.preventDefault();await fetch(prefix+'/finalize',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(variant==='double_submit')try{await fetch(prefix+'/finalize',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}catch(error){}root.innerHTML='<h1>Your application has been submitted</h1>'};
}
async function draft(){await fetch('/wday/cxs/example/jobpostings/'+POSTING+'/jobapplications',{method:'POST',body:'{}'})}
async function login(){await fetch('/en-US/External/login',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'username=synthetic&password=synthetic'})}
async function init(){await fetch(METADATA);if(variant!=='login')await draft();render()};init();
</script></body></html>'''


class Control:
    def __init__(self,adapter,page,resume):
        self.adapter,self.page,self.resume_path=adapter,page,resume
        self.profile=json.loads(json.dumps(PROFILE));self.job=adapter.job
        self.started=False;self.phase='opening';self.human_acknowledged=False
        self.reserved=False;self.final_request_count=0;self.blocked_requests=[];self.verifications=0
    async def verify(self):self.verifications+=1
    async def reserve(self):
        assert not self.reserved
        self.reserved=True
    async def emit(self,*args,**kwargs):pass
    def result(self,status,message,**kwargs):return {'status':status,'message':message,**kwargs}
    def pause(self,kind,message,**kwargs):
        self.phase='human';self.human_acknowledged=False
        return self.result('paused',message,kind=kind,**kwargs)


@asynccontextmanager
async def browser_scenario(tmp_path,variant='success',wrong_metadata=False):
    resume=tmp_path/'synthetic-resume.txt';resume.write_bytes(b'Synthetic resume only.\r\nBinary-safe proof\x00\xff')
    adapter=WorkdayApplication({'id':'synthetic','apply_url':URL,'ats':'workday'},resume)
    receipts=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        context=await browser.new_context(service_workers='block')
        page=await context.new_page();control=Control(adapter,page,resume)
        page.on('response',adapter.observe_response)
        async def route(request_route):
            req=request_route.request
            if not await adapter.request_allowed(req,control.phase):
                control.blocked_requests.append({'method':req.method,'phase':control.phase})
                await request_route.abort();return
            if req.method=='GET':
                if '/wday/cxs/' in req.url:
                    body=metadata(jobReqId='JR7654321') if wrong_metadata else metadata()
                    await request_route.fulfill(json=body);return
                html=HTML.replace('VARIANT',json.dumps(variant)).replace('PREFIX',json.dumps(PREFIX)).replace('APPID',json.dumps(APP)).replace('POSTING',json.dumps('Software-Engineer_JR1234567' if variant=='slug_draft' else POSTING)).replace('METADATA',json.dumps(public_workday_detail_url(URL)))
                await request_route.fulfill(content_type='text/html',body=html);return
            prepared=await adapter.prepare_request(req,control.phase)
            body=prepared['post_data'] if prepared else req.post_data_buffer
            receipts.append((req.url.split('.com',1)[1],body))
            if adapter.is_final_request(req):
                assert control.reserved and control.final_request_count==0
                control.final_request_count+=1
            if req.url.endswith('/jobapplications'):await request_route.fulfill(json={'id':APP,'sessionId':'synthetic-session'})
            elif req.url.endswith('/finalize'):await request_route.fulfill(status=500 if variant=='server_error' else 200,json={'id':APP})
            elif req.url.endswith('/attachments'):await request_route.fulfill(json={'id':'c'*32})
            else:await request_route.fulfill(json={})
        await context.route('**/*',route)
        try:
            yield adapter,control,receipts,resume
        finally:
            await context.close();await browser.close()


@pytest.mark.parametrize('variant',['success','slug_draft'])
def test_real_browser_multistep_typed_records_exact_upload_and_single_receipt(tmp_path,variant):
    async def check():
        async with browser_scenario(tmp_path,variant) as (adapter,control,receipts,resume):
            result=await adapter.drive(control)
            assert result['status']=='submitted',(result,await control.page.locator('body').inner_text(),receipts,control.blocked_requests)
            assert control.final_request_count==1 and control.reserved
            name=json.loads(next(body for path,body in receipts if path.endswith('/name')))
            assert name=={'first':'Alex','last':'Example','email':'alex@example.test'}
            education=[json.loads(body) for path,body in receipts if path.endswith('/educations')]
            assert [item[f'school{i}'] for i,item in enumerate(education)]==['Example University','Other University']
            work=json.loads(next(body for path,body in receipts if path.endswith('/workexperiences')))
            assert work['company']=='Example Employer' and work['current'] is True and work['start']=='2025-02'
            uploaded=next(body for path,body in receipts if path.endswith('/resume'))
            assert resume.read_bytes() in uploaded
            before=len(receipts)
            assert (await adapter.drive(control))['status']=='submitted'
            assert len(receipts)==before
    asyncio.run(check())


def test_real_browser_login_and_verification_pause_resume_same_form_without_storing_credentials(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'login') as (adapter,control,receipts,_):
            first=await adapter.drive(control)
            assert first['status']=='paused' and first['kind']=='login'
            assert not control.reserved
            # Explicit human actions on synthetic controls, not provider autofill.
            await control.page.locator('#loginemail').fill('synthetic@example.test')
            await control.page.locator('#password').fill('synthetic-password')
            await control.page.get_by_role('button',name='Sign In',exact=True).click()
            await control.page.locator('#verification').wait_for(state='visible')
            control.human_acknowledged=True
            second=await adapter.drive(control)
            assert second['kind']=='verification',second
            # Model out-of-band email verification and reauthentication through
            # the observed login route, not an invented OTP submission API.
            await control.page.get_by_role('button',name='Verified email; sign in',exact=True).click()
            await control.page.locator('#first').wait_for(state='visible')
            control.human_acknowledged=True
            assert (await adapter.drive(control))['status']=='submitted'
            assert 'password' not in json.dumps(control.profile)
            assert len([path for path,_ in receipts if path.endswith('/finalize')])==1
    asyncio.run(check())


def test_real_browser_human_password_stays_in_dom_but_snapshot_and_pause_redact_it(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'login') as (adapter,control,receipts,_):
            first=await adapter.drive(control)
            assert first['status']=='paused' and first['kind']=='login'
            secret='Synthetic-only-private-password-42!'
            await control.page.locator('#loginemail').fill('synthetic@example.test')
            await control.page.locator('#password').fill(secret)
            state=await adapter._snapshot(control.page,control.profile)
            password_fields=[item for item in state['fields'] if item.get('type')=='password']
            assert password_fields and all(item.get('value')=='' for item in password_fields)
            assert secret not in json.dumps(state)
            assert await control.page.locator('#password').input_value()==secret
            control.human_acknowledged=True
            paused=await adapter.drive(control)
            assert paused['status']=='paused' and paused['kind']=='login'
            assert secret not in json.dumps(paused) and not paused.get('pending_questions')
            assert await control.page.locator('#password').input_value()==secret
            assert not receipts and not control.reserved
    asyncio.run(check())


def test_real_browser_unknown_question_needs_explicit_current_step_review(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'unknown') as (adapter,control,receipts,_):
            result=await adapter.drive(control)
            assert result['status']=='paused' and result['kind']=='question',result
            assert not any(path.endswith('/name') for path,_ in receipts)
            await control.page.locator('#custom').fill('Explicit human answer')
            control.human_acknowledged=True
            assert (await adapter.drive(control))['status']=='submitted'
    asyncio.run(check())


def test_real_browser_wrong_requisition_never_fills_or_reserves(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,wrong_metadata=True) as (adapter,control,receipts,_):
            result=await adapter.drive(control)
            assert result['status']=='paused' and result['kind']=='transport',result
            assert not control.reserved
            assert await control.page.locator('#first').count()==0 or await control.page.locator('#first').input_value()==''
            assert not any(path.endswith('/name') for path,_ in receipts)
    asyncio.run(check())


def test_real_browser_failed_final_response_cannot_be_retried_or_counted_from_words(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'server_error') as (adapter,control,receipts,_):
            result=await adapter.drive(control)
            assert result['status']=='uncertain',result
            assert control.final_request_count==1
            before=len(receipts)
            assert (await adapter.drive(control))['status']=='uncertain'
            assert len(receipts)==before
    asyncio.run(check())


def test_real_browser_duplicate_final_request_is_blocked_and_outcome_stays_uncertain(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'double_submit') as (adapter,control,receipts,_):
            result=await adapter.drive(control)
            assert result['status']=='uncertain',result
            assert control.final_request_count==1 and len(control.blocked_requests)==1
            assert len([path for path,_ in receipts if path.endswith('/finalize')])==1
    asyncio.run(check())


def test_real_browser_observed_common_upload_links_only_its_returned_file_id(tmp_path):
    async def check():
        async with browser_scenario(tmp_path,'common_upload') as (adapter,control,receipts,resume):
            done=await adapter.drive(control)
            assert done['status']=='submitted',done
            uploaded=next(body for path,body in receipts if path=='/wday/calypso/cxs/common/example/attachments')
            assert resume.read_bytes() in uploaded
            link=json.loads(next(body for path,body in receipts if path.endswith('/resumeattachments')))
            assert link=={'attachments':[{'file':'c'*32,'fileName':resume.name}]}
            assert not await adapter.request_allowed(request(PREFIX+'/resumeattachments',body='{"attachments":[{"file":"unobserved"}]}'),'next')
    asyncio.run(check())


def test_real_browser_changed_approval_stops_before_filling_or_final_attempt(tmp_path):
    async def check():
        async with browser_scenario(tmp_path) as (adapter,control,receipts,_):
            async def stale():
                control.verifications+=1
                if control.verifications>1:raise ValueError('Configuration changed')
            control.verify=stale
            with pytest.raises(ValueError,match='Configuration changed'):
                await adapter.drive(control)
            assert not control.reserved and not any(path.endswith('/name') for path,_ in receipts)
            assert await control.page.locator('#first').input_value()==''
    asyncio.run(check())


def test_resume_transport_never_accepts_wrong_file_bytes_or_extra_multipart_fields(tmp_path):
    resume=tmp_path/'resume.txt';resume.write_bytes(b'Explicit resume bytes')
    adapter=WorkdayApplication({'apply_url':URL},resume)
    adapter.posting=extract_workday_posting(URL,metadata());adapter.application_id=APP
    def multipart(payload,extra=b''):
        return b'--boundary\r\nContent-Disposition: form-data; name="body"; filename="resume.txt"\r\nContent-Type: text/plain\r\n\r\n'+payload+b'\r\n'+extra+b'--boundary--\r\n'
    def upload(payload,extra=b''):
        return request(PREFIX+'/resume',body=multipart(payload,extra),content_type='multipart/form-data; boundary=boundary')
    async def check():
        assert await adapter.request_allowed(upload(resume.read_bytes()),'uploading')
        assert not await adapter.request_allowed(upload(b'Wrong file'),'uploading')
        extra=b'--boundary\r\nContent-Disposition: form-data; name="secret"\r\n\r\nUnapproved\r\n'
        assert not await adapter.request_allowed(upload(resume.read_bytes(),extra),'uploading')
        assert not await adapter.request_allowed(upload(resume.read_bytes()),'filling')
        restored=await adapter.prepare_request(upload(b''),'uploading')
        assert restored['post_data']==multipart(resume.read_bytes())
    asyncio.run(check())


@pytest.fixture
def local_workday():
    """Actual loopback HTTP sink for the real ApplicationSessions guard."""
    receipts=[]
    configuration={}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,value,content_type='application/json'):
            body=value.encode() if isinstance(value,str) else json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if urlsplit(self.path).path.startswith('/wday/cxs/'):
                self.respond(metadata(externalUrl=configuration['url']))
            else:
                html=HTML.replace('VARIANT',json.dumps('login')).replace('PREFIX',json.dumps(PREFIX)).replace('APPID',json.dumps(APP)).replace('POSTING',json.dumps(POSTING)).replace('METADATA',json.dumps(public_workday_detail_url(configuration['url'],fixture=True)))
                self.respond(html,'text/html')
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length','0')))
            # Synthetic account inputs are not retained even by the fixture sink.
            receipts.append((urlsplit(self.path).path,b'' if '/External/' in self.path else body))
            self.respond({'id':APP,'sessionId':'synthetic-session'} if self.path.endswith('/jobapplications') else {'id':APP} if self.path.endswith('/finalize') else {})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    configuration['url']=f'http://127.0.0.1:{server.server_port}/en-US/External/job/Boston/Software-Engineer_JR1234567'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield configuration['url'],receipts
    finally:server.shutdown();server.server_close();thread.join(timeout=3)


def test_real_application_sessions_workday_login_email_verification_resume_same_browser_and_exact_final_once(local_workday,tmp_path):
    url,receipts=local_workday
    resume=tmp_path/'synthetic-resume.txt';resume.write_bytes(b'Exact local Workday resume.\x00\xff\r\n')
    job={'id':'local-workday','ats':'workday','apply_url':url,'demo':True}
    reservations=[]
    async def yes():return True
    async def reserve():
        assert not any(path.endswith('/finalize') for path,_ in receipts)
        reservations.append('reserved');return True
    async def check():
        sessions=ApplicationSessions(headless=True)
        try:
            first=await sessions.start(job,PROFILE,resume,verify=yes,reserve=reserve,fixture=True,binding='test')
            assert first['status']=='paused' and first['verification']['kind']=='login',first
            control=sessions.sessions[job['id']];page=control.page
            await page.locator('#loginemail').fill('synthetic@example.test')
            await page.locator('#password').fill('synthetic-password')
            await page.get_by_role('button',name='Sign In',exact=True).click()
            await page.locator('#verification').wait_for(state='visible')
            second=await sessions.resume(job['id'],first['verification']['session_id'],acknowledge=True,profile=PROFILE,binding='test',verify=yes,reserve=reserve)
            assert second['status']=='paused' and second['verification']['kind']=='verification',second
            assert sessions.sessions[job['id']].page is page and not reservations
            await page.get_by_role('button',name='Verified email; sign in',exact=True).click()
            await page.locator('#first').wait_for(state='visible')
            done=await sessions.resume(job['id'],second['verification']['session_id'],acknowledge=True,profile=PROFILE,binding='test',verify=yes,reserve=reserve)
            assert done['status']=='submitted',done
            assert reservations==['reserved'] and control.final_request_count==1
            assert not control.blocked_requests and control.closed and not sessions.sessions
            assert len([path for path,_ in receipts if path.endswith('/finalize')])==1
            assert resume.read_bytes() in next(body for path,body in receipts if path.endswith('/resume'))
            names=json.loads(next(body for path,body in receipts if path.endswith('/name')))
            assert names=={'first':'Alex','last':'Example','email':'alex@example.test'}
            with pytest.raises(ValueError):
                await sessions.resume(job['id'],second['verification']['session_id'],acknowledge=True,profile=PROFILE,binding='test',verify=yes,reserve=reserve)
            assert len(reservations)==1
        finally:await sessions.close()
    asyncio.run(check())
