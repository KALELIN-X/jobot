"""Ephemeral, resumable application browsers; credentials never enter storage.

Only the small pause receipt is persisted by the API. A process restart cannot
restore authenticated browser state. An attempted submission is never resumed.
"""
from __future__ import annotations

import asyncio
import inspect
import secrets
import time
from pathlib import Path

from .autofill import _blocked_request_info


def application_adapter(job, resume_path, *, fixture=False):
    if job.get("ats") == "ashby":
        from .ashby_application import AshbyApplication
        return AshbyApplication(job, resume_path, fixture=fixture)
    if job.get("ats") == "smartrecruiters":
        from .smartrecruiters_application import SmartRecruitersApplication
        return SmartRecruitersApplication(job, resume_path, fixture=fixture)
    if job.get("ats") == "workday":
        from .workday_application import WorkdayApplication
        return WorkdayApplication(job, resume_path, fixture=fixture)
    raise ValueError("No resumable application adapter for this destination.")


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class ApplicationControl:
    def __init__(self, adapter, job, profile, resume_path, verify, reserve, on_progress=None):
        self.adapter, self.job, self.profile = adapter, job, profile
        self.resume_path = Path(resume_path) if resume_path else None
        self._verify, self._reserve, self._progress = verify, reserve, on_progress
        self.page = None
        self.phase, self.started, self.reserved = "opening", False, False
        self.human_acknowledged, self.final_request_count = False, 0
        self.blocked_requests = []
        self.session_id = secrets.token_hex(24)
        self.created_at = self.touched_at = time.monotonic()
        self.busy, self.closed = False, False
        self.playwright = self.browser = self.context = None
        self.pause_receipt = None

    async def verify(self):
        if self.closed:
            raise ValueError("This browser session has ended.")
        if self._verify is None or await _await(self._verify()) is False:
            raise ValueError("The current application settings are not approved.")

    async def reserve(self):
        await self.verify()
        if self.reserved or self.final_request_count:
            raise ValueError("A submission attempt already exists.")
        if self._reserve is None or await _await(self._reserve()) is False:
            raise ValueError("The final application attempt was refused.")
        self.reserved = True
        return True

    async def emit(self, stage, **details):
        if self._progress:
            await _await(self._progress({"stage": stage, **details}))

    def result(self, status, message, **extra):
        return {"status": status, "message": message, **extra}

    def pause(self, kind, message, *, pending_questions=None):
        if self.reserved or self.final_request_count:
            return self.result("uncertain", "A submission attempt exists. Verify its outcome; automatic resume is disabled.")
        allowed = {"login", "verification", "agreement", "question", "transport", "review"}
        self.phase = "human"
        self.human_acknowledged = False
        self.touched_at = time.monotonic()
        self.pause_receipt = {"state": "paused", "session_id": self.session_id,
                              "pause_id": secrets.token_hex(16),
                              "kind": kind if kind in allowed else "review", "message": str(message)[:1200],
                              "next_action": "Complete this step in the separate Jobot browser, then confirm Resume in Jobot. Keep the server and that browser open."}
        return self.result("paused", str(message)[:1200], verification=dict(self.pause_receipt),
                           pending_questions=pending_questions or [])

    async def guard(self, route):
        request = route.request
        try:
            if hasattr(self.adapter, "ignore_request") and await _await(self.adapter.ignore_request(request)):
                await route.abort()
                return
            final = bool(self.adapter.is_final_request(request))
            allowed = await _await(self.adapter.request_allowed(request, self.phase))
            if final:
                allowed = allowed and self.phase == "submitting" and self.reserved and self.final_request_count == 0
            if allowed and request.method not in {"GET", "HEAD", "OPTIONS"}:
                await self.verify()
            if not allowed:
                raise ValueError("Request refused")
            prepared = None
            if hasattr(self.adapter, "prepare_request"):
                prepared = await _await(self.adapter.prepare_request(request, self.phase))
                if prepared is not None and (not isinstance(prepared, dict) or set(prepared) - {"post_data"}
                                             or not isinstance(prepared.get("post_data"), bytes)):
                    raise ValueError("Invalid transport rewrite")
            if final:
                self.final_request_count += 1
            await route.continue_(**(prepared or {}))
        except Exception:
            if len(self.blocked_requests) < 30:
                stage = {"opening": "opening_form", "filling": "filling_fields", "uploading": "uploading_resume", "next": "checking_answers", "human": "checking_answers", "confirmation": "awaiting_confirmation"}.get(self.phase, self.phase)
                self.blocked_requests.append(_blocked_request_info(request.method, request.url, stage))
            await route.abort()

    async def open(self, *, headless=False, browser_factory=None):
        await self.verify()
        if browser_factory is None:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=headless)
            self.context = await self.browser.new_context(accept_downloads=False, service_workers="block")
        else:
            self.browser, self.context = await browser_factory(self)
        await self.context.route("**/*", self.guard)
        if hasattr(self.context, "route_web_socket"):
            await self.context.route_web_socket("**/*", lambda socket: socket.close())
        self.page = await self.context.new_page()
        self.page.on("close", lambda: setattr(self, "closed", True))
        self.browser.on("disconnected", lambda: setattr(self, "closed", True))
        self.page.set_default_timeout(8000)
        self.page.on("response", self.adapter.observe_response)

    async def close(self):
        self.closed = True
        for item in (self.context, self.browser, self.playwright):
            if item is not None:
                try:
                    close = item.stop if item is self.playwright else item.close
                    await asyncio.wait_for(close(), timeout=2)
                except Exception:
                    pass


