const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function dashboardHelpers(rules = {}) {
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  const start = source.indexOf("  function experienceLimit(");
  const end = source.indexOf("  function renderJobs(", start);
  assert.ok(start >= 0 && end > start, "dashboard experience helpers are present");
  const context = {state: {rules}};
  vm.createContext(context);
  // Execute the production pure functions without a DOM, network, or app state.
  vm.runInContext(source.slice(start, end) + "\nthis.helpers = {experienceLimit, experienceMatches, experienceVisible, experienceLabel, newGradPriority, compareJobs};", context);
  return context.helpers;
}

function job(minimum, isNewGrad = false, score = 80) {
  return {score, score_detail: {
    experience_requirement: {minimum_months: minimum, status: minimum == null ? "unknown" : "known"},
    is_new_grad: isNewGrad,
    eligibility: [{label: "Work location", status: "unknown"}],
  }};
}

test("known minimum experience must be within the selected cap", () => {
  const helpers = dashboardHelpers({max_required_experience_months: 12, prefer_new_grad: true});
  assert.equal(helpers.experienceVisible(job(0), 12), true);
  assert.equal(helpers.experienceVisible(job(12), 12), true);
  assert.equal(helpers.experienceVisible(job(24), 12), false);
  assert.equal(helpers.experienceVisible(job(24, true), 12), false);
});

test("explicit new-grad unknowns remain discoverable without becoming a numeric match", () => {
  const helpers = dashboardHelpers({max_required_experience_months: 12, prefer_new_grad: true});
  const posting = job(null, true);
  assert.equal(helpers.experienceVisible(posting, 12), true);
  assert.equal(helpers.experienceMatches(posting, 12), false);
  assert.equal(posting.score_detail.experience_requirement.minimum_months, null);
  assert.equal(posting.score_detail.experience_requirement.status, "unknown");
});

test("other unknowns stay hidden and turning off new-grad preference removes the exception", () => {
  const preferred = dashboardHelpers({max_required_experience_months: 12, prefer_new_grad: true});
  assert.equal(preferred.experienceVisible(job(null), 12), false);
  const ordinary = dashboardHelpers({max_required_experience_months: 12, prefer_new_grad: false});
  assert.equal(ordinary.experienceVisible(job(null, true), 12), false);
});

test("unknown new-grad requirements are labelled for review", () => {
  const helpers = dashboardHelpers();
  assert.equal(helpers.experienceLabel({minimum_months: null}, true), "Experience: unknown · review needed");
  assert.equal(helpers.experienceLabel({minimum_months: null}), "Experience: unknown");
  assert.equal(helpers.experienceLabel({minimum_months: 12, maximum_months: 24}, true), "Experience: 1–2 yr");
});

test("new-grad review ordering does not change fit scores or erase unknown eligibility", () => {
  const helpers = dashboardHelpers({max_required_experience_months: 12, prefer_new_grad: true});
  const newGrad = job(null, true, 55);
  const ordinary = job(0, false, 100);
  assert.equal(helpers.newGradPriority(newGrad), true);
  assert.ok(helpers.compareJobs(newGrad, ordinary) < 0);
  assert.equal(newGrad.score, 55);
  assert.equal(newGrad.score_detail.eligibility[0].status, "unknown");
});

function queueHelpers() {
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  const start = source.indexOf("  function companyScope(");
  const end = source.indexOf("  function renderQueueStatus(", start);
  assert.ok(start >= 0 && end > start);
  const context = {list: value => Array.isArray(value) ? value : []};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + "\nthis.helpers = {queueControlState, setupDestination, companyScope, locationMode, exactLines, bulkQueueControl, attemptBudgetAvailable, dailyLimitValue};", context);
  return context.helpers;
}

test("runnable roles sort before held higher-scoring listings", () => {
  const helpers = dashboardHelpers({prefer_new_grad:true});
  const ready = {...job(0,false,60),submission_check:{eligible:true}};
  const held = {...job(null,true,100),submission_check:{eligible:false}};
  assert.ok(helpers.compareJobs(ready,held) < 0);
  assert.equal(held.score,100);
});

test("selected queue and questions stay visible before unrelated discoveries", () => {
  const helpers = dashboardHelpers();
  const queued = {...job(0,false,60),status:"queued"};
  const discovery = {...job(0,true,100),status:"new",submission_check:{eligible:true}};
  const waiting = {...job(0,false,60),unanswered_question_count:2};
  assert.ok(helpers.compareJobs(queued,discovery) < 0);
  assert.ok(helpers.compareJobs(waiting,discovery) < 0);
});

test("progress distinguishes delayed or disconnected status from live work", () => {
  const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
  const start = source.indexOf("  function progressPresentation(");
  const end = source.indexOf("  function renderProgress(",start);
  const context = {};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.present = progressPresentation;",context);
  const running = {status:"running",worker_alive:true,active_job:{company:"Example"}};
  assert.equal(context.present(running).label,"Working");
  assert.equal(context.present({...running,worker_alive:false}).label,"Worker update delayed");
  assert.equal(context.present(running,true).label,"Connection lost");
  assert.match(context.present({...running,status:"completed"}).title,/Last application/);
  assert.equal(context.present({...running,status:"completed"}).active,false);
});

