"""Read public application questions without filling or submitting an application."""
from __future__ import annotations

import asyncio
import ipaddress
import uuid
from urllib.parse import urlsplit

from .autofill import URLNotApproved, _label, validate_application_url
from .destinations import SUPPORTED_HOSTS, parse_destination
from .form_adapters import SNAPSHOT_JS, read_combobox


LOOKUP_TIMEOUT_SECONDS = 20.0
MAX_COMBOBOXES = 60


def _origin(parts):
    return (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))


def _public_resource(url, initial_url, fixture):
    try:
        parts, initial = urlsplit(url), urlsplit(initial_url)
        if parts.username or parts.password or not parts.hostname:
            return False
        if fixture and _origin(parts) == _origin(initial):
            return parts.scheme == "http"
        if parts.scheme != "https" or parts.port not in (None, 443):
            return False
        hostname = parts.hostname.casefold()
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(hostname).is_global
        except ValueError:
            return "." in hostname
    except (ValueError, TypeError):
        return False


def _same_document(url, initial_url):
    try:
        candidate, initial = urlsplit(url), urlsplit(initial_url)
        return (_origin(candidate) == _origin(initial) and candidate.path == initial.path
                and candidate.query == initial.query and not candidate.fragment
                and not candidate.username and not candidate.password)
    except (ValueError, TypeError):
        return False


def _response(status, message, *, fields=None, blocked=None):
    return {"status": status, "message": message, "fields": fields or [], "blocked_requests": blocked or []}


async def inspect_questions(job, profile_for_planning=None, *, question=None, query=None, fixture=False):
    """Return fresh fields and real visible choices from one exact hosted posting.

    ``profile_for_planning`` is accepted for the caller's planner contract but
    intentionally never read or passed into the browser. A whole-form refresh
    uses no typed queries. A targeted lookup may type only its explicit query
    into one uniquely labelled combobox, without selecting an option. The caller
    must separately validate, save and approve any returned answer.
    """
    url = job.get("apply_url") if isinstance(job, dict) else None
    try:
        validate_application_url(url, {"127.0.0.1"} if fixture else set(SUPPORTED_HOSTS), fixture=fixture)
        if not fixture and url != parse_destination(url)["apply_url"]:
            raise URLNotApproved("A canonical posting URL is required.")
    except (URLNotApproved, TypeError, ValueError):
        return _response("needs_attention", "A canonical supported employer posting is required to refresh questions.")
    if question is not None and (not isinstance(question, str) or not _label(question) or len(question) > 500 or "\x00" in question):
        return _response("needs_attention", "Choose one exact recorded application question.")
    if query is not None and (question is None or not isinstance(query, str) or not query.strip()
                              or len(query) > 240 or any(ord(char) < 32 for char in query)):
        return _response("needs_attention", "A choice search needs one exact question and an explicit query of at most 240 characters.")
    question = _label(question) if question is not None else None
    blocked = []
    browser = context = page = None

    def record(request, category):
        # Host/method/category are sufficient to diagnose a blocked capability.
        # Never retain a URL path/query, request body, cookies or typed answer.
        value = {"hostname": (urlsplit(request.url).hostname or "")[:250],
                 "method": request.method[:12], "category": category}
        if value not in blocked and len(blocked) < 20:
            blocked.append(value)

    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            try:
                async with asyncio.timeout(LOOKUP_TIMEOUT_SECONDS):
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context(accept_downloads=False, service_workers="block")
                    if hasattr(context, "route_web_socket"):
                        await context.route_web_socket("**/*", lambda socket: socket.close())
                    page = await context.new_page()
                    main_navigation_started = False

                    async def guard(route):
                        nonlocal main_navigation_started
                        request = route.request
                        if request.method not in {"GET", "HEAD"}:
                            record(request, "read_only_method")
                            await route.abort()
                            return
                        if request.is_navigation_request():
                            if (request.frame != page.main_frame or main_navigation_started
                                    or not _same_document(request.url, url)):
                                record(request, "posting_navigation")
                                await route.abort()
                                return
                            main_navigation_started = True
                        if not _public_resource(request.url, url, fixture):
                            record(request, "non_public_resource")
                            await route.abort()
                            return
                        await route.continue_()

                    await context.route("**/*", guard)
                    page.set_default_timeout(4000)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                    if not _same_document(page.url, url) or response is None or response.status >= 400:
                        return _response("needs_attention", "The public employer form could not be read at its exact posting URL.", blocked=blocked)
                    await page.locator('input,select,textarea,[role="combobox"]').first.wait_for(state="attached", timeout=5000)
                    snapshot = await page.evaluate(SNAPSHOT_JS, uuid.uuid4().hex)
                    if snapshot.get("captcha") or snapshot.get("login"):
                        return _response("needs_attention", "The form requires account access or verification before its questions can be refreshed.", blocked=blocked)
                    fields = snapshot.get("fields") or []
                    if question is not None:
                        matches = [field for field in fields if field.get("visible") and not field.get("disabled")
                                   and _label(field.get("label")) == question]
                        if len(matches) != 1:
                            return _response("needs_attention", "This exact question was not uniquely visible on the current employer form.", blocked=blocked)
                        target = matches[0]
                        if query is not None and (not target.get("combobox") or target.get("multiple")):
                            return _response("needs_attention", "This control does not support a single-choice search.", blocked=blocked)
                        targets = [target] if target.get("combobox") else []
                    else:
                        targets = [field for field in fields if field.get("combobox") and field.get("visible") and not field.get("disabled")]
                    if len(targets) > MAX_COMBOBOXES:
                        return _response("needs_attention", "The form has too many custom controls for a bounded question refresh.", blocked=blocked)
                    for target in targets:
                        choices = await read_combobox(page, target, query=query if question is not None else None)
                        if choices.get("multiple") is True:
                            target["multiple"] = True
                        # Some async menus render only an owned "start typing"
                        # hint until a query exists, without a listbox yet. That
                        # is a valid blank refresh, not a completed query result.
                        initial_search = (query is None and choices.get("searchable") is True
                                          and choices.get("lookup_required") is True
                                          and choices.get("query_hint") is True
                                          and not choices.get("loading") and not choices.get("options"))
                        supported = choices.get("supported") is True or initial_search
                        failed = choices.get("loading") is True or not supported
                        optional_multiple = (question is None and target.get("multiple") is True
                                             and not target.get("required") and not choices.get("loading"))
                        if failed and not optional_multiple:
                            # A timeout or unreadable menu is not a fresh empty
                            # choice list. Callers preserve their last verified
                            # questions when this inspection did not succeed.
                            return _response("needs_attention", "The current question choices could not finish loading or be read. Previously recorded choices were not refreshed.", blocked=blocked)
                        target["options"] = [{key: value for key, value in option.items() if key != "id"}
                                             for option in choices.get("options", [])]
                        for name in ("searchable", "lookup_required", "minimum_query_length"):
                            if name in choices:
                                target[name] = choices[name]
                        target["unsupported_choices"] = not supported
                    if not _same_document(page.url, url):
                        return _response("needs_attention", "The employer form navigated away during question inspection.", blocked=blocked)
                    return _response("inspected", "Current public questions were read without filling or submitting an application.", fields=fields, blocked=blocked)
            finally:
                if context is not None:
                    try:
                        await asyncio.wait_for(context.close(), 1)
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.close(), 1)
                    except Exception:
                        pass
    except TimeoutError:
        return _response("needs_attention", "Question inspection reached its bounded time limit. No application was submitted.", blocked=blocked)
    except Exception:
        return _response("needs_attention", "The public form could not be inspected. No application was submitted.", blocked=blocked)
