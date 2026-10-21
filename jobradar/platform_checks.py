"""Public, data-free platform preflight; never a candidate application run."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from .platforms import platform_support
from .question_lookup import _public_resource


def _report(result: dict, support: dict, blocked: int = 0) -> dict:
    fields = []
    for item in result.get("fields", [])[:60]:
        if not isinstance(item, dict) or item.get("type") == "hidden":
            continue
        options = [{"label": str(option.get("label") or "")[:500], "disabled": option.get("disabled") is True}
                   for option in item.get("options", [])[:100] if isinstance(option, dict)]
        fields.append({"label": str(item.get("label") or "Unlabelled control")[:500],
                       "type": str(item.get("type") or "unknown")[:30], "required": item.get("required") is True,
                       "options": options})
    gates = [{"kind": gate.get("kind") if isinstance(gate, dict) and gate.get("kind") in ("review", "transport") else "review",
              "message": str(gate.get("message") if isinstance(gate, dict) else gate)[:700]}
             for gate in result.get("gates", [])[:15]]
    if blocked:
        gates.append({"kind": "transport", "message": "The public check blocked a data-changing request or a navigation outside the pinned posting. The complete form may need a specialized adapter."})
    return {"platform": support["platform"], "mode": "inspection_only",
            "automatic_submission_supported": False, "stage": str(result.get("stage") or "unavailable")[:60],
            "fields": fields, "field_count": len(fields), "gates": gates,
            "next_action": str(result.get("next_action") or support["next_action"])[:1000],
            "message": "Public controls were inspected without applicant details, uploads or submission. This does not validate later steps or enable automatic applications."}


async def inspect_public_platform(job: dict) -> dict:
    support = platform_support(job)
    url = job.get("apply_url")
    if support["platform"] not in {"ashby", "smartrecruiters"} or not support["can_inspect"]:
        return _report({"stage": "unsupported", "gates": [support["reason"]]}, support)
    if support["platform"] == "ashby":
        from .ashby_adapter import ashby_document_allowed, inspect_ashby_page, parse_ashby_destination
        url = parse_ashby_destination(url)["apply_url"]
        document_allowed = lambda target: (not urlsplit(target).query and not urlsplit(target).fragment
                                           and ashby_document_allowed(target, url))
        inspect_page = inspect_ashby_page
    else:
        from .smartrecruiters_adapter import inspect_smartrecruiters_page, parse_smartrecruiters_url
        url = parse_smartrecruiters_url(url)["canonical_url"]
        origin = urlsplit(url)
        document_urls = {url}
        document_allowed = lambda target: target in document_urls
        inspect_page = inspect_smartrecruiters_page

    async def inspect():
        nonlocal url
        from playwright.async_api import async_playwright
        blocked = 0
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")
                if hasattr(context, "route_web_socket"):
                    await context.route_web_socket("**/*", lambda socket: socket.close())
                async def guard(route):
                    nonlocal blocked
                    request = route.request
                    try:
                        permitted = (request.method in {"GET", "HEAD"} and _public_resource(request.url, url, False)
                                     and (not request.is_navigation_request() or document_allowed(request.url)))
                    except (TypeError, ValueError):
                        permitted = False
                    if not permitted:
                        blocked += 1
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route("**/*", guard)
                page = await context.new_page()
                page.set_default_timeout(5000)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if not document_allowed(page.url):
                    return _report({"stage": "unavailable", "gates": ["The posting redirected outside the pinned destination."]}, support, blocked)
                if support["platform"] == "smartrecruiters" and parse_smartrecruiters_url(url)["page_type"] == "posting":
                    initial = parse_smartrecruiters_url(url)
                    # Follow only a single exact, visible application link
                    # actually published by this employer posting. Its distinct
                    # publication UUID is pinned before navigation; never guess it.
                    links = await page.locator("a[href]").evaluate_all("nodes => nodes.filter(node => node.getClientRects().length && /^(?:i.m interested|apply(?: now)?)$/i.test(node.innerText.trim())).map(node => node.href)")
                    targets = set()
                    for link in links[:100]:
                        parsed = parse_smartrecruiters_url(link)
                        if (parsed and parsed["page_type"] == "application" and parsed["company"] == initial["company"]
                                and urlsplit(parsed["canonical_url"]).netloc == origin.netloc):
                            targets.add(parsed["canonical_url"])
                    if len(targets) == 1:
                        url = next(iter(targets))
                        document_urls.add(url)
                        response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                        if not document_allowed(page.url):
                            return _report({"stage": "unavailable", "gates": ["The advertised application redirected outside the pinned publication."]}, support, blocked)
                try:
                    await page.locator("input,select,textarea,[role=combobox]").first.wait_for(state="attached", timeout=5000)
                except Exception:
                    pass
                result = await inspect_page(page, url)
                if response is not None and response.status in {401, 403, 404, 410, 429}:
                    result.update(stage="unavailable", gates=[f"The public page returned HTTP {response.status}; no complete application flow was verified."])
                return _report(result, support, blocked)
            finally:
                if 'context' in locals():
                    try:
                        await asyncio.wait_for(context.close(), timeout=1)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(browser.close(), timeout=1)
                except Exception:
                    pass
    try:
        return await asyncio.wait_for(inspect(), timeout=35)
    except Exception:
        return _report({"stage": "unavailable", "gates": ["The public platform check could not finish within its guarded browser boundary. No candidate details were supplied."]}, support)
