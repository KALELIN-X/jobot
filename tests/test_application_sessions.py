"""Real local browsers prove pause/resume without a candidate or employer."""
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from jobradar.application_sessions import ApplicationSessions


@pytest.fixture
def local_application():
    receipts = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            gate = "" if "ready=1" in self.path else '<button id="verify" onclick="this.remove()">Complete local verification</button>'
            body = (f'<html><body>{gate}<label>Personal note<input id="note" name="note"></label>'
                    '<form method="POST" action="/final"><label>Full name<input id="name" name="name" required></label>'
                    '<button type="submit">Submit application</button></form></body></html>').encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            receipts.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<h1>Application received</h1>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}/", receipts
    server.shutdown(); server.server_close(); thread.join(timeout=2)


class LocalAdapter:
    def __init__(self, job, resume_path, *, fixture=False):
        assert fixture
        self.url = job["apply_url"]
        self.uncertain = job.get("uncertain", False)
    def is_final_request(self, request):
        return request.method == "POST" and urlsplit(request.url).path == "/final"
    async def request_allowed(self, request, phase):
        return urlsplit(request.url).netloc == urlsplit(self.url).netloc and (request.method == "GET" or self.is_final_request(request))
    async def observe_response(self, response):
        pass
    async def drive(self, control):
        if not control.started:
            await control.page.goto(self.url)
            control.started = True
        if await control.page.locator("#verify").count():
            return control.pause("verification", "Complete the local verification in this browser.")
        await control.verify()
        await control.page.locator("#name").fill(control.profile["name"])
        await control.reserve()
        control.phase = "submitting"
        await control.page.get_by_role("button", name="Submit application").click()
        await control.page.get_by_text("Application received").wait_for()
        return control.result("uncertain" if self.uncertain else "submitted", "Synthetic local receipt only")


def manager(**kwargs):
    return ApplicationSessions(headless=True, adapter_factory=LocalAdapter, **kwargs)


async def yes():
    return True


def test_same_browser_retains_progress_and_resumes_once(local_application):
    url, receipts = local_application
    async def run():
        sessions = manager()
        try:
            result = await sessions.start({"id": "one", "apply_url": url}, {"name": "Alex Example"}, None,
                                          verify=yes, reserve=yes, fixture=True, binding="original")
            assert result["status"] == "paused" and not receipts
            control = sessions.sessions["one"]
            page = control.page
            await page.locator("#note").fill("Synthetic work retained")
            await page.locator("#verify").click()
            assert await page.locator("#note").input_value() == "Synthetic work retained"
            done = await sessions.resume("one", result["verification"]["session_id"], acknowledge=True,
                                         verify=yes, reserve=yes, profile={"name": "Alex Example"}, binding="original")
            assert done["status"] == "submitted" and len(receipts) == 1
            assert receipts == [b"name=Alex+Example"]
            assert control.page is page and control.closed and not sessions.sessions
            with pytest.raises(ValueError):
                await sessions.resume("one", control.session_id, acknowledge=True, verify=yes, reserve=yes, profile={})
        finally:
            await sessions.close()
    asyncio.run(run())


def test_other_job_can_complete_while_first_waits(local_application):
    url, receipts = local_application
    async def run():
        sessions = manager()
        try:
            first = await sessions.start({"id": "waiting", "apply_url": url}, {"name": "Alex"}, None,
                                          verify=yes, reserve=yes, fixture=True)
            second = await sessions.start({"id": "other", "apply_url": url + "?ready=1"}, {"name": "Other Example"}, None,
                                           verify=yes, reserve=yes, fixture=True)
            assert first["status"] == "paused" and second["status"] == "submitted"
            assert sessions.available("waiting", first["verification"]["session_id"])
            assert len(receipts) == 1
        finally:
            await sessions.close()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["wrong_session", "not_acknowledged", "changed_settings", "stopped", "expired"])
def test_invalid_resume_never_sends(local_application, case):
    url, receipts = local_application
    async def run():
        sessions = manager()
        try:
            result = await sessions.start({"id": "one", "apply_url": url}, {"name": "Alex"}, None,
                                          verify=yes, reserve=yes, fixture=True, binding="original")
            control = sessions.sessions["one"]
            await control.page.locator("#verify").click()
            if case == "expired":
                control.touched_at -= 7200
            async def verify():
                return case != "stopped"
            args = dict(acknowledge=case != "not_acknowledged", verify=verify, reserve=yes,
                        profile={"name": "Alex"}, binding="changed" if case == "changed_settings" else "original")
            if case == "stopped":
                outcome = await sessions.resume("one", control.session_id, **args)
                assert outcome["status"] == "needs_attention"
            else:
                with pytest.raises(ValueError):
                    await sessions.resume("one", "wrong" if case == "wrong_session" else control.session_id, **args)
            assert not receipts
        finally:
            await sessions.close()
    asyncio.run(run())


def test_session_capacity_and_cancel_do_not_create_or_send_an_application(local_application):
    url, receipts = local_application
    async def run():
        sessions = manager(max_sessions=1)
        first = await sessions.start({"id": "one", "apply_url": url}, {"name": "Alex"}, None, verify=yes, reserve=yes, fixture=True)
        blocked = await sessions.start({"id": "two", "apply_url": url}, {"name": "Alex"}, None, verify=yes, reserve=yes, fixture=True)
        assert blocked["status"] == "needs_attention" and len(sessions.sessions) == 1
        await sessions.cancel("one")
        assert not sessions.available("one", first["verification"]["session_id"]) and not receipts
    asyncio.run(run())


def test_uncertain_submission_cannot_resume(local_application):
    url, receipts = local_application
    async def run():
        sessions = manager()
        result = await sessions.start({"id": "one", "apply_url": url + "?ready=1", "uncertain": True}, {"name": "Alex"}, None,
                                      verify=yes, reserve=yes, fixture=True)
        assert result["status"] == "uncertain" and len(receipts) == 1 and not sessions.sessions
    asyncio.run(run())


def test_user_closed_browser_is_not_available_to_resume(local_application):
    url, receipts = local_application
    async def run():
        sessions = manager()
        result = await sessions.start({"id": "one", "apply_url": url}, {"name": "Alex"}, None, verify=yes, reserve=yes, fixture=True)
        await sessions.sessions["one"].page.close()
        assert not sessions.available("one", result["verification"]["session_id"])
        await sessions.cancel("one")
        assert not receipts and not sessions.sessions
    asyncio.run(run())
