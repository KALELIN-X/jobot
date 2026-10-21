"""Real Chromium platform checks with every request fulfilled in memory.

The public-looking origins below are intercepted locally before any network
connection. No employer, candidate data store, credentials or résumé is used.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright as real_playwright

from jobradar.platform_checks import inspect_public_platform
from test_ashby_forms import HTML as ASHBY_FORM, IDENTITY


ASHBY = "https://jobs.ashbyhq.com/ExampleCo/12345678-1234-1234-1234-123456789abc/application"
SMART = "https://jobs.smartrecruiters.com/ExampleCo/123456789-software-engineer"
ONECLICK = "https://jobs.smartrecruiters.com/oneclick-ui/company/ExampleCo/publication/abcdef12-1234-1234-1234-123456789abc"
SECOND_ONECLICK = ONECLICK.replace("abcdef12", "abcdef34")


def ashby_html(script=""):
    identity = "<script>window.__appData=" + json.dumps(IDENTITY) + ";</script>"
    return ASHBY_FORM.replace("<body>", "<body>" + identity).replace("</body>", f"<script>{script}</script></body>")


def smart_html(links="", script=""):
    return f"""<!doctype html><html><body><h1>ExampleCo Software Engineer</h1>
    {links}<form><label for="first">First name*</label><input id="first" required value="PRIVATE PREFILLED NAME">
    <label for="email">Email*</label><input id="email" type="email" value="private@example.test" required>
    <label for="location">Office preference</label><select id="location"><option value="internal-1">Boston</option><option value="internal-2">New York</option></select>
    <button type="button">Next</button><button type="submit">Submit application</button></form>
    <script>window.fixtureInputs=[];document.addEventListener('input',event=>window.fixtureInputs.push(event.target.value));{script}</script>
    </body></html>"""


@pytest.fixture
def browser_fixture(monkeypatch):
    """Exercise the production guard, replacing its continue_ with fulfill."""
    observed = {"allowed": [], "blocked": [], "closed": [], "contexts": [], "inputs": [],
                "sockets": [], "socket_routes": [], "pages": {}, "last_urls": [], "abort_initial": False,
                "context_close_mode": None, "context_close_cancelled": False}

    class MockRoute:
        def __init__(self, route):
            self.route, self.request = route, route.request

        async def abort(self, *args, **kwargs):
            observed["blocked"].append({"url": self.request.url, "method": self.request.method,
                                        "navigation": self.request.is_navigation_request()})
            return await self.route.abort(*args, **kwargs)

        async def continue_(self, **kwargs):
            # This fixture must never continue a real network request.
            request = self.request
            observed["allowed"].append({"url": request.url, "method": request.method,
                                        "navigation": request.is_navigation_request()})
            if request.is_navigation_request():
                if observed["abort_initial"]:
                    return await self.route.abort()
                html = observed["pages"].get(request.url)
                if html is None:
                    raise AssertionError(f"Unexpected navigation was allowed: {request.url}")
                html = html.replace("<body>", "<body><script>window.fixtureInputs=[];document.addEventListener('input',event=>window.fixtureInputs.push(event.target.value));</script>", 1)
                return await self.route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            return await self.route.fulfill(status=200, content_type="application/json", body="{}")

    class MockSocket:
        def __init__(self, socket):
            self.socket = socket

        async def close(self, **kwargs):
            observed["sockets"].append(self.socket.url)
            return await self.socket.close(**kwargs)

        def connect_to_server(self):
            raise AssertionError("The inspector must not connect a WebSocket to a server.")

    class Context:
        def __init__(self, context):
            self.context, self.page = context, None

        def __getattr__(self, name):
            return getattr(self.context, name)

        async def route(self, pattern, handler):
            async def dispatch(route):
                await handler(MockRoute(route))
            await self.context.route(pattern, dispatch)

        async def route_web_socket(self, pattern, handler):
            observed["socket_routes"].append(pattern)
            async def dispatch(socket):
                result = handler(MockSocket(socket))
                if inspect.isawaitable(result):
                    await result
            await self.context.route_web_socket(pattern, dispatch)

        async def new_page(self):
            self.page = await self.context.new_page()
            return self.page

        async def close(self):
            if self.page and not self.page.is_closed():
                observed["last_urls"].append(self.page.url)
                try:
                    observed["inputs"] += await self.page.evaluate("window.fixtureInputs || []")
                    observed["last_values"] = await self.page.locator("input:not([type=file])").evaluate_all("items=>items.map(item=>item.value)")
                    observed["file_count"] = await self.page.locator("input[type=file]").evaluate_all("items=>items.reduce((count,item)=>count+item.files.length,0)")
                except Exception:
                    pass
            observed["closed"].append("context")
            if observed["context_close_mode"] == "raise":
                raise RuntimeError("Synthetic context close failure")
            if observed["context_close_mode"] == "hang":
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    observed["context_close_cancelled"] = True
                    raise
            await self.context.close()

    class Browser:
        def __init__(self, browser):
            self.browser = browser

        async def new_context(self, **options):
            observed["contexts"].append(options)
            return Context(await self.browser.new_context(**options))

        async def close(self):
            observed["closed"].append("browser")
            await self.browser.close()

    @asynccontextmanager
    async def mocked_playwright():
        async with real_playwright() as playwright:
            async def launch(**options):
                assert options == {"headless": True}
                return Browser(await playwright.chromium.launch(**options))
            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    monkeypatch.setattr("playwright.async_api.async_playwright", mocked_playwright)
    return observed


@pytest.mark.parametrize("url,html,platform", [(ASHBY, ashby_html(), "ashby"), (ONECLICK, smart_html(), "smartrecruiters")])
def test_public_checks_remain_inspection_only_without_filling_or_leaking_prefill(browser_fixture, url, html, platform):
    browser_fixture["pages"][url] = html
    result = asyncio.run(inspect_public_platform({"apply_url": url, "profile": {"name": "DO NOT TYPE"}, "resume_path": "DO NOT UPLOAD"}))
    assert result["platform"] == platform and result["mode"] == "inspection_only", result
    assert result["automatic_submission_supported"] is False and result["field_count"] >= 2
    assert not browser_fixture["inputs"] and browser_fixture["file_count"] == 0
    assert browser_fixture["contexts"] == [{"accept_downloads": False, "service_workers": "block"}]
    assert browser_fixture["closed"] == ["context", "browser"]
    assert browser_fixture["socket_routes"] == ["**/*"]
    serialized = json.dumps(result)
    for private in ("PRIVATE PREFILLED NAME", "Synthetic Private Name", "private@example.test", "DO NOT TYPE", "DO NOT UPLOAD", "internal-1"):
        assert private not in serialized
    for field in result["fields"]:
        assert set(field) == {"label", "type", "required", "options"}
        assert all(set(option) == {"label", "disabled"} for option in field["options"])


@pytest.mark.parametrize("url,html", [(ASHBY, ashby_html()), (ONECLICK, smart_html())])
def test_initial_tracking_query_is_stripped_before_browser_navigation(browser_fixture, url, html):
    browser_fixture["pages"][url] = html
    result = asyncio.run(inspect_public_platform({"apply_url": url + "?token=DO_NOT_FORWARD&utm_source=tracker"}))
    assert result["field_count"] >= 2, result
    assert browser_fixture["allowed"] == [{"url": url, "method": "GET", "navigation": True}]
    assert "DO_NOT_FORWARD" not in json.dumps(result)


def test_read_only_guard_blocks_writes_private_resources_and_websockets(browser_fixture):
    script = """
    for(const method of ['POST','PUT','OPTIONS'])fetch('/api/collect?secret=DO_NOT_RETAIN',{method,body:method==='OPTIONS'?undefined:'PRIVATE BODY'}).catch(()=>{});
    for(const target of ['http://127.0.0.1:9999/private','https://127.0.0.1/private','https://10.0.0.1/private','https://localhost/private','https://service.local/private'])fetch(target).catch(()=>{});
    fetch('https://static.example.test/public-metadata').catch(()=>{});
    new WebSocket('wss://stream.example.test/live?secret=DO_NOT_RETAIN');
    """
    browser_fixture["pages"][ASHBY] = ashby_html(script)
    result = asyncio.run(inspect_public_platform({"apply_url": ASHBY}))
    assert {request["method"] for request in browser_fixture["blocked"]} >= {"POST", "PUT", "OPTIONS"}
    for target in ("127.0.0.1", "10.0.0.1", "localhost", "service.local"):
        assert any(target in request["url"] for request in browser_fixture["blocked"])
        assert not any(target in request["url"] for request in browser_fixture["allowed"])
    assert all(request["method"] in {"GET", "HEAD"} for request in browser_fixture["allowed"])
    assert browser_fixture["sockets"] == ["wss://stream.example.test/live?secret=DO_NOT_RETAIN"]
    assert any(gate["kind"] == "transport" for gate in result["gates"]), result
    assert "DO_NOT_RETAIN" not in json.dumps(result) and "PRIVATE BODY" not in json.dumps(result)
    assert browser_fixture["closed"] == ["context", "browser"]


@pytest.mark.parametrize("target", [
    ASHBY.replace("ExampleCo", "OtherCo"), ASHBY.replace("789abc", "789def"),
    "https://evil.example.test/another-job",
])
def test_ashby_navigation_is_pinned_to_exact_board_and_posting(browser_fixture, target):
    browser_fixture["pages"][ASHBY] = ashby_html(f"location.href={json.dumps(target)}")
    asyncio.run(inspect_public_platform({"apply_url": ASHBY}))
    assert not any(request["url"] == target for request in browser_fixture["allowed"])
    assert any(request["url"] == target and request["navigation"] for request in browser_fixture["blocked"])
    assert browser_fixture["closed"] == ["context", "browser"]


def test_smart_follows_one_visible_published_same_company_same_origin_oneclick_link(browser_fixture):
    browser_fixture["pages"][SMART] = smart_html(f'<a href="{ONECLICK}?utm_source=posting">I’m interested</a>')
    browser_fixture["pages"][ONECLICK] = smart_html()
    result = asyncio.run(inspect_public_platform({"apply_url": SMART}))
    assert result["mode"] == "inspection_only" and result["automatic_submission_supported"] is False
    assert [request["url"] for request in browser_fixture["allowed"] if request["navigation"]] == [SMART, ONECLICK]
    assert browser_fixture["last_urls"] == [ONECLICK]
    assert browser_fixture["closed"] == ["context", "browser"]


@pytest.mark.parametrize("links", [
    f'<a href="{ONECLICK}">Apply</a><a href="{SECOND_ONECLICK}">I’m interested</a>',
    f'<a href="{ONECLICK.replace("ExampleCo", "OtherCo")}">I’m interested</a>',
    f'<a href="{ONECLICK.replace("jobs.smartrecruiters.com", "www.smartrecruiters.com")}">I’m interested</a>',
    f'<a href="{ONECLICK}" style="display:none">I’m interested</a>',
    '<a href="https://evil.example.test/application">Apply</a>',
])
def test_smart_does_not_follow_conflicting_cross_company_cross_origin_or_hidden_links(browser_fixture, links):
    browser_fixture["pages"][SMART] = smart_html(links)
    result = asyncio.run(inspect_public_platform({"apply_url": SMART}))
    assert result["mode"] == "inspection_only"
    assert [request["url"] for request in browser_fixture["allowed"] if request["navigation"]] == [SMART]
    assert browser_fixture["closed"] == ["context", "browser"]


def test_smart_duplicate_links_to_same_publication_are_one_unique_target(browser_fixture):
    browser_fixture["pages"][SMART] = smart_html(f'<a href="{ONECLICK}">Apply</a><a href="{ONECLICK}">I’m interested</a>')
    browser_fixture["pages"][ONECLICK] = smart_html()
    result = asyncio.run(inspect_public_platform({"apply_url": SMART}))
    assert result["field_count"] >= 2
    assert [request["url"] for request in browser_fixture["allowed"] if request["navigation"]] == [SMART, ONECLICK]


def test_smart_application_cannot_navigate_to_a_different_publication(browser_fixture):
    browser_fixture["pages"][ONECLICK] = smart_html(script=f"location.href={json.dumps(SECOND_ONECLICK)}")
    asyncio.run(inspect_public_platform({"apply_url": ONECLICK}))
    assert not any(request["url"] == SECOND_ONECLICK for request in browser_fixture["allowed"])
    assert any(request["url"] == SECOND_ONECLICK for request in browser_fixture["blocked"])
    assert browser_fixture["closed"] == ["context", "browser"]


def test_browser_and_context_close_after_navigation_error(browser_fixture):
    browser_fixture["abort_initial"] = True
    report = asyncio.run(inspect_public_platform({"apply_url": ASHBY}))
    assert report["stage"] == "unavailable" and report["fields"] == []
    assert browser_fixture["closed"] == ["context", "browser"]


def test_context_close_exception_does_not_skip_browser_cleanup(browser_fixture):
    browser_fixture["pages"][ASHBY] = ashby_html()
    browser_fixture["context_close_mode"] = "raise"
    report = asyncio.run(inspect_public_platform({"apply_url": ASHBY}))
    assert report["field_count"] >= 2
    assert browser_fixture["closed"] == ["context", "browser"]


def test_hanging_context_close_is_cancelled_and_browser_cleanup_is_attempted(browser_fixture):
    browser_fixture["pages"][ASHBY] = ashby_html()
    browser_fixture["context_close_mode"] = "hang"
    started = time.monotonic()
    report = asyncio.run(inspect_public_platform({"apply_url": ASHBY}))
    elapsed = time.monotonic() - started
    assert report["field_count"] >= 2
    assert browser_fixture["context_close_cancelled"] is True
    assert browser_fixture["closed"] == ["context", "browser"]
    assert elapsed < 10, "Bounded cleanup must not await the synthetic one-hour close"


@pytest.mark.parametrize("url", ["https://evil.example.test/application", "https://jobs.ashbyhq.com/ExampleCo/not-a-posting", ASHBY + "#fragment"])
def test_unsupported_destination_does_not_start_browser(browser_fixture, url):
    result = asyncio.run(inspect_public_platform({"apply_url": url}))
    assert result["stage"] == "unsupported"
    assert not browser_fixture["allowed"] and not browser_fixture["contexts"]
