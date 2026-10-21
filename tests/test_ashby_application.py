"""Complete hosted-renderer flow, with all Chromium traffic fulfilled locally.

Synthetic forms exercise the actual field-save/upload/submit browser protocol;
no real Ashby mutation, candidate database, credentials or résumé is used.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from email import policy
from email.parser import BytesParser
import hashlib
import json
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from jobradar.ashby_application import AshbyApplication, _graphql_shape, _OPERATIONS


URL = "https://jobs.ashbyhq.com/ExampleCo/12345678-1234-1234-1234-123456789abc/application"
POSTING = "12345678-1234-1234-1234-123456789abc"
UPLOAD = "https://ashby-synthetic.s3.amazonaws.com/"
PROFILE = {"name": "Alex Example", "email": "alex@example.test", "work_authorized": True,
           "needs_sponsorship": False, "answers": {"Country": "United States"}}


def query(operation, variables):
    kind, root = _OPERATIONS[operation]
    alias = "fileUploadHandle: " if operation == "ApiCreateFileUploadHandle" else "submitApplicationFormAction: " if operation == "ApiSubmitSingleApplicationFormAction" else ""
    return f"{kind} {operation}(" + ",".join(f"${key}: JSON" for key in variables) + ") { " + alias + root + "(" + ",".join(f"{key}:${key}" for key in variables) + ") { __typename } }"


def form(variant="success"):
    definitions = [("_systemfield_name", "Name", "String"), ("_systemfield_email", "Email", "Email"),
                   ("auth", "Are you legally authorized to work in the United States?", "Boolean"),
                   ("sponsor", "Will you now or in the future require visa sponsorship?", "Boolean"),
                   ("country", "Country", "ValueSelect"), ("_systemfield_resume", "Resume", "File")]
    if variant == "unknown":
        definitions.append(("editor", "Favorite code editor", "String"))
    if variant == "legal":
        definitions.append(("agreement", "I agree to the candidate terms", "Boolean"))
    if variant == "location":
        definitions.append(("location", "Location (City)", "Location"))
    if variant == "school":
        definitions.append(("education", "Education", "EducationHistory"))
    entries = [{"id": "render-1_" + path, "field": {"id": path, "path": path, "title": title, "type": kind,
                "selectableValues": [{"label": "Canada", "value": "Canada"}, {"label": "United States", "value": "United States"}] if kind == "ValueSelect" else []},
                "isRequired": True, "fieldValue": None} for path, title, kind in definitions]
    if variant == "location":
        next(entry for entry in entries if entry["field"]["path"] == "location")["field"]["locationTypes"] = ["City"]
    if variant in {"radio", "autocomplete"}:
        country = next(entry for entry in entries if entry["field"]["path"] == "country")
        country["field"]["selectableValues"] = [{"label": "Canada", "value": "ca"}, {"label": "United States", "value": "us"}]
        if variant == "autocomplete":
            country["field"]["selectableValues"] += [{"label": f"Other country {index}", "value": f"other-{index}"} for index in range(9)]
    return {"id": "render-1", "sourceFormDefinitionId": "definition-1", "formControls": [{"identifier": "submit-1", "title": "Submit application"}],
            "sections": [{"title": "Application", "fieldEntries": entries}], "formErrors": [], "errorMessages": []}


def html(variant):
    identity = {"organization": {"hostedJobsPageSlug": "ExampleCo"}, "posting": {"id": POSTING, "isListed": True, "title": "Engineer"}}
    operations = {operation: _OPERATIONS[operation][1] for operation in _OPERATIONS}
    return """<!doctype html><html><head><meta charset="utf-8"></head><body>
    <script>window.__appData=IDENTITY;</script><main id="mount"></main>
    <script>
    const variant=VARIANT, operations=OPERATIONS;
    async function operation(name,variables){
      const kind=['ApiJobPosting','ApiAutocompleteGeoLocation','ApiSearchSchoolByCanonicalName'].includes(name)?'query':'mutation';
      const alias=name==='ApiCreateFileUploadHandle'?'fileUploadHandle: ':name==='ApiSubmitSingleApplicationFormAction'?'submitApplicationFormAction: ':'';
      const query=kind+' '+name+'('+Object.keys(variables).map(key=>'$'+key+': JSON').join(',')+') { '+alias+operations[name]+'('+Object.keys(variables).map(key=>key+':$'+key).join(',')+') { __typename } }';
      return await (await fetch('/api/non-user-graphql?op='+name,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operationName:name,variables,query})})).json();
    }
    const base={organizationHostedJobsPageName:'ExampleCo',formRenderIdentifier:'render-1',formDefinitionIdentifier:'definition-1'};
    async function set(path,value){await operation('ApiSetFormValue',{...base,path,value})}
    operation('ApiJobPosting',{organizationHostedJobsPageName:'ExampleCo',jobPostingId:'POSTING'}).then(data=>{
      const form=document.createElement('form');form.id='application';
      for(const entry of data.data.jobPosting.applicationForm.sections[0].fieldEntries){
        const field=entry.field, box=document.createElement('div');box.className='ashby-application-form-field-entry';
        const inner=document.createElement('div');inner.dataset.fieldPath=field.path;inner.dataset.fieldEntryId=entry.id;box.appendChild(inner);
        const title=document.createElement('div');title.className='ashby-application-form-question-title';
        const label=document.createElement('label');label.htmlFor=field.path;label.textContent=field.title+'*';title.appendChild(label);inner.appendChild(title);
        if(field.type==='Boolean' && field.path!=='agreement'){
          const group=document.createElement('div');
          for(const option of ['yes','no']){const button=document.createElement('button');button.type='button';button.dataset.option=option;
            button.setAttribute('aria-pressed','false');button.textContent=option==='yes'?'Yes':'No';
            button.onclick=()=>{group.querySelectorAll('button').forEach(node=>node.setAttribute('aria-pressed',String(node===button)));group.querySelector('input').checked=option==='yes';set(field.path,option==='yes')};group.appendChild(button)}
          const proxy=document.createElement('input');proxy.type='checkbox';proxy.name=field.path;proxy.tabIndex=-1;proxy.style.display='none';group.appendChild(proxy);inner.appendChild(group);form.appendChild(box);continue;
        }
        if(field.type==='ValueSelect' && variant==='radio'){
          box.className='';const group=document.createElement('fieldset');inner.appendChild(group);
          for(const option of field.selectableValues){const label=document.createElement('label'),radio=document.createElement('input');radio.type='radio';radio.name=field.path;radio.value=option.value;radio.required=true;
            label.appendChild(radio);label.appendChild(document.createTextNode(option.label));radio.onchange=()=>set(field.path,option.value);group.appendChild(label)}form.appendChild(box);continue;
        }
        if(field.type==='ValueSelect' && variant==='autocomplete'){
          box.className='';const input=document.createElement('input');input.setAttribute('role','combobox');input.setAttribute('aria-autocomplete','list');input.setAttribute('aria-controls','country-options');input.setAttribute('aria-expanded','false');inner.appendChild(input);
          input.onclick=()=>{document.getElementById('country-options')?.remove();const menu=document.createElement('div');menu.id='country-options';menu.setAttribute('role','listbox');input.setAttribute('aria-expanded','true');
            for(const option of field.selectableValues){const item=document.createElement('div');item.setAttribute('role','option');item.textContent=option.label;item.onclick=()=>{input.value=option.label;input.setAttribute('aria-expanded','false');menu.remove();set(field.path,option.value)};menu.appendChild(item)}document.body.appendChild(menu)};
          input.onkeydown=event=>{if(event.key==='Escape'){document.getElementById('country-options')?.remove();input.setAttribute('aria-expanded','false')}};form.appendChild(box);continue;
        }
        if(field.type==='Location' || field.type==='EducationHistory'){
          const input=document.createElement('input');input.id=field.path;input.setAttribute('role','combobox');input.setAttribute('aria-autocomplete','list');input.setAttribute('aria-controls',field.path+'-options');input.setAttribute('aria-expanded','false');input.required=true;inner.appendChild(input);
          let suggestions=[];
          function show(){document.getElementById(field.path+'-options')?.remove();const menu=document.createElement('div');menu.id=field.path+'-options';menu.setAttribute('role','listbox');input.setAttribute('aria-expanded','true');
            if(!suggestions.length)menu.textContent='Start typing to search';
            for(const option of suggestions){const item=document.createElement('div');item.setAttribute('role','option');item.textContent=option.name;item.onclick=()=>{input.value=option.name;input.setAttribute('aria-expanded','false');menu.remove();
             set(field.path,field.type==='Location'?{text:option.name,providerLocationId:option.geoLocationPath.at(-1).providerLocationId}:[{schoolName:option.name,canonicalSchoolId:option.id}]);};menu.appendChild(item)}document.body.appendChild(menu)}
          input.onclick=show;input.oninput=async()=>{const isGeo=field.type==='Location';const response=await operation(isGeo?'ApiAutocompleteGeoLocation':'ApiSearchSchoolByCanonicalName',isGeo?{text:input.value,locationTypes:field.locationTypes}:{name:input.value,organizationHostedJobsPageName:'ExampleCo'});
            suggestions=isGeo?response.data.result.suggestions:response.data.result;show()};
          input.onkeydown=event=>{if(event.key==='Escape'){document.getElementById(field.path+'-options')?.remove();input.setAttribute('aria-expanded','false')}};
          form.appendChild(box);continue;
        }
        let input=document.createElement(field.type==='ValueSelect'?'select':'input');input.id=field.path;input.name=field.path;
        if(field.type==='ValueSelect'){input.innerHTML='<option value="">Choose one</option>';for(const item of field.selectableValues){const option=document.createElement('option');option.value=item.value;option.textContent=item.label;input.appendChild(option)}}
        else input.type=field.type==='File'?'file':field.type==='Email'?'email':field.path==='agreement'?'checkbox':'text';
        input.required=true;inner.appendChild(input);
        if(field.type==='File')input.onchange=async()=>{
          const file=input.files[0]; const result=await operation('ApiCreateFileUploadHandle',{organizationHostedJobsPageName:'ExampleCo',fileUploadContext:'NonUserFormEngine',filename:file.name,contentType:file.type,contentLength:file.size});
          const upload=result.data.fileUploadHandle;const body=new FormData();body.append('Content-Type',file.type);Object.entries(upload.fields).forEach(([key,value])=>body.append(key,value));body.append('file',file);
          await fetch(upload.url,{method:'POST',body});await operation('ApiSetFormValueToFile',{...base,path:field.path,fileHandle:upload.handle});
        };else input.onchange=()=>set(field.path,input.type==='checkbox'?input.checked:input.value);
        form.appendChild(box);
      }
      const submit=document.createElement('button');submit.type='submit';submit.textContent='Submit application';form.appendChild(submit);
      form.onsubmit=async event=>{event.preventDefault();await new Promise(resolve=>setTimeout(resolve,30));
       const result=await operation('ApiSubmitSingleApplicationFormAction',{organizationHostedJobsPageName:'ExampleCo',jobPostingId:'POSTING',formRenderIdentifier:'render-1',formDefinitionIdentifier:'definition-1',actionIdentifier:'submit-1',recaptchaToken:'synthetic-token',sourceAttributionCode:null,viewedAutomatedProcessingLegalNoticeRuleId:null,deviceFingerprint:null,applicationRequestId:null});
       document.body.dataset.result='received';document.body.insertAdjacentHTML('beforeend','<h1>Thank you for applying</h1>');
      };
      document.getElementById('mount').appendChild(form);
      if(variant==='verification'||variant==='verification_legal')document.body.insertAdjacentHTML('beforeend','<div class="g-recaptcha" data-sitekey="synthetic">Verification</div>');
      if(variant==='verification_legal')document.body.insertAdjacentHTML('beforeend','<p>By submitting, I agree to the employer terms.</p>');
    });
    </script></body></html>""".replace("IDENTITY", json.dumps(identity)).replace("VARIANT", json.dumps(variant)).replace("OPERATIONS", json.dumps(operations)).replace("POSTING", POSTING)


class Control:
    def __init__(self, page, provider, profile=None):
        self.page, self.job, self.profile, self.resume_path = page, provider.job, deepcopy(profile or PROFILE), provider.resume_path
        self.started, self.reserved, self.human_acknowledged = False, False, False
        self.phase, self.blocked_requests = "opening", []
        self.reservations, self.verifications, self.events = 0, 0, []
        self.allow_verify = True

    async def verify(self):
        self.verifications += 1
        return self.allow_verify

    async def reserve(self):
        if self.reserved:
            return False
        self.reserved = True
        self.reservations += 1
        return True

    async def emit(self, stage, **details):
        self.events.append(stage)

    def pause(self, kind, message, *, pending_questions=None):
        self.phase, self.human_acknowledged = "human", False
        return {"status": "paused", "pause_kind": kind, "message": message, "pending_questions": pending_questions or []}

    def result(self, status, message, **extra):
        return {"status": status, "message": message, **extra}


async def scenario(tmp_path, variant="success", action=None, profile=None):
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(b"%PDF-1.4\nSynthetic local test resume only.\n%%EOF")
    provider = AshbyApplication({"apply_url": URL, "company": "ExampleCo", "title": "Engineer", "country_code": "US"}, resume, fixture=True)
    state = {"form": form(variant), "writes": [], "uploads": [], "submissions": [], "allowed": [], "blocked": [], "lookups": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False)
            page = await context.new_page()
            control = Control(page, provider, profile)
            async def route_handler(route):
                request = route.request
                allowed = await provider.request_allowed(request, control.phase)
                if not allowed:
                    diagnostic = {"method": request.method, "host": "synthetic", "phase": control.phase}
                    state["blocked"].append(request.url)
                    control.blocked_requests.append(diagnostic)
                    return await route.abort()
                state["allowed"].append(request.url)
                if request.is_navigation_request():
                    return await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html(variant))
                if request.url == UPLOAD:
                    prepared = await provider.prepare_request(request, control.phase)
                    raw = prepared["post_data"] if prepared else request.post_data_buffer
                    message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + request.headers["content-type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
                    upload = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
                    state["uploads"].append(upload)
                    return await route.fulfill(status=204)
                body = json.loads(request.post_data_buffer)
                operation, variables = body["operationName"], body["variables"]
                if operation == "ApiJobPosting":
                    posting = {"id": "wrong-posting" if variant == "wrong_metadata" else POSTING, "applicationForm": state["form"], "surveyForms": []}
                    response = {"data": {"jobPosting": posting}}
                elif operation == "ApiAutocompleteGeoLocation":
                    state["lookups"].append(deepcopy(variables))
                    response = {"data": {"result": {"suggestions": [{"name": "Boston, Massachusetts, United States", "geoLocationPath": [{"providerLocationId": "observed-city-id"}]}] if variables["text"] else []}}}
                elif operation == "ApiSearchSchoolByCanonicalName":
                    state["lookups"].append(deepcopy(variables))
                    response = {"data": {"result": [{"name": "Synthetic University", "id": "observed-school-id"}]}}
                elif operation == "ApiCreateFileUploadHandle":
                    response = {"data": {"fileUploadHandle": {"url": UPLOAD, "fields": {"key": "synthetic/key", "policy": "synthetic-policy", "signature": "synthetic-signature"}, "handle": "synthetic-file-handle"}}}
                elif operation in {"ApiSetFormValue", "ApiSetFormValueToFile"}:
                    state["writes"].append(deepcopy(variables))
                    entry = next(entry for entry in state["form"]["sections"][0]["fieldEntries"] if entry["field"]["path"] == variables["path"])
                    entry["fieldValue"] = {"filename": resume.name} if operation.endswith("ToFile") else {"value": variables["value"], "__typename": "JSONBox"}
                    response = {"data": {_OPERATIONS[operation][1]: deepcopy(state["form"])}}
                elif operation.startswith("ApiSubmit"):
                    assert control.reserved and control.reservations == 1
                    state["submissions"].append(deepcopy(variables))
                    response = {"data": {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormRender" if variant == "server_error" else "FormSubmitSuccess"}}}}
                else:
                    raise AssertionError("Unexpected request escaped provider guard")
                await route.fulfill(status=500 if variant == "server_error" and operation.startswith("ApiSubmit") else 200,
                                    content_type="application/json", body=json.dumps(response))
            await context.route("**/*", route_handler)
            page.on("response", provider.observe_response)
            result = await provider.drive(control)
            if action:
                result = await action(result, control, provider, state)
            return result, control, provider, state, resume.read_bytes()
        finally:
            await browser.close()


def test_full_hosted_flow_saves_exact_answers_uploads_exact_bytes_and_submits_once(tmp_path):
    result, control, provider, state, resume = asyncio.run(scenario(tmp_path))
    assert result["status"] == "submitted", (result, state)
    assert control.reservations == 1 and provider.final_sent
    values = {write["path"]: write.get("value") for write in state["writes"]}
    assert values["_systemfield_name"] == "Alex Example" and values["_systemfield_email"] == "alex@example.test"
    assert values["auth"] is True and values["sponsor"] is False and values["country"] == "United States"
    assert len(state["uploads"]) == len(state["submissions"]) == 1
    assert state["uploads"][0]["file"] == resume
    assert state["uploads"][0]["key"] == b"synthetic/key"
    assert not state["blocked"] and control.verifications > len(state["writes"])


def test_verification_pauses_before_any_applicant_write_and_resumes_same_page(tmp_path):
    async def resume(result, control, provider, state):
        assert result["status"] == "paused" and result["pause_kind"] == "verification", result
        assert not state["writes"] and not state["uploads"] and not control.reserved
        original_page = control.page
        await control.page.locator(".g-recaptcha").evaluate("node=>node.remove()")
        control.human_acknowledged = True
        result = await provider.drive(control)
        assert control.page is original_page and control.started
        return result
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "verification", resume))
    assert result["status"] == "submitted", (result, state)
    assert control.reservations == 1 and len(state["submissions"]) == 1


@pytest.mark.parametrize("variant", ["radio", "autocomplete"])
def test_actual_ashby_choice_shapes_commit_visible_labels_with_internal_option_values(tmp_path, variant):
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, variant))
    assert result["status"] == "submitted", (result, state)
    assert next(write for write in state["writes"] if write["path"] == "country")["value"] == "us"
    assert control.reservations == 1 and not state["blocked"]


def test_changed_approval_on_resume_blocks_all_profile_writes(tmp_path):
    async def resume(result, control, provider, state):
        assert result["pause_kind"] == "verification"
        await control.page.locator(".g-recaptcha").evaluate("node=>node.remove()")
        control.human_acknowledged, control.allow_verify = True, False
        return await provider.drive(control)
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "verification", resume))
    assert result["status"] == "paused" and result["pause_kind"] == "settings_changed"
    assert not state["writes"] and not state["uploads"] and not control.reserved


def test_geo_lookup_uses_confirmed_query_and_observed_provider_id_then_completes(tmp_path):
    profile = {**PROFILE, "location": "Boston, Massachusetts, United States"}
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "location", profile=profile))
    assert result["status"] == "submitted", (result, state)
    location = next(write["value"] for write in state["writes"] if write["path"] == "location")
    assert location == {"text": profile["location"], "providerLocationId": "observed-city-id"}
    assert all(item["text"] in {"", profile["location"]} and item["locationTypes"] == ["City"] for item in state["lookups"])
    assert control.reservations == 1 and not state["blocked"]


def test_user_can_confirm_canonical_location_for_this_form_without_rewriting_profile(tmp_path):
    profile = {**PROFILE, "location": "Boston, MA"}
    async def resume(result, control, provider, state):
        assert result["status"] == "paused" and not control.reserved, result
        await control.page.locator("#location").fill("Boston, Massachusetts, United States")
        await control.page.get_by_role("option", name="Boston, Massachusetts, United States", exact=True).click()
        await control.page.wait_for_timeout(50)
        control.human_acknowledged = True
        result = await provider.drive(control)
        assert control.profile["location"] == "Boston, MA"
        return result
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "location", resume, profile))
    assert result["status"] == "submitted", (result, state)
    assert control.reservations == 1


def test_school_lookup_and_required_education_record_can_be_completed_in_retained_browser(tmp_path):
    async def resume(result, control, provider, state):
        assert result["status"] == "paused" and not state["writes"], result
        before = deepcopy(control.profile)
        await control.page.locator("#education").fill("Synthetic University")
        await control.page.get_by_role("option", name="Synthetic University", exact=True).click()
        await control.page.wait_for_timeout(50)
        control.human_acknowledged = True
        result = await provider.drive(control)
        assert control.profile == before
        return result
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "school", resume))
    assert result["status"] == "submitted", (result, state)
    assert any(write.get("value") == [{"schoolName": "Synthetic University", "canonicalSchoolId": "observed-school-id"}] for write in state["writes"])
    assert state["lookups"] == [{"name": "Synthetic University", "organizationHostedJobsPageName": "ExampleCo"}]
    assert control.reservations == 1 and not state["blocked"]


def test_verification_ack_does_not_accept_a_separate_employer_agreement(tmp_path):
    async def resume(result, control, provider, state):
        assert result["pause_kind"] == "verification" and not state["writes"]
        await control.page.locator(".g-recaptcha").evaluate("node=>node.remove()")
        control.human_acknowledged = True
        second = await provider.drive(control)
        assert second["pause_kind"] == "agreement" and not state["writes"], second
        control.human_acknowledged = True
        return await provider.drive(control)
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "verification_legal", resume))
    assert result["status"] == "submitted", (result, state)
    assert control.reservations == 1


def test_unknown_question_can_be_completed_by_user_without_learning_fabricated_fact(tmp_path):
    async def resume(result, control, provider, state):
        assert result["status"] == "paused" and result["pause_kind"] == "questions", result
        assert not state["writes"] and not control.reserved
        before = deepcopy(control.profile)
        await control.page.locator("#editor").fill("Synthetic Editor")
        await control.page.locator("#editor").blur()
        await control.page.wait_for_timeout(50)
        control.human_acknowledged = True
        result = await provider.drive(control)
        assert control.profile == before
        return result
    result, _, _, state, _ = asyncio.run(scenario(tmp_path, "unknown", resume))
    assert result["status"] == "submitted", (result, state)
    assert any(write.get("value") == "Synthetic Editor" for write in state["writes"])


def test_legal_control_requires_manual_action_before_autofill(tmp_path):
    async def resume(result, control, provider, state):
        assert result["status"] == "paused" and result["pause_kind"] == "agreement", result
        assert not state["writes"] and not control.reserved
        await control.page.locator("#agreement").check()
        await control.page.wait_for_timeout(50)
        control.human_acknowledged = True
        return await provider.drive(control)
    result, _, _, state, _ = asyncio.run(scenario(tmp_path, "legal", resume))
    assert result["status"] == "submitted", (result, state)


def test_mismatched_metadata_never_sends_profile_or_reserves_attempt(tmp_path):
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "wrong_metadata"))
    assert result["status"] == "paused" and result["pause_kind"] == "destination"
    assert not state["writes"] and not state["uploads"] and not state["submissions"] and not control.reserved


def test_failed_final_response_remains_uncertain_and_cannot_retry(tmp_path):
    async def again(result, control, provider, state):
        assert result["status"] == "uncertain", result
        count = len(state["submissions"])
        result = await provider.drive(control)
        assert len(state["submissions"]) == count == 1
        return result
    result, control, _, state, _ = asyncio.run(scenario(tmp_path, "server_error", again))
    assert result["status"] == "uncertain" and control.reservations == 1


@pytest.mark.parametrize("url", [URL.replace("ExampleCo", "%45xampleCo"), URL.replace("https", "http"), URL + "/other", URL + "#fragment", "https://evil.example.test/apply"])
def test_invalid_destination_is_rejected_before_browser(url):
    with pytest.raises(ValueError):
        AshbyApplication({"apply_url": url}, None)


def fake_request(operation, variables, *, query_text=None, url=None):
    return SimpleNamespace(url=url or "https://jobs.ashbyhq.com/api/non-user-graphql?op=" + operation,
                           method="POST", headers={"content-type": "application/json"},
                           post_data_buffer=json.dumps({"operationName": operation, "variables": variables,
                                                        "query": query_text or query(operation, variables)}).encode(), is_navigation_request=lambda: False)


def test_mutation_capability_rejects_wrong_board_render_definition_path_or_value(tmp_path):
    provider = AshbyApplication({"apply_url": URL}, None)
    provider.document_verified = True
    assert provider._accept_form(form())
    provider.control = SimpleNamespace(verify=lambda: asyncio.sleep(0, result=True), reserved=False)
    expected = {"organizationHostedJobsPageName": "ExampleCo", "formRenderIdentifier": "render-1", "formDefinitionIdentifier": "definition-1", "path": "_systemfield_name", "value": "Alex Example"}
    provider.active = {**expected, "sent": False}
    for key, value in (("organizationHostedJobsPageName", "OtherCo"), ("formRenderIdentifier", "stale-render"),
                       ("formDefinitionIdentifier", "wrong-definition"), ("path", "_systemfield_email"), ("value", "DIFFERENT")):
        assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", {**expected, key: value}), "filling"))
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "opening"))
    assert asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "filling"))
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "filling")), "One field capability cannot authorize duplicate writes"


@pytest.mark.parametrize("value", [0, 1, "false", "true", None])
def test_boolean_field_capability_does_not_accept_numeric_or_string_substitutes(value):
    provider = AshbyApplication({"apply_url": URL}, None)
    provider.document_verified = True
    provider._accept_form(form())
    provider.control = SimpleNamespace(verify=lambda: asyncio.sleep(0, result=True), reserved=False)
    expected = {"organizationHostedJobsPageName": "ExampleCo", "formRenderIdentifier": "render-1", "formDefinitionIdentifier": "definition-1", "path": "sponsor", "value": False}
    provider.active = {**expected, "sent": False}
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", {**expected, "value": value}), "filling"))


def test_metadata_definition_cannot_name_a_different_posting():
    provider = AshbyApplication({"apply_url": URL}, None)
    wrong = form()
    wrong["sourceFormDefinitionId"] = json.dumps({"kind": "CompositeFormDefinition", "jobPostingId": "other-posting"})
    assert not provider._accept_form(wrong)


def test_graphql_envelope_rejects_second_root_inline_argument_and_mismatched_variable():
    variables = {"organizationHostedJobsPageName": "ExampleCo", "jobPostingId": POSTING}
    valid = query("ApiJobPosting", variables)
    assert _graphql_shape(valid, "ApiJobPosting", variables)
    assert not _graphql_shape(valid[:-1] + " danger { id } }", "ApiJobPosting", variables)
    assert not _graphql_shape(valid.replace("jobPostingId:$jobPostingId", 'jobPostingId:"other"'), "ApiJobPosting", variables)
    assert not _graphql_shape(valid.replace("jobPostingId:$jobPostingId", "jobPostingId:$organizationHostedJobsPageName"), "ApiJobPosting", variables)


def test_lookup_queries_are_bound_to_declared_fields_confirmed_text_and_employer():
    provider = AshbyApplication({"apply_url": URL}, None)
    provider.document_verified = True
    provider._accept_form(form("location"))
    provider.lookup_queries = {"Boston, Massachusetts, United States"}
    assert provider._lookup_allowed("ApiAutocompleteGeoLocation", {"text": "Boston, Massachusetts, United States", "locationTypes": ["City"]}, "filling")
    assert not provider._lookup_allowed("ApiAutocompleteGeoLocation", {"text": "private@example.test", "locationTypes": ["City"]}, "filling")
    assert not provider._lookup_allowed("ApiAutocompleteGeoLocation", {"text": "Boston, Massachusetts, United States", "locationTypes": ["Country"]}, "filling")
    assert not provider._lookup_allowed("ApiSearchSchoolByCanonicalName", {"name": "Synthetic University", "organizationHostedJobsPageName": "ExampleCo"}, "human")
    provider._accept_form(form("school"))
    assert provider._lookup_allowed("ApiSearchSchoolByCanonicalName", {"name": "Synthetic University", "organizationHostedJobsPageName": "ExampleCo"}, "human")
    assert not provider._lookup_allowed("ApiSearchSchoolByCanonicalName", {"name": "Synthetic University", "organizationHostedJobsPageName": "OtherCo"}, "human")


def test_manual_lookup_selections_cannot_send_unobserved_geo_or_canonical_school_ids():
    provider = AshbyApplication({"apply_url": URL}, None)
    provider.document_verified = True
    provider._accept_form(form("location"))
    provider.control = SimpleNamespace(verify=lambda: asyncio.sleep(0, result=True), reserved=False)
    expected = {"organizationHostedJobsPageName": "ExampleCo", "formRenderIdentifier": "render-1", "formDefinitionIdentifier": "definition-1", "path": "location",
                "value": {"text": "Boston, Massachusetts, United States", "providerLocationId": "observed-id"}}
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "human"))
    provider.location_options[json.dumps(["City"])] = {"Boston, Massachusetts, United States": expected["value"]}
    assert asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "human"))
    provider._accept_form(form("school"))
    expected.update(path="education", value=[{"schoolName": "Synthetic University", "canonicalSchoolId": "unobserved-id"}])
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "human"))
    provider.school_options["unobserved-id"] = "Synthetic University"
    assert asyncio.run(provider.request_allowed(fake_request("ApiSetFormValue", expected), "human"))


def test_unknown_post_and_api_key_submission_host_are_never_allowed():
    provider = AshbyApplication({"apply_url": URL}, None)
    variables = {"organizationHostedJobsPageName": "ExampleCo", "jobPostingId": POSTING}
    assert not asyncio.run(provider.request_allowed(fake_request("ApiJobPosting", variables, url="https://api.ashbyhq.com/applicationForm.submit"), "opening"))
    assert not asyncio.run(provider.request_allowed(fake_request("ApiJobPosting", variables, url="https://jobs.ashbyhq.com/api/collect"), "opening"))


def test_final_capability_requires_reservation_exact_render_and_one_request_only():
    provider = AshbyApplication({"apply_url": URL}, None)
    provider.document_verified = True
    provider._accept_form(form())
    provider.application_id, provider.posting = "render-1", {}
    provider.control = SimpleNamespace(verify=lambda: asyncio.sleep(0, result=True), reserved=False)
    variables = {"organizationHostedJobsPageName": "ExampleCo", "jobPostingId": POSTING, "formRenderIdentifier": "render-1",
                 "formDefinitionIdentifier": "definition-1", "actionIdentifier": "submit-1", "recaptchaToken": "synthetic-token"}
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSubmitSingleApplicationFormAction", variables), "human"))
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSubmitSingleApplicationFormAction", variables), "submitting"))
    provider.control.reserved = True
    for key, value in (("jobPostingId", "wrong-posting"), ("formRenderIdentifier", "stale-render"), ("actionIdentifier", "wrong-action")):
        assert not asyncio.run(provider.request_allowed(fake_request("ApiSubmitSingleApplicationFormAction", {**variables, key: value}), "submitting"))
    assert asyncio.run(provider.request_allowed(fake_request("ApiSubmitSingleApplicationFormAction", variables), "submitting"))
    assert provider.is_final_request(fake_request("ApiSubmitSingleApplicationFormAction", variables))
    assert not asyncio.run(provider.request_allowed(fake_request("ApiSubmitSingleApplicationFormAction", variables), "submitting"))


def test_resume_body_rejects_wrong_file_bytes_names_extra_fields_and_changed_selected_file(tmp_path):
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(b"%PDF synthetic selected file")
    provider = AshbyApplication({"apply_url": URL}, resume)
    provider.active = {"contentType": "application/pdf"}
    provider.upload = {"url": UPLOAD, "fields": {"key": "synthetic/key"}, "handle": "synthetic-handle"}
    def request(payload=b"", filename="synthetic-resume.pdf", extra=False):
        chunks = [("Content-Type", b"application/pdf"), ("key", b"synthetic/key")]
        if extra:
            chunks.append(("unexpected", b"extra"))
        raw = b"".join(b'--boundary\r\nContent-Disposition: form-data; name="'+name.encode()+b'"\r\n\r\n'+value+b'\r\n' for name, value in chunks)
        raw += b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="'+filename.encode()+b'"\r\nContent-Type: application/pdf\r\n\r\n'+payload+b'\r\n--boundary--\r\n'
        return SimpleNamespace(method="POST", post_data_buffer=raw, headers={"content-type": "multipart/form-data; boundary=boundary"})
    restored = provider._upload_body(request())
    assert resume.read_bytes() in restored
    assert provider._upload_body(request(b"DIFFERENT FILE")) is None
    assert provider._upload_body(request(filename="different.pdf")) is None
    assert provider._upload_body(request(extra=True)) is None
    resume.write_bytes(b"%PDF selected file changed after approval")
    assert provider._upload_body(request()) is None


@pytest.mark.parametrize("url,fields", [("https://evil.example.test/upload", {"key": "x"}),
                                        ("https://ashby-synthetic.s3.amazonaws.com/", {"key": "x", "acl": "public-read"}),
                                        ("https://ashby-synthetic.s3.amazonaws.com/private?token=x", {"key": "x"})])
def test_upload_capability_rejects_non_s3_public_acl_or_unexpected_url_shape(tmp_path, url, fields):
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(b"synthetic")
    provider = AshbyApplication({"apply_url": URL}, resume)
    provider.document_verified = True
    provider._accept_form(form())
    provider.active = {"file": True}
    provider.control = SimpleNamespace(verify=lambda: asyncio.sleep(0, result=True), reserved=False)
    variables = {"organizationHostedJobsPageName": "ExampleCo", "fileUploadContext": "NonUserFormEngine",
                 "filename": resume.name, "contentType": "application/pdf", "contentLength": resume.stat().st_size}
    request = fake_request("ApiCreateFileUploadHandle", variables)
    async def check():
        assert await provider.request_allowed(request, "uploading")
        async def body():
            return json.dumps({"data": {"fileUploadHandle": {"url": url, "fields": fields, "handle": "synthetic-handle"}}}).encode()
        await provider.observe_response(SimpleNamespace(request=request, status=200, url=request.url, body=body))
        assert provider.upload is None
    asyncio.run(check())


def test_actual_session_registry_retains_verification_browser_then_resumes_one_guarded_attempt(tmp_path):
    from jobradar.application_sessions import ApplicationSessions
    async def check():
        resume = tmp_path / "synthetic-resume.pdf"
        resume.write_bytes(b"%PDF-1.4\nSynthetic registry integration only\n%%EOF")
        saved_form = form("verification")
        observed = {"attempts": 0, "uploads": [], "finals": 0}
        async with async_playwright() as p:
            async def factory(control):
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                class Route:
                    def __init__(self, route):
                        self.route, self.request = route, route.request
                    async def abort(self):
                        await self.route.abort()
                    async def continue_(self, **kwargs):
                        request = self.request
                        if request.is_navigation_request():
                            return await self.route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html("verification"))
                        if request.url == UPLOAD:
                            raw = kwargs.get("post_data", request.post_data_buffer)
                            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + request.headers["content-type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw)
                            uploaded = next(part.get_payload(decode=True) for part in message.iter_parts() if part.get_param("name", header="content-disposition") == "file")
                            observed["uploads"].append(uploaded)
                            return await self.route.fulfill(status=204)
                        body = json.loads(request.post_data_buffer)
                        operation, variables = body["operationName"], body["variables"]
                        if operation == "ApiJobPosting":
                            payload = {"jobPosting": {"id": POSTING, "applicationForm": deepcopy(saved_form), "surveyForms": []}}
                        elif operation == "ApiCreateFileUploadHandle":
                            payload = {"fileUploadHandle": {"url": UPLOAD, "fields": {"key": "synthetic/key"}, "handle": "synthetic-handle"}}
                        elif operation in {"ApiSetFormValue", "ApiSetFormValueToFile"}:
                            entry = next(entry for entry in saved_form["sections"][0]["fieldEntries"] if entry["field"]["path"] == variables["path"])
                            entry["fieldValue"] = {"filename": resume.name} if operation.endswith("ToFile") else {"value": variables["value"]}
                            payload = {_OPERATIONS[operation][1]: deepcopy(saved_form)}
                        elif operation == "ApiSubmitSingleApplicationFormAction":
                            observed["finals"] += 1
                            assert control.reserved and control.final_request_count == 1
                            payload = {"submitApplicationFormAction": {"applicationFormResult": {"__typename": "FormSubmitSuccess"}}}
                        else:
                            raise AssertionError("Unexpected provider request")
                        return await self.route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": payload}))
                class Context:
                    def __getattr__(self, name):
                        return getattr(context, name)
                    async def route(self, pattern, handler):
                        async def dispatch(route):
                            await handler(Route(route))
                        await context.route(pattern, dispatch)
                return browser, Context()
            sessions = ApplicationSessions(headless=True, browser_factory=factory)
            job = {"id": "synthetic-ashby-registry", "ats": "ashby", "apply_url": URL, "company": "ExampleCo", "title": "Engineer", "country_code": "US"}
            async def verify():
                return True
            async def reserve():
                observed["attempts"] += 1
                return True
            try:
                first = await sessions.start(job, deepcopy(PROFILE), resume, verify=verify, reserve=reserve, fixture=True, binding="test")
                assert first["status"] == "paused", first
                control = sessions.sessions[job["id"]]
                assert first["verification"]["kind"] == "verification"
                assert sessions.available(job["id"], control.session_id)
                assert not control.reserved and not control.final_request_count and not observed["uploads"]
                original_page = control.page
                await control.page.locator(".g-recaptcha").evaluate("node=>node.remove()")
                result = await sessions.resume(job["id"], control.session_id, acknowledge=True, verify=verify, reserve=reserve,
                                               profile=deepcopy(PROFILE), binding="test")
                assert result["status"] == "submitted", (result, control.blocked_requests)
                assert control.page is original_page and control.closed
                assert control.reserved and control.final_request_count == 1
                assert observed == {"attempts": 1, "uploads": [resume.read_bytes()], "finals": 1}
                assert not sessions.sessions
            finally:
                await sessions.close()
    asyncio.run(check())