class ApplicationSessions:
    """Bounded live browsers, detached from the queue while waiting for a user."""
    def __init__(self, *, headless=False, max_sessions=6, ttl_seconds=3600, browser_factory=None, adapter_factory=application_adapter):
        self.headless, self.max_sessions, self.ttl_seconds = headless, max_sessions, ttl_seconds
        self.browser_factory, self.adapter_factory = browser_factory, adapter_factory
        self.sessions = {}

    def available(self, job_id, session_id):
        control = self.sessions.get(job_id)
        return bool(control and not control.closed and not control.busy and not control.reserved
                    and control.session_id == session_id and control.pause_receipt
                    and time.monotonic() - control.touched_at <= self.ttl_seconds)

    async def _drive(self, control):
        control.busy = True
        try:
            await control.verify()
            result = await asyncio.wait_for(control.adapter.drive(control), timeout=180)
            if not isinstance(result, dict):
                raise ValueError("Invalid application result")
            if result.get("status") == "submitted" and (not control.reserved or control.final_request_count != 1 or control.blocked_requests):
                result = control.result("uncertain", "The application receipt did not pass the single-attempt checks. Verify the employer outcome; do not retry.")
            if result.get("status") == "paused" and not control.reserved:
                return result
            await control.close()
            self.sessions.pop(control.job["id"], None)
            return result
        except asyncio.CancelledError:
            await control.close()
            self.sessions.pop(control.job["id"], None)
            raise
        except Exception:
            result = control.result("uncertain" if control.reserved else "needs_attention",
                                    "The reserved application outcome needs manual verification; automatic retry is disabled." if control.reserved else
                                    "The application stopped before submission. Review the browser step and current settings before starting again.")
            await control.close()
            self.sessions.pop(control.job["id"], None)
            return result
        finally:
            control.busy = False

    async def start(self, job, profile, resume_path, *, verify, reserve, on_progress=None, fixture=False, binding=""):
        await self.expire()
        if job["id"] in self.sessions:
            return {"status": "needs_attention", "message": "This application has a live browser. Resume or cancel its paused session first."}
        if len(self.sessions) >= self.max_sessions:
            return {"status": "needs_attention", "message": "The waiting-browser limit is reached. Finish or cancel a paused application, then queue this role again."}
        try:
            adapter = self.adapter_factory(job, resume_path, fixture=fixture)
        except Exception:
            return {"status": "needs_attention", "message": "This exact posting or supplied document is not supported by its application adapter. Review it before running."}
        control = ApplicationControl(adapter, job, profile, resume_path, verify, reserve, on_progress)
        control.binding = binding
        self.sessions[job["id"]] = control
        try:
            await control.open(headless=self.headless, browser_factory=self.browser_factory)
        except Exception:
            await control.close()
            self.sessions.pop(job["id"], None)
            return {"status": "needs_attention", "message": "The separate application browser could not open. Check Playwright setup and the local desktop session."}
        return await self._drive(control)

    async def resume(self, job_id, session_id, *, acknowledge, verify, reserve, profile, on_progress=None, binding="", pause_id=None):
        if not acknowledge:
            raise ValueError("Confirm you completed the displayed step in the application browser.")
        if not self.available(job_id, session_id):
            raise ValueError("This browser session expired, ended, is busy or was already attempted. Review the saved application; automatic resume is unavailable.")
        control = self.sessions[job_id]
        if pause_id is not None and pause_id != control.pause_receipt.get("pause_id"):
            raise ValueError("The application is waiting at a different step. Review that step before resuming.")
        if control.binding != binding:
            raise ValueError("Your application settings changed. Cancel this browser and start a newly reviewed application.")
        control._verify, control._reserve, control._progress, control.profile = verify, reserve, on_progress, profile
        control.human_acknowledged = True
        control.touched_at = time.monotonic()
        return await self._drive(control)

    async def cancel(self, job_id):
        control = self.sessions.get(job_id)
        if control and control.busy:
            raise ValueError("Wait for this application to pause before closing its browser.")
        if control:
            await control.close()
            self.sessions.pop(job_id, None)

    async def expire(self):
        for job_id, control in list(self.sessions.items()):
            if not control.busy and time.monotonic() - control.touched_at > self.ttl_seconds:
                await self.cancel(job_id)

    async def close(self):
        for control in list(self.sessions.values()):
            await control.close()
        self.sessions.clear()
