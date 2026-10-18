"""Synthetic local-browser checks for Greenhouse-shaped application controls.

Fixtures reflect the public hosted form's labelled upload groups, ARIA-required
name fields, and React-select listbox/required proxy. No employer is contacted.
"""

import asyncio
import json
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from jobradar.autofill import (
    URLNotApproved, plan_form, run_application,
    validate_application_url, validate_destination_url,
)
from jobradar.form_adapters import GreenhouseTransport


GREENHOUSE = "https://job-boards.greenhouse.io/example/jobs/1234567"
GREENHOUSE_HOSTS = {"job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io"}
PROFILE = {"name": "Alex Example", "first_name": "Alex", "last_name": "Example",
           "email": "alex@example.test", "phone": "+15555550123", "work_authorized": True,
           "needs_sponsorship": False, "answers": {"Country": "United States"}}


def combo(identifier, label, options):
    labels = "|".join(options)
    return f'''<div class="field-wrapper"><label id="{identifier}-label" for="{identifier}">{label}*</label>
      <div class="select__container" data-options="{labels}">
        <div class="select__single-value"></div>
        <input id="{identifier}" role="combobox" aria-labelledby="{identifier}-label" aria-required="true"
          aria-expanded="false" aria-controls="{identifier}-listbox" autocomplete="off">
        <input required tabindex="-1" aria-hidden="true" class="fixture-requiredInput" style="opacity:0;pointer-events:none;position:absolute">
      </div></div>'''


def form_html(variant):
    custom = combo("shift", "Preferred shift?", ["Day", "Night"]) if variant == "unknown" else ""
    if variant == "search":
        custom = combo("city", "Location (City)", ["Boston, Massachusetts, United States"])
    legal = '<label><input type="checkbox" required>I agree to the terms</label>' if variant == "legal" else ""
    captcha = '<div class="g-recaptcha" data-sitekey="synthetic-fixture"></div>' if variant == "captcha" else ""
    return ('''<!doctype html><html><head><style>
      body{font:16px sans-serif;padding:20px}form{max-width:600px}label{display:block;margin-top:12px}
      input{display:block;padding:8px}.select__container{position:relative}.select__single-value{min-height:18px}
      [role=listbox]{background:#eee;padding:4px}[role=option]{padding:6px;cursor:pointer}
      .visually-hidden{width:1px;height:1px;opacity:0;position:absolute}button{padding:10px;margin-top:15px}
      </style></head><body><h1>Example engineering position</h1><h2>Apply for this job</h2>
      <form id="application-form" method="get" action="/mock/apply">
      <label for="first_name">First Name*</label><input id="first_name" aria-label="First Name" aria-required="true">
      <label for="last_name">Last Name*</label><input id="last_name" aria-label="Last Name" aria-required="true">
      <label for="email">Email*</label><input id="email" type="email" aria-required="true">
      <label for="phone">Phone</label><input id="phone" type="tel">
      ''' + combo("country", "Country", ["Canada", "United States"]) +
      combo("auth", "Are you legally authorized to work in the United States?", ["Yes", "No"]) +
      combo("sponsor", "Will you now or in the future require visa sponsorship?", ["Yes", "No"]) + '''
      <div role="group" class="file-upload" aria-labelledby="upload-label-resume" aria-required="true">
      <div id="upload-label-resume">Resume/CV*</div><label for="resume" class="visually-hidden">Attach</label>
      <input id="resume" type="file" class="visually-hidden" accept=".txt,.pdf"></div>
      ''' + custom + legal + captcha + '''
      <button type="submit">Submit application</button></form>
      <script>
      const variant = VARIANT;
      document.querySelectorAll('[role=combobox]').forEach(input => {
        const root=input.closest('.select__container');
        input.addEventListener('click',() => {
          document.querySelectorAll('[role=listbox]').forEach(node=>node.remove());
          const menu=document.createElement('div');menu.id=input.id+'-listbox';menu.setAttribute('role','listbox');
          const renderOptions = () => {menu.innerHTML='';root.dataset.options.split('|').forEach(label => {
            const option=document.createElement('div');option.setAttribute('role','option');option.textContent=label;
            option.addEventListener('click',event=>{
              event.stopPropagation();root.querySelector('.select__single-value').textContent=label;input.value='';
              if(variant!=='uncommitted'||input.id!=='country')root.querySelector('[aria-hidden=true]').value=label;
              input.setAttribute('aria-expanded','false');menu.remove();
            });menu.appendChild(option);
          })};
          if(variant!=='search'||input.id!=='city'||input.value)renderOptions();
          root.appendChild(menu);input.setAttribute('aria-expanded','true');
          if(variant==='search'&&input.id==='city')input.oninput=renderOptions;
        });
        input.addEventListener('keydown',event=>{if(event.key==='Escape'){root.querySelector('[role=listbox]')?.remove();input.setAttribute('aria-expanded','false')}});
      });
      if(variant==='premature')document.getElementById('first_name').addEventListener('input',()=>fetch('/mock/thanks',{method:'POST',body:new FormData()}));
      document.getElementById('application-form').addEventListener('submit',async event => {
        event.preventDefault();const data=new FormData();
        ['first_name','last_name','email','phone'].forEach(id=>data.append(id,document.getElementById(id).value));
        ['country','auth','sponsor'].forEach(id=>data.append(id,document.getElementById(id).closest('.select__container').querySelector('.select__single-value').textContent));
        ['shift','city'].forEach(id=>{const input=document.getElementById(id);if(input)data.append(id,input.closest('.select__container').querySelector('.select__single-value').textContent)});
        data.append('resume',document.getElementById('resume').files[0]);
        const response=await fetch('/mock/thanks?variant='+variant,{method:'POST',body:data});
        document.body.innerHTML=await response.text();
      });
      </script></body></html>''').replace("VARIANT", repr(variant))


