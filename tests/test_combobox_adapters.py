"""Real Chromium dropdown fixtures use only in-memory HTML and no submissions."""
import asyncio
import json

import pytest
from playwright.async_api import async_playwright

from jobradar.form_adapters import SNAPSHOT_JS, choose_combobox, enrich_comboboxes, read_combobox


TARGET = "Boston, Massachusetts, United States"


def html(*, mode="normal", defaults=False, delay=300, unassociated=False, uncommitted=False, commit_delay=0, closes_during_search=False):
    config = {"mode": mode, "defaults": defaults, "delay": delay, "unassociated": unassociated,
              "uncommitted": uncommitted, "commit_delay": commit_delay, "target": TARGET,
              "closes_during_search": closes_during_search}
    return r'''<!doctype html><html><head><style>
      body{font:16px sans-serif;padding:20px}.select__container{padding:12px;border:1px solid #555}
      .select__single-value{min-height:24px}[role=listbox]{padding:12px;background:#eee}
      [role=option]{padding:8px;cursor:pointer}input,select{padding:8px}
      </style></head><body><form onsubmit="event.preventDefault();window.submissions++">
      <label for="native">Office</label><select id="native"><option value="">Choose</option>
      <optgroup disabled label="Unavailable"><option value="closed">Closed office</option></optgroup>
      <option value="open">Open office</option></select>
      <label for="city">Location (City)</label><div class="select__container">
      <div class="select__single-value"></div><input id="city" role="combobox" type="text"
        aria-autocomplete="list" aria-expanded="false" aria-required="true" aria-describedby="hint"
        aria-controls="not-mounted-yet"><p id="hint">Type at least 3 characters to search.</p></div>
      <button type="submit">Submit application</button></form>
      <div role="listbox" id="unrelated"><div role="option">Boston, Massachusetts, United States</div></div>
      <script>
      const config=CONFIG, input=document.getElementById('city');
      const selected=document.querySelector('.select__single-value');
      window.queries=[];window.submissions=0;window.reopenings=0;const mounted=Date.now();let menu=null,version=0,queryVersion=0,cached=null;
      function render(labels, message='') {
        menu?.remove();menu=document.createElement('div');menu.setAttribute('role','listbox');
        menu.id='city-results-'+(++version);
        input.setAttribute('aria-controls',config.unassociated?'missing-owned-listbox':menu.id);
        if(message)menu.appendChild(document.createTextNode(message));
        labels.forEach((item,index)=>{
          const label=typeof item==='string'?item:item.label;
          const option=document.createElement('div');option.setAttribute('role','option');option.textContent=label;
          if(item.disabled)option.setAttribute('aria-disabled','true');
          option.addEventListener('click',()=>{
            if(item.disabled)return;
            if(!config.uncommitted)setTimeout(()=>selected.textContent=label,config.commit_delay);
            input.value='';input.setAttribute('aria-expanded','false');menu?.remove();menu=null;
          });menu.appendChild(option);
        });
        // The target list is deliberately portaled beside another open listbox.
        document.body.appendChild(menu);input.setAttribute('aria-expanded','true');
      }
      input.addEventListener('click',()=>{
        if(config.mode==='blank-hydration')return;
        if(config.mode==='stale-loading'){input.setAttribute('aria-busy','true');render([config.target]);}
        else render(config.defaults?['New York, New York, United States']:[],config.defaults?'':'Type at least 3 characters to search.');
      });
      input.addEventListener('input',()=>{
        const query=input.value,current=++queryVersion;window.queries.push(query);
        if(query.length<3){input.setAttribute('aria-busy','false');render([],'Type at least 3 characters to search.');return;}
        input.setAttribute('aria-busy','true');cached=null;
        if(config.closes_during_search){menu?.remove();menu=null;input.setAttribute('aria-expanded','false');}
        else render([],'Loading…');
        setTimeout(()=>{
          if(current!==queryVersion)return;
          input.setAttribute('aria-busy','false');
          const labels=query===config.target?config.mode==='duplicate'?[config.target,config.target]:
            config.mode==='disabled'?[{label:config.target,disabled:true}]:
            config.mode==='disabled-duplicate'?[{label:config.target,disabled:true},config.target]:
            config.mode==='similar-first'?['Boston District, England, United Kingdom',config.target]:[config.target]:[];
          cached=labels;
          if(config.closes_during_search){menu?.remove();menu=null;input.setAttribute('aria-expanded','false');}
          else render(labels,labels.length?'':'No results');
        },config.delay);
      });
      input.addEventListener('keydown',event=>{
        if(event.key==='Escape'){menu?.remove();menu=null;input.setAttribute('aria-expanded','false')}
        if(event.key==='ArrowDown'&&config.mode==='blank-hydration'&&Date.now()-mounted>=config.delay){window.reopenings++;render([config.target])}
        if(event.key==='ArrowDown'&&config.closes_during_search){window.reopenings++;render(cached||[],cached?(cached.length?'':'No results'):'Loading…')}
      });
      </script></body></html>'''.replace("CONFIG", json.dumps(config))


