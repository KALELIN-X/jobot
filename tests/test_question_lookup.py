"""Fresh public-form inspection, with every browser request mocked locally."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright as real_playwright

from jobradar.question_lookup import inspect_questions
from test_greenhouse_forms import form_html


GREENHOUSE = "https://job-boards.greenhouse.io/example/jobs/1234567"
LEVER = "https://jobs.lever.co/example/12345678-abcd-1234-abcd-123456789abc/apply"


@pytest.fixture
def browser_fixture(monkeypatch):
    """Run real Chromium but fulfill all allowed public-origin traffic in memory."""
    observed = {"allowed": [], "closed": [], "contexts": [], "inputs": [], "html": form_html("search")}
    monkeypatch.setattr("jobradar.form_adapters.COMBOBOX_TIMEOUT_MS", 200)

    class MockRoute:
        def __init__(self, route):
            self.route, self.request = route, route.request

        async def abort(self, *args, **kwargs):
            return await self.route.abort(*args, **kwargs)

        async def continue_(self, **kwargs):
            request = self.request
            observed["allowed"].append({"url": request.url, "method": request.method})
            if request.is_navigation_request():
                html = observed["html"].replace("<script>", "<script>window.fixtureInputs=[];document.addEventListener('input',event=>window.fixtureInputs.push({id:event.target.id,value:event.target.value}));", 1)
                await self.route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await self.route.fulfill(status=200, content_type="application/json", body='[]')

    class Context:
        def __init__(self, context):
            self.context, self.page = context, None

        def __getattr__(self, name):
            return getattr(self.context, name)

        async def route(self, pattern, handler):
            async def dispatch(route):
                await handler(MockRoute(route))
            await self.context.route(pattern, dispatch)

        async def new_page(self):
            self.page = await self.context.new_page()
            return self.page

        async def close(self):
            if self.page and not self.page.is_closed():
                try:
                    observed["inputs"] += await self.page.evaluate("window.fixtureInputs || []")
                    observed["last_values"] = await self.page.locator("input:not([type=file])").evaluate_all("items=>items.map(item=>({id:item.id,value:item.value}))")
                    observed["selected"] = await self.page.locator(".select__single-value").all_text_contents()
                except Exception:
                    pass
            observed["closed"].append("context")
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


@pytest.mark.parametrize("url", [GREENHOUSE, LEVER])
def test_refresh_reads_real_choices_without_typing_profile_uploading_or_selecting(browser_fixture, url):
    result = asyncio.run(inspect_questions({"apply_url": url}, {"name": "DO NOT TYPE", "location": "DO NOT SEARCH", "answers": {"Country": "DO NOT SELECT"}}))
    assert result["status"] == "inspected", result
    country = next(field for field in result["fields"] if field["label"].startswith("Country"))
    city = next(field for field in result["fields"] if field["label"].startswith("Location"))
    assert {choice["label"] for choice in country["options"]} == {"Canada", "United States"}
    assert city["options"] == [] and city["lookup_required"] is True
    assert not browser_fixture["inputs"] and not any(browser_fixture["selected"])
    assert all(not item["value"] for item in browser_fixture["last_values"])
    assert browser_fixture["contexts"] == [{"accept_downloads": False, "service_workers": "block"}]
    assert browser_fixture["closed"] == ["context", "browser"]
    assert all(request["method"] in {"GET", "HEAD"} for request in browser_fixture["allowed"])
    assert "DO NOT" not in json.dumps(result)


def test_exact_question_query_reads_options_but_never_commits_choice_or_fills_other_fields(browser_fixture):
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question="Location (City)", query="Boston"))
    assert result["status"] == "inspected", result
    city = next(field for field in result["fields"] if field["label"].startswith("Location"))
    assert city["options"] == [{"label": "Boston, Massachusetts, United States", "value": "Boston, Massachusetts, United States", "disabled": False}]
    assert all("id" not in choice for choice in city["options"])
    assert browser_fixture["inputs"] == [{"id": "city", "value": "Boston"}, {"id": "city", "value": ""}]
    assert not any(browser_fixture["selected"])
    assert all(not field["value"] for field in browser_fixture["last_values"])


@pytest.mark.parametrize("choices", [
    {"supported": False, "options": [], "loading": True, "searchable": True},
    {"supported": False, "options": [], "loading": False, "searchable": True},
    {"supported": True, "options": [], "loading": True, "searchable": True},
])
def test_failed_target_menu_is_not_reported_as_successful_empty_choices(browser_fixture, monkeypatch, choices):
    async def failed_menu(*args, **kwargs):
        return choices
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", failed_menu)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question="Location (City)", query="Boston"))
    assert result["status"] == "needs_attention" and result["fields"] == []
    assert "could not finish loading or be read" in result["message"]
    assert browser_fixture["closed"] == ["context", "browser"]


def test_genuine_supported_empty_search_result_is_still_a_successful_inspection(browser_fixture, monkeypatch):
    async def no_matches(*args, **kwargs):
        return {"supported": True, "options": [], "loading": False, "searchable": True,
                "lookup_required": True, "no_results": True}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", no_matches)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question="Location (City)", query="Boston"))
    assert result["status"] == "inspected"
    city = next(field for field in result["fields"] if field["label"].startswith("Location"))
    assert city["options"] == [] and city["lookup_required"] is True
    assert not city.get("lookup_failed")


@pytest.mark.parametrize("question", [None, "Location (City)"])
def test_blank_refresh_preserves_a_real_initial_typeahead_hint_before_listbox_exists(browser_fixture, monkeypatch, question):
    async def initial_hint(*args, **kwargs):
        return {"supported": False, "options": [], "loading": False, "searchable": True,
                "lookup_required": True, "query_hint": True, "minimum_query_length": 3}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", initial_hint)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question=question))
    assert result["status"] == "inspected"
    city = next(field for field in result["fields"] if field["label"].startswith("Location"))
    assert city["options"] == [] and city["searchable"] is city["lookup_required"] is True
    assert city["minimum_query_length"] == 3 and city["unsupported_choices"] is False


def test_query_cannot_treat_an_unresolved_initial_hint_as_completed_empty_search(browser_fixture, monkeypatch):
    async def initial_hint(*args, **kwargs):
        return {"supported": False, "options": [], "loading": False, "searchable": True,
                "lookup_required": True, "query_hint": True}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", initial_hint)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question="Location (City)", query="Boston"))
    assert result["status"] == "needs_attention" and result["fields"] == []


def test_failed_required_menu_stops_whole_refresh_instead_of_replacing_cached_questions(browser_fixture, monkeypatch):
    async def failed_menu(*args, **kwargs):
        return {"supported": False, "options": [], "loading": False}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", failed_menu)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert result["status"] == "needs_attention" and result["fields"] == []


@pytest.mark.parametrize("metadata_source", ["snapshot", "choices"])
def test_optional_unsupported_multiselect_does_not_block_other_question_refreshes(browser_fixture, monkeypatch, metadata_source):
    script = "document.querySelector('label[for=city]').textContent='Optional topics';document.getElementById('city').setAttribute('aria-required','false');"
    if metadata_source == "snapshot":
        script += "document.getElementById('city').setAttribute('aria-multiselectable','true');"
    browser_fixture["html"] = browser_fixture["html"].replace("const variant = 'search';", "const variant = 'search';" + script)
    async def menu(_page, field, **kwargs):
        if field["label"] == "Optional topics":
            return {"supported": False, "options": [], "loading": False,
                    **({"multiple": True} if metadata_source == "choices" else {})}
        return {"supported": True, "options": [{"label": "Existing choice", "value": "existing", "disabled": False}]}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", menu)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert result["status"] == "inspected"
    optional = next(field for field in result["fields"] if field["label"] == "Optional topics")
    assert optional["multiple"] is True and not optional["required"]
    assert optional["unsupported_choices"] is True
    assert optional["options"] == []


def test_unreadable_optional_single_choice_also_preserves_prior_refresh_state(browser_fixture, monkeypatch):
    browser_fixture["html"] = browser_fixture["html"].replace("const variant = 'search';", "const variant = 'search';document.querySelector('label[for=city]').textContent='Optional topics';document.getElementById('city').setAttribute('aria-required','false');")
    async def menu(_page, field, **kwargs):
        if field["label"] == "Optional topics":
            return {"supported": False, "options": [], "loading": False}
        return {"supported": True, "options": [{"label": "Existing choice", "value": "existing", "disabled": False}]}
    monkeypatch.setattr("jobradar.question_lookup.read_combobox", menu)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert result["status"] == "needs_attention" and result["fields"] == []


def test_post_put_options_and_upload_attempts_are_blocked_even_for_the_approved_origin(browser_fixture):
    script = """
    for(const method of ['POST','PUT','OPTIONS'])fetch('/collect?private=DO_NOT_RETAIN',{method,body:method==='OPTIONS'?undefined:'DO_NOT_RETAIN'}).catch(()=>{});
    """
    browser_fixture["html"] = browser_fixture["html"].replace("const variant = 'search';", "const variant = 'search';" + script)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert result["status"] == "inspected"
    assert {request["method"] for request in result["blocked_requests"]} == {"POST", "PUT", "OPTIONS"}
    assert all(request["category"] == "read_only_method" for request in result["blocked_requests"])
    assert all(request["method"] in {"GET", "HEAD"} for request in browser_fixture["allowed"])
    assert "DO_NOT_RETAIN" not in json.dumps(result)


def test_form_navigation_to_another_posting_or_host_is_blocked(browser_fixture):
    browser_fixture["html"] = browser_fixture["html"].replace("const variant = 'search';", "const variant = 'search';setTimeout(()=>location.href='https://evil.example.test/other-job',10);")
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert not any(request["url"].startswith("https://evil") for request in browser_fixture["allowed"])
    assert any(request["category"] == "posting_navigation" for request in result["blocked_requests"])
    assert browser_fixture["closed"] == ["context", "browser"]


def test_public_form_cannot_fetch_local_machine_resources(browser_fixture):
    browser_fixture["html"] = browser_fixture["html"].replace("const variant = 'search';", "const variant = 'search';fetch('http://127.0.0.1:9999/private?secret=NOT_RETAINED').catch(()=>{});")
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert any(request["category"] == "non_public_resource" for request in result["blocked_requests"])
    assert not any("127.0.0.1" in request["url"] for request in browser_fixture["allowed"])
    assert "NOT_RETAINED" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["captcha", "password", "duplicate", "absent"])
def test_target_question_must_be_unique_visible_and_not_behind_verification(browser_fixture, kind):
    if kind == "captcha":
        browser_fixture["html"] = browser_fixture["html"].replace("<h2>Apply for this job</h2>", '<h2>Apply for this job</h2><div class="g-recaptcha" data-sitekey="test"></div>')
    elif kind == "password":
        browser_fixture["html"] = browser_fixture["html"].replace("<h2>Apply for this job</h2>", '<h2>Apply for this job</h2><input type="password">')
    elif kind == "duplicate":
        browser_fixture["html"] = browser_fixture["html"].replace("<h2>Apply for this job</h2>", '<h2>Apply for this job</h2><label>Location (City)<input role="combobox"></label>')
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, question="Absent question" if kind == "absent" else "Location (City)", query="Boston"))
    assert result["status"] == "needs_attention" and not result["fields"]
    assert not browser_fixture["inputs"]


@pytest.mark.parametrize("url", ["https://evil.example.test/example/jobs/1234", "http://boards.greenhouse.io/example/jobs/1234",
                                 GREENHOUSE + "?arbitrary=query", GREENHOUSE + "#fragment", GREENHOUSE.replace("example/jobs", "other/example/jobs"),
                                 LEVER.removesuffix("/apply"), "http://127.0.0.1:8765/mock/apply"])
def test_unsupported_initial_urls_are_rejected_before_browser_start(monkeypatch, url):
    called = []
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: called.append(True))
    result = asyncio.run(inspect_questions({"apply_url": url}))
    assert result["status"] == "needs_attention" and not called


@pytest.mark.parametrize("arguments", [{"query": "Boston"}, {"question": ""}, {"question": "City", "query": ""},
                                      {"question": "City", "query": "x" * 241}, {"question": "City", "query": "line\nbreak"}])
def test_invalid_lookup_scope_does_not_start_browser(monkeypatch, arguments):
    called = []
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: called.append(True))
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}, **arguments))
    assert result["status"] == "needs_attention" and not called


def test_timeout_closes_the_fresh_context_and_browser(monkeypatch):
    closed = []

    class Page:
        url = GREENHOUSE
        def set_default_timeout(self, value):
            pass
        async def goto(self, *args, **kwargs):
            await asyncio.sleep(5)

    class Context:
        async def new_page(self):
            return Page()
        async def route(self, *args):
            pass
        async def close(self):
            closed.append("context")

    class Browser:
        async def new_context(self, **kwargs):
            return Context()
        async def close(self):
            closed.append("browser")

    @asynccontextmanager
    async def mocked_playwright():
        async def launch(**kwargs):
            return Browser()
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    monkeypatch.setattr("playwright.async_api.async_playwright", mocked_playwright)
    monkeypatch.setattr("jobradar.question_lookup.LOOKUP_TIMEOUT_SECONDS", 0.02)
    result = asyncio.run(inspect_questions({"apply_url": GREENHOUSE}))
    assert result["status"] == "needs_attention" and "time limit" in result["message"]
    assert closed == ["context", "browser"]