@pytest.fixture
def employer():
    receipts = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if urlsplit(self.path).path != "/mock/apply":
                self.send_error(404)
                return
            variant = parse_qs(urlsplit(self.path).query).get("variant", ["success"])[0]
            payload = form_html(variant).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            content = self.rfile.read(length)
            parsed = BytesParser(policy=policy.default).parsebytes(
                ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + content)
            receipts.append({part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in parsed.iter_parts()})
            variant = parse_qs(urlsplit(self.path).query).get("variant", ["success"])[0]
            body = b"<h1>Thank you for applying</h1><p>Your application has been received.</p>"
            self.send_response(500 if variant == "server_error" else 200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mock/apply", receipts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def run_fixture(employer, tmp_path, variant="success", profile=None, permitted=True):
    url, receipts = employer
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_text("Synthetic demo resume. Python backend engineering.")
    reservations = []
    async def reserve():
        assert not receipts, "No application request may precede atomic reservation"
        reservations.append("reserved")
        return permitted
    result = asyncio.run(run_application(
        {"apply_url": url + "?variant=" + variant, "ats": "greenhouse", "demo": True},
        profile or PROFILE, resume, allowed_hosts={"127.0.0.1"},
        artifacts_dir=tmp_path / "artifacts", fixture=True, before_submit=reserve))
    return result, reservations, receipts, resume


@pytest.mark.parametrize("host", sorted(GREENHOUSE_HOSTS))
def test_greenhouse_exact_posting_urls_are_supported(host):
    url = GREENHOUSE.replace("job-boards.greenhouse.io", host)
    assert validate_application_url(url, GREENHOUSE_HOSTS) == host
    validate_destination_url(url + "/confirmation", url, GREENHOUSE_HOSTS, document=True)
    with pytest.raises(URLNotApproved):
        validate_destination_url(url.replace("1234567", "7654321"), url, GREENHOUSE_HOSTS, document=True)
    with pytest.raises(URLNotApproved):
        validate_destination_url(f"https://{host}/another-board", url, GREENHOUSE_HOSTS, document=True)


def test_browser_greenhouse_shape_uploads_and_commits_choices_then_submits_once(employer, tmp_path):
    result, reservations, receipts, resume = run_fixture(employer, tmp_path)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"]
    assert len(receipts) == 1
    assert receipts[0]["resume"] == resume.read_bytes()
    assert receipts[0]["country"] == b"United States"
    assert receipts[0]["auth"] == b"Yes"
    assert receipts[0]["sponsor"] == b"No"
    assert receipts[0]["first_name"] == b"Alex"


def test_browser_unknown_react_select_captures_exact_question_and_options(employer, tmp_path):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "unknown")
    assert result["status"] == "needs_attention", result
    question = next(item for item in result["pending_questions"] if item["question"] == "Preferred shift?")
    assert question["type"] == "select" and question["required"]
    assert question["options"] == [{"label": "Day", "value": "Day"}, {"label": "Night", "value": "Night"}]
    assert not reservations and not receipts