async def in_browser(markup, check):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            # Fixtures must remain isolated even if their markup is changed.
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(markup)
            snapshot = await page.evaluate(SNAPSHOT_JS, "fixture")
            field = next(item for item in snapshot["fields"] if item["name"] == "city")
            await check(page, snapshot, field)
            assert await page.evaluate("window.submissions") == 0
        finally:
            await browser.close()


def test_async_portal_results_replace_unrelated_defaults_and_commit_the_exact_label():
    async def check(page, snapshot, field):
        result = await read_combobox(page, field, query=TARGET)
        assert result["supported"] is True
        assert [option["label"] for option in result["options"]] == [TARGET]
        assert result["searchable"] is True and result["lookup_required"] is False
        assert await page.locator("#city").input_value() == ""
        assert await page.locator(".select__single-value").inner_text() == ""
        await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == TARGET
        assert (await page.evaluate("window.queries"))[0] == TARGET
    asyncio.run(in_browser(html(defaults=True, delay=450, commit_delay=150), check))


def test_empty_search_lookup_exposes_only_observed_query_metadata():
    async def check(page, snapshot, field):
        assert field["searchable"] is True
        assert field["minimum_query_length"] == 3
        enriched = await enrich_comboboxes(page, snapshot)
        city = next(item for item in enriched["fields"] if item["name"] == "city")
        assert city["lookup_required"] is True
        assert city["minimum_query_length"] == 3
        assert city["options"] == []
        assert await page.evaluate("window.queries") == []
        result = await read_combobox(page, field, query="Bo")
        assert result["options"] == []
        assert await page.evaluate("window.queries") == []
    asyncio.run(in_browser(html(), check))


def test_query_menu_is_reopened_after_async_loading_without_choosing_the_first_city():
    async def check(page, snapshot, field):
        await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == TARGET
        assert await page.evaluate("window.reopenings") >= 2
    asyncio.run(in_browser(html(mode="similar-first", delay=350, closes_during_search=True), check))


def test_no_async_results_never_fall_back_to_an_unrelated_visible_option():
    async def check(page, snapshot, field):
        result = await read_combobox(page, field, query="Nowhere, Example")
        assert result["supported"] is True
        assert result["lookup_required"] is True
        assert result["options"] == []
        with pytest.raises(ValueError, match="no longer available"):
            await choose_combobox(page, field["id"], "Nowhere, Example")
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(html(delay=100), check))


@pytest.mark.parametrize("mode", ["duplicate", "disabled"])
def test_ambiguous_or_disabled_exact_results_are_never_selected(mode):
    async def check(page, snapshot, field):
        with pytest.raises(ValueError, match="no longer available"):
            await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(html(mode=mode, delay=100), check))


def test_a_disabled_duplicate_does_not_hide_the_one_enabled_exact_option():
    async def check(page, snapshot, field):
        await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == TARGET
    asyncio.run(in_browser(html(mode="disabled-duplicate", delay=100), check))


def test_a_declared_missing_listbox_never_uses_another_open_dropdown():
    async def check(page, snapshot, field):
        result = await read_combobox(page, field, query=TARGET)
        assert result["supported"] is False and result["options"] == []
        with pytest.raises(ValueError, match="no longer available"):
            await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(html(delay=100, unassociated=True), check))


def test_clicking_an_option_without_committing_selection_is_a_failure():
    async def check(page, snapshot, field):
        with pytest.raises(ValueError, match="did not commit"):
            await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(html(delay=100, uncommitted=True), check))