for (const phase of ["headers","body"]) test(`hung status ${phase} cannot leave stale work displayed indefinitely`,async () => {
  const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
  const start = source.indexOf("  async function api(");
  const end = source.indexOf("  async function post(",start);
  let expire;
  const context = {AbortController,FormData,
    setTimeout(callback,delay) {assert.equal(delay,6000);expire=callback;return 1;},clearTimeout(){},
    fetch(_url,options) {
      const hung = () => options.signal.aborted ? Promise.reject(new Error("aborted")) : new Promise((_resolve,reject) => options.signal.addEventListener("abort",() => reject(new Error("aborted"))));
      return phase === "headers" ? hung() : Promise.resolve({ok:true,json:hung});
    }};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.request = api;",context);
  const request = context.request("/api/state");
  await Promise.resolve();
  expire();
  await assert.rejects(request,/fresh status|stopped responding/);
});

test("history URL validation accepts exact Greenhouse and Lever routes only", () => {
  const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
  const start = source.indexOf("  function supportedPostingURL(");
  const end = source.indexOf("  function externalLink(",start);
  const context = {URL};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.validate = supportedPostingURL;",context);
  assert.ok(context.validate("https://job-boards.greenhouse.io/example/jobs/1234"));
  assert.ok(context.validate("https://boards.eu.greenhouse.io/example/jobs/1234?gh_src=test"));
  assert.ok(context.validate("https://jobs.lever.co/example/11111111-1111-4111-8111-111111111111/apply"));
  for (const invalid of ["https://evil.test/example/jobs/1234","https://job-boards.greenhouse.io/example/jobs/nope","https://user:pass@boards.greenhouse.io/example/jobs/1234","http://boards.greenhouse.io/example/jobs/1234"]) assert.equal(context.validate(invalid),null);
});

test("unapproved and blocked queues have an actionable review path", () => {
  const {queueControlState} = queueHelpers();
  const setup = queueControlState({approval: {approved: false}, queue: {ready: 0, can_run: false}});
  assert.equal(setup.label, "Review & start queue");
  assert.equal(setup.needsReview, true);
  assert.equal(setup.canRun, false);
  const blocked = queueControlState({approval: {approved: true}, queue: {ready: 0, can_run: false}});
  assert.equal(blocked.label, "Review queue blockers");
  assert.equal(blocked.needsReview, true);
});

test("one-time runs work without enabling scheduling; dirty forms or busy workers cannot run", () => {
  const {queueControlState} = queueHelpers();
  const snapshot = {approval: {approved: true}, queue: {ready: 1, can_run: true}, automation: {enabled: false}};
  assert.equal(queueControlState(snapshot).canRun, true);
  assert.equal(snapshot.automation.enabled, false);
  assert.equal(queueControlState(snapshot, true).canRun, false);
  assert.equal(queueControlState({...snapshot, automation: {running: true}}).canRun, false);
});

test("any-company setup skips employer names and boards; a selected list remains optional", () => {
  const {setupDestination} = queueHelpers();
  const snapshot = {profile: {name: "Alex Example", email: "alex@example.test"}, resume: {loaded: true}, rules: {company_scope:"any",companies: [], allowed_ats: ["lever"]}};
  assert.equal(setupDestination(snapshot).selector, "#approval-acknowledge");
  snapshot.rules.company_scope = "selected";
  assert.match(setupDestination(snapshot).selector, /companies/);
  snapshot.rules.companies = ["Example"];
  assert.equal(setupDestination(snapshot).selector, "#approval-acknowledge");
  snapshot.rules.allowed_ats = [];
  assert.match(setupDestination(snapshot).selector, /allowed_ats/);
  snapshot.resume.loaded = false;
  assert.equal(setupDestination(snapshot).selector, "#resume-file");
});

test("simple company and location controls preserve existing preferences", () => {
  const {companyScope,locationMode} = queueHelpers();
  assert.equal(companyScope({}), "any");
  assert.equal(companyScope({companies:["Example"]}), "selected");
  assert.equal(companyScope({company_scope:"any",companies:["Example"]}), "any");
  assert.equal(locationMode(["United States","USA","U.S.","US"]), "us");
  assert.equal(locationMode(["Remote"]), "remote");
  assert.equal(locationMode(["New York","Chicago"]), "custom");
  assert.equal(locationMode([]), "any");
});

test("saving exact location and employer lists does not split names containing commas", () => {
  const {exactLines} = queueHelpers();
  assert.deepEqual(Array.from(exactLines("San Francisco, CA\nChicago\r\nSan Francisco, CA")), ["San Francisco, CA","Chicago"]);
  assert.deepEqual(Array.from(exactLines("Widgets, Inc.\nAnother Company")), ["Widgets, Inc.","Another Company"]);
});

