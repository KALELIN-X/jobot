"""Small, best-effort progress events without application answers or credentials."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


ProgressCallback = Callable[[dict], Awaitable[None]]
STAGES = frozenset({
    "opening_form", "inspecting_form", "filling_fields", "uploading_resume",
    "checking_answers", "submitting", "awaiting_confirmation", "finished",
    "needs_attention", "uncertain", "failed",
})
TERMINAL_STATUSES = frozenset({"submitted", "needs_attention", "uncertain", "failed"})


class ProgressReporter:
    """Report observed actions only; a broken observer never retries the worker.

    The callback is optional and receives a new plain dictionary. On its first
    exception/timeout it is disabled for this run so observer failures cannot
    repeatedly delay browser actions. External worker cancellation still works.
    """

    def __init__(self, callback: ProgressCallback | None, *, timeout: float = 0.5):
        self.callback = callback
        self.timeout = timeout
        self.disabled = False
        self.last_event = None
        self.finished = False

    async def emit(self, stage: str, **details) -> None:
        if not self.callback or self.disabled or self.finished or stage not in STAGES:
            return
        event = {"stage": stage}
        for key in ("field_count", "completed_fields", "pending_question_count"):
            value = details.get(key)
            if type(value) is int and 0 <= value <= 10000:
                event[key] = value
        if details.get("status") in TERMINAL_STATUSES:
            event["status"] = details["status"]
        screenshot = details.get("screenshot")
        if isinstance(screenshot, str) and 0 < len(screenshot) <= 2000:
            event["screenshot"] = screenshot
        if event == self.last_event:
            return
        self.last_event = dict(event)
        try:
            await asyncio.wait_for(self.callback(dict(event)), self.timeout)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            self.disabled = True
        except Exception:
            self.disabled = True
        if stage in {"finished", "needs_attention", "uncertain", "failed"}:
            self.finished = True

    async def finish(self, status: str, **details) -> None:
        if status in TERMINAL_STATUSES:
            await self.emit("finished" if status == "submitted" else status, status=status, **details)