def test_pending_loading_never_turns_stale_visible_options_into_a_committed_choice(monkeypatch):
    monkeypatch.setattr("jobradar.form_adapters.COMBOBOX_TIMEOUT_MS", 350)
    async def check(page, snapshot, field):
        result = await read_combobox(page, field, query=TARGET)
        assert result["supported"] is False and result["options"] == []
        with pytest.raises(ValueError, match="no longer available"):
            await choose_combobox(page, field["id"], TARGET)
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(html(mode="stale-loading"), check))


def test_native_disabled_groups_and_nonsearchable_comboboxes_are_preserved():
    async def check(page, snapshot, field):
        native = next(item for item in snapshot["fields"] if item["name"] == "native")
        assert native["searchable"] is False and native["lookup_required"] is False
        assert next(option for option in native["options"] if option["value"] == "closed")["disabled"] is True
        await page.locator("#native").select_option("open")
        for attribute, value in [("readonly", ""), ("aria-multiselectable", "true")]:
            await page.locator("#city").evaluate("(el, attribute) => el.setAttribute(attribute[0], attribute[1])", [attribute, value])
            current = await page.evaluate(SNAPSHOT_JS, "changed")
            city = next(item for item in current["fields"] if item["name"] == "city")
            assert city["searchable"] is False
            await page.locator("#city").evaluate("(el, name) => el.removeAttribute(name)", attribute)
    asyncio.run(in_browser(html(), check))


@pytest.mark.parametrize("required", [False, True])
def test_closed_react_multiselect_state_is_preserved_in_snapshot_read_and_enrichment(required):
    # CLEAR's blank public form puts the state on this value-container class,
    # while the input itself has no aria-multiselectable attribute.
    markup = html().replace('class="select__container"', 'class="select__value-container select__value-container--is-multi"')
    markup = markup.replace('aria-required="true"', 'aria-required="true"' if required else 'aria-required="false"')
    async def check(page, snapshot, field):
        assert field["multiple"] is True and field["required"] is required
        assert field["searchable"] is False and field["lookup_required"] is False
        result = await read_combobox(page, field, query=TARGET)
        assert result["multiple"] is True and result["supported"] is False
        assert result["searchable"] is False and result["lookup_required"] is False
        enriched = await enrich_comboboxes(page, snapshot)
        city = next(item for item in enriched["fields"] if item["name"] == "city")
        assert city["multiple"] is True and city["unsupported_choices"] is True
        assert city["searchable"] is False
        assert await page.evaluate("window.queries") == []
    asyncio.run(in_browser(markup, check))


def test_owned_portaled_listbox_can_reveal_multiselect_after_the_snapshot():
    markup = html().replace("menu.id='city-results-'+(++version);", "menu.id='city-results-'+(++version);menu.setAttribute('aria-multiselectable','true');")
    async def check(page, snapshot, field):
        assert field["multiple"] is False
        result = await read_combobox(page, field, query=TARGET)
        assert result["multiple"] is True and result["supported"] is False
        assert result["searchable"] is False and result["options"] == []
        enriched = await enrich_comboboxes(page, snapshot)
        assert next(item for item in enriched["fields"] if item["name"] == "city")["multiple"] is True
        assert await page.evaluate("window.queries") == []
    asyncio.run(in_browser(markup, check))


def test_unrelated_multiselect_and_mark_all_wording_do_not_override_single_choice_dom():
    markup = html(delay=100).replace('id="unrelated"', 'id="unrelated" aria-multiselectable="true"')
    markup = markup.replace('Location (City)</label>', 'Location (mark all that apply)</label>')
    async def check(page, snapshot, field):
        assert field["multiple"] is False and field["searchable"] is True
        result = await read_combobox(page, field, query=TARGET)
        assert result["multiple"] is False and result["supported"] is True
        assert [option["label"] for option in result["options"]] == [TARGET]
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(markup, check))


def test_blank_single_choice_menu_reopens_after_hydration_without_typing_or_selecting():
    markup = html(mode="blank-hydration", delay=600).replace("Type at least 3 characters to search.", "Choose an option.")
    async def check(page, snapshot, field):
        result = await read_combobox(page, field)
        assert result["supported"] is True and result["multiple"] is False
        assert [option["label"] for option in result["options"]] == [TARGET]
        assert await page.evaluate("window.reopenings") >= 1
        assert await page.evaluate("window.queries") == []
        assert await page.locator(".select__single-value").inner_text() == ""
    asyncio.run(in_browser(markup, check))