def test_browser_saved_exact_custom_choice_is_reused(employer, tmp_path):
    profile = {**PROFILE, "answers": {**PROFILE["answers"], "Preferred shift?": "Day"}}
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "unknown", profile)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"] and len(receipts) == 1
    assert receipts[0]["shift"] == b"Day"


def test_browser_searchable_location_requires_and_commits_an_exact_result(employer, tmp_path):
    profile = {**PROFILE, "answers": {**PROFILE["answers"], "Location (City)": "Boston, Massachusetts, United States"}}
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "search", profile)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"] and len(receipts) == 1
    assert receipts[0]["city"] == b"Boston, Massachusetts, United States"


def test_browser_site_cannot_submit_on_input_before_attempt_reservation(employer, tmp_path):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "premature")
    assert result["status"] in {"needs_attention", "failed"}, result
    assert not reservations and not receipts


def test_browser_required_proxy_must_actually_be_valid_after_real_option_click(employer, tmp_path):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "uncommitted")
    assert result["status"] == "needs_attention", result
    assert "invalid" in result["message"]
    assert not reservations and not receipts


@pytest.mark.parametrize("variant", ["legal", "captcha"])
def test_browser_challenges_or_legal_fields_never_submit(employer, tmp_path, variant):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, variant)
    assert result["status"] == "needs_attention", result
    assert not reservations and not receipts


def test_browser_final_recheck_refusal_does_not_click(employer, tmp_path):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, permitted=False)
    assert result["status"] == "needs_attention", result
    assert reservations == ["reserved"] and not receipts


def test_browser_server_error_cannot_become_success_just_from_confirmation_words(employer, tmp_path):
    result, reservations, receipts, _ = run_fixture(employer, tmp_path, "server_error")
    assert result["status"] == "uncertain", result
    assert reservations == ["reserved"] and len(receipts) == 1
    assert "Do not retry" in result["message"] or "do not retry" in result["message"]


def test_month_only_fact_answers_month_and_year_without_inventing_a_day():
    def field(label, kind):
        return {"id": label, "label": label, "type": kind, "tag": "input", "required": True, "visible": True}
    profile = {"graduation_month": "2024-07"}
    plan = plan_form([field("Graduation date", "month"), field("Graduation year", "number"), field("Graduation date", "date")], profile)
    assert [(action.field_id, action.value) for action in plan.actions] == [("Graduation date", "2024-07"), ("Graduation year", "2024")]
    assert plan.blockers and plan.pending_questions[0]["type"] == "date"