function postingHelpers() {
  const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
  const start = source.indexOf("  function queueCardAction(");
  const end = source.indexOf("  function jobCard(", start);
  const context = {
    isDemo: posting => posting.demo === true,
    experienceLabel: dashboardHelpers().experienceLabel,
    escape: value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char])),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end) + "\nthis.helpers={queueCardAction,jobFactsMarkup};",context);
  return context.helpers;
}

test("manual queue controls rely on availability instead of a fit or eligibility gate", () => {
  const {queueCardAction} = postingHelpers();
  const held = {score:10,status:"needs_attention",submission_check:{eligible:false},queue_actions:{can_add:true}};
  assert.equal(queueCardAction(held).kind,"add");
  assert.equal(queueCardAction({...held,status:"queued",queue_actions:{can_remove:true}}).kind,"remove");
  const prior = queueCardAction({...held,status:"submitted",queue_actions:{can_add:false,reason:"Already recorded as applied."}});
  assert.equal(prior.kind,"unavailable");
  assert.equal(prior.reason,"Already recorded as applied.");
  assert.match(queueCardAction({demo:true}).reason,/demo/);
});

test("compact posting facts preserve stated pay units, expose missing facts, and escape source text", () => {
  const {jobFactsMarkup} = postingHelpers();
  const result = jobFactsMarkup({location:"Boston, MA",facts:{
    pay:{summary:"USD $45–$55 / hour",status:"stated"},
    degree:{summary:'Bachelor’s or equivalent <script>alert("x")</script>',status:"stated"},
    graduation:{summary:"2025–2026",status:"stated"},
  }});
  assert.match(result,/USD \$45–\$55 \/ hour/);
  assert.match(result,/Graduation window/);
  assert.match(result,/2025–2026/);
  assert.match(result,/Boston, MA/);
  assert.match(result,/Experience/);
  assert.match(result,/unknown/);
  assert.doesNotMatch(result,/<script>/);
  assert.match(result,/&lt;script&gt;/);
  assert.match(jobFactsMarkup({}),/Not listed/);
});

test("queued jobs stay visible when the optional experience view filter is enabled", () => {
  const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
  const start = source.indexOf("  function renderJobs(");
  const end = source.indexOf("  function queueCardAction(",start);
  const elements = new Map();
  const context = {
    state:{rules:{max_required_experience_months:12},jobs:[
      {id:"selected-unknown",status:"queued",score_detail:{}},
      {id:"discovered-unknown",status:"new",score_detail:{}},
      {id:"discovered-fit",status:"new",score_detail:{experience_requirement:{minimum_months:0}}},
    ]},
    lastJobSignature:"",filter:"queued",search:"",experienceFilterEnabled:true,
    $: selector => {if (!elements.has(selector)) elements.set(selector,{setAttribute(){}});return elements.get(selector);},
    list: value => Array.isArray(value) ? value : [],
    formatNumber: String,
    ...dashboardHelpers({max_required_experience_months:12}),
    jobCard: posting => `<article>${posting.id}</article>`,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nrenderJobs();",context);
  assert.equal(elements.get("#job-list").innerHTML,"<article>selected-unknown</article>");
  context.filter="all";
  context.experienceFilterEnabled=false;
  vm.runInContext("renderJobs(true);",context);
  assert.match(elements.get("#job-list").innerHTML,/discovered-unknown/);
});

test("bulk loading uses saved rules and remains clickable with unsaved edits or an empty shortlist", () => {
  const {bulkQueueControl} = queueHelpers();
  const snapshot = {rules:{min_score:60},queue:{loadable:27},automation:{running:false}};
  assert.equal(bulkQueueControl(snapshot,true,false,true).disabled,false);
  assert.match(bulkQueueControl(snapshot,true,false,true).help,/SAVED score cutoff of 60/);
  assert.equal(bulkQueueControl({...snapshot,queue:{loadable:0},automation:{running:true}},true).disabled,false);
  assert.equal(bulkQueueControl(snapshot,false).disabled,true);
  assert.equal(bulkQueueControl(snapshot,true,true).disabled,true);
  assert.match(bulkQueueControl(snapshot,true,true).label,/Loading/);
});

test("unlimited daily capacity permits a run while an exhausted optional cap still blocks it", () => {
  const {attemptBudgetAvailable,dailyLimitValue} = queueHelpers();
  assert.equal(attemptBudgetAvailable({remaining_today:null}),true);
  assert.equal(attemptBudgetAvailable({remaining_today:0}),false);
  assert.equal(attemptBudgetAvailable({remaining_today:3}),true);
  assert.equal(attemptBudgetAvailable({}),false);
  const form = {elements:{daily_limit_enabled:{checked:false},daily_limit:{value:"5"}}};
  assert.equal(dailyLimitValue(form),null);
  form.elements.daily_limit_enabled.checked=true;
  form.elements.daily_limit.value="50";
  assert.equal(dailyLimitValue(form),50);
});