@pytest.mark.parametrize("country", ["Canada", "Japan", "the United Kingdom"])
def test_authorization_for_another_country_requires_an_exact_answer(country):
    item = {"id": "q", "label": f"Are you authorized to work in {country}?", "type": "select", "tag": "select", "required": True,
            "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]}
    plan = plan_form([item], {"work_authorized": True})
    assert plan.blockers and not plan.actions


@pytest.fixture
def hosted_transport():
    """Three real HTTP origins: rendered form, advertised API, advertised upload."""
    receipts = {"upload": [], "application": [], "unrelated": []}
    config = {}
    servers, threads = [], []

    def handler_for(kind):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, body, content_type="application/json", status=200):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", config["form_origin"])
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.respond(b"", status=204)

            def do_GET(self):
                if kind == "metadata" and urlsplit(self.path).path == "/uncacheable_attributes/presigned_fields":
                    endpoint = config["upload_url"]
                    if config["variant"] == "wrong_bucket":
                        endpoint = config["metadata_origin"] + "/upload"
                    self.respond(json.dumps({"url": endpoint, "resume": {
                        "fields": {"policy": "synthetic-policy", "signature": "synthetic-signature"},
                        "key": "resume/{timestamp}/{unique_id}/synthetic-resume.txt"}}).encode())
                elif kind == "form" and urlsplit(self.path).path == "/mock/apply":
                    html = form_html("success")
                    submit_url = config["submit_url"] + ("9" if config["variant"] == "wrong_posting" else "")
                    marker = '<script type="application/json">' + json.dumps({"submitPath": submit_url}) + '</script>'
                    bootstrap = """
                    let uploadMetadata;
                    const metadataReady=fetch(METADATA).then(response=>response.json()).then(value=>uploadMetadata=value);
                    document.getElementById('resume').addEventListener('change',async event=>{
                      await metadataReady;
                      const payload=new FormData();
                      payload.append('utf8','✓');
                      Object.entries(uploadMetadata.resume.fields).forEach(([key,value])=>payload.append(key,value));
                      payload.append('key',uploadMetadata.resume.key.replace('{timestamp}',Date.now()).replace('{unique_id}','fixture123'));
                      payload.append('authenticity_token','1234');payload.append('Content-Type','application/octet-stream');
                      payload.append('file',event.target.files[0]);
                      const response=await fetch(uploadMetadata.url,{method:'POST',body:payload});
                      if(response.ok)event.target.dataset.uploaded='true';
                    });
                    """.replace("METADATA", json.dumps(config["metadata_origin"] + "/uncacheable_attributes/presigned_fields?fields[]=resume"))
                    if config["variant"] == "premature":
                        bootstrap += "document.getElementById('first_name').addEventListener('input',()=>fetch(" + json.dumps(config["submit_url"]) + ",{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));"
                    html = html.replace("const variant = 'success';", "const variant = 'success';" + bootstrap)
                    application_request = "fetch(" + json.dumps(config["submit_url"]) + ",{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_application:{first_name:document.getElementById('first_name').value,resume_uploaded:document.getElementById('resume').dataset.uploaded==='true'}})})"
                    submission = "const response=await " + application_request + ";"
                    if config["variant"] == "double_submit":
                        submission += "try{await " + application_request + ";}catch(error){}"
                    elif config["variant"] == "unrelated":
                        submission += "try{await fetch('/collect',{method:'POST',body:'unapproved'});}catch(error){}"
                    elif config["variant"] == "upload_only":
                        submission = "const response={text:async()=>'<h1>Thank you for applying</h1><p>Your application has been received.</p>'};"
                    html = html.replace("const response=await fetch('/mock/thanks?variant='+variant,{method:'POST',body:data});", submission)
                    self.respond((marker + html).encode(), "text/html; charset=utf-8")
                else:
                    self.send_error(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if kind == "upload" and self.path == "/upload":
                    parsed = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
                    receipts["upload"].append({part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in parsed.iter_parts()})
                    self.respond(b"", status=201)
                elif kind == "metadata" and self.path == "/example/jobs/1234567":
                    receipts["application"].append(json.loads(body))
                    self.respond(b"<h1>Thank you for applying</h1><p>Your application has been received.</p>", "text/html")
                elif kind == "form":
                    receipts["unrelated"].append(body)
                    self.respond(b"{}")
                else:
                    self.send_error(404)
        return Handler

    for kind in ("form", "metadata", "upload"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(kind))
        servers.append(server)
        config[kind + "_origin"] = f"http://127.0.0.1:{server.server_port}"
    config.update({"submit_url": config["metadata_origin"] + "/example/jobs/1234567", "upload_url": config["upload_origin"] + "/upload", "variant": "success"})
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        yield config, receipts
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=3)


def run_hosted_fixture(hosted_transport, tmp_path, variant="success"):
    config, receipts = hosted_transport
    config["variant"] = variant
    resume = tmp_path / "synthetic-resume.txt"
    resume.write_bytes(b"Synthetic PDF-like bytes\nwith LF\r\nand CRLF\x00\xff\xfe; not a real resume.")
    reservations = []
    async def reserve():
        assert len(receipts["upload"]) == 1
        assert not receipts["application"]
        reservations.append("reserved")
        return True
    result = asyncio.run(run_application(
        {"apply_url": config["form_origin"] + "/mock/apply", "ats": "greenhouse", "demo": True},
        PROFILE, resume, allowed_hosts={"127.0.0.1"}, artifacts_dir=tmp_path / "artifacts",
        fixture=True, fixture_transport={"submit_url": config["submit_url"], "upload_url": config["upload_url"]}, before_submit=reserve))
    return result, receipts, reservations, resume


def test_browser_advertised_cross_origin_resume_and_exact_posting_transport(hosted_transport, tmp_path):
    result, receipts, reservations, resume = run_hosted_fixture(hosted_transport, tmp_path)
    assert result["status"] == "submitted", result
    assert reservations == ["reserved"]
    assert len(receipts["application"]) == len(receipts["upload"]) == 1
    assert receipts["upload"][0]["file"] == resume.read_bytes()
    assert receipts["application"][0]["job_application"] == {"first_name": "Alex", "resume_uploaded": True}


@pytest.mark.parametrize("variant", ["wrong_bucket", "wrong_posting", "premature"])
def test_browser_unadvertised_or_premature_transport_cannot_send_applicant_data(hosted_transport, tmp_path, variant):
    result, receipts, reservations, _ = run_hosted_fixture(hosted_transport, tmp_path, variant)
    assert result["status"] == "needs_attention", result
    assert not receipts["upload"] and not receipts["application"] and not reservations


@pytest.mark.parametrize("variant", ["double_submit", "unrelated", "upload_only"])
def test_browser_upload_success_cannot_mask_duplicate_or_unrelated_application_requests(hosted_transport, tmp_path, variant):
    result, receipts, reservations, _ = run_hosted_fixture(hosted_transport, tmp_path, variant)
    assert result["status"] == "uncertain", result
    assert reservations == ["reserved"] and len(receipts["upload"]) == 1
    assert len(receipts["application"]) == (0 if variant == "upload_only" else 1)
    assert not receipts["unrelated"]


def test_upload_capability_is_bound_to_verified_posting_bucket_fields_and_file(tmp_path):
    resume = tmp_path / "resume.txt"
    resume.write_bytes(b"synthetic resume bytes")
    transport = GreenhouseTransport(GREENHOUSE, resume)
    assert not transport.verify_document('{"submitPath":"https://boards.greenhouse.io/example/jobs/7654321"}')
    assert not transport.is_submit_url("https://boards.greenhouse.io/example/jobs/1234567")
    assert transport.verify_document('{"submitPath":"https://boards.greenhouse.io/example/jobs/1234567"}')
    metadata = {"url": "https://grnhse-prod-jben-us-east-1.s3.amazonaws.com", "resume": {"fields": {"policy": "synthetic"}, "key": "resume/{timestamp}/{unique_id}/resume.txt"}}
    assert not transport.accept_upload_fields({**metadata, "url": "https://unrelated.s3.amazonaws.com"})
    assert transport.accept_upload_fields(metadata)
    def multipart(extra=None, file_bytes=b"synthetic resume bytes"):
        values = {"policy": "synthetic", "key": "resume/1760000000000/abc123/resume.txt", "utf8": "✓", "authenticity_token": "1234", "Content-Type": "application/octet-stream", **(extra or {})}
        body = b""
        for name, value in values.items():
            body += (f'--boundary\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
        return body + b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="resume.txt"\r\nContent-Type: text/plain\r\n\r\n' + file_bytes + b'\r\n--boundary--\r\n'
    args = (metadata["url"], "POST", {"content-type": "multipart/form-data; boundary=boundary"})
    assert not transport.permits_upload(*args, multipart())
    transport.upload_active = True
    assert transport.permits_upload(*args, multipart())
    for extra in ({"policy": "tampered"}, {"unexpected": "applicant secret"}, {"authenticity_token": "different"}, {"key": "other/resume.txt"}):
        assert not transport.permits_upload(*args, multipart(extra))
    assert not transport.permits_upload(*args, multipart(file_bytes=b"wrong resume"))
