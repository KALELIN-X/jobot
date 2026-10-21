const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
const index = fs.readFileSync(path.join(__dirname,"..","static","index.html"),"utf8");
const escape = value => String(value ?? "").replace(/[&<>"']/g,char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));

function helpers(overrides = {}) {
  const start = source.indexOf("  function platformMode(");
  const end = source.indexOf("  function jobCard(",start);
  assert.ok(start >= 0 && end > start);
  const context = {
    list:value => Array.isArray(value) ? value : [],escape,
    isDemo:job => job.demo === true,fullDate:value => value || "Time unknown",
    state:{platforms:[],jobs:[],approval:{approved:true}},lastPlatformSignature:"",...overrides,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.helpers={platformMode,canCheckPlatform,platformReportMarkup,jobPlatformMarkup,platformCatalogMarkup,renderPlatforms};",context);
  return context;
}

const manual = {platform:"workday",label:"Workday",automatic_submission_supported:false,can_inspect:true,mode:"inspection_only",reason:"Account or employer-specific steps need review.",next_action:"Open the employer form to continue manually."};
const job = {id:"example-role",status:"new",platform_support:manual};

test("automatic badges are limited to explicit Lever and Greenhouse capabilities",() => {
  const {platformMode} = helpers().helpers;
  for (const platform of ["lever","greenhouse"]) assert.equal(platformMode({platform,automatic_submission_supported:true}).className,"automatic");
  for (const platform of ["ashby","smartrecruiters","workday","icims","taleo","rippling",undefined]) {
    assert.equal(platformMode({platform,automatic_submission_supported:true,can_inspect:true}).className,"inspection");
  }
  assert.equal(platformMode({...manual,automatic_submission_supported:"true"}).className,"inspection");
  assert.equal(platformMode({...manual,can_inspect:false}).className,"manual");
  assert.equal(platformMode(undefined).className,"unknown");
});

test("platform checks appear only for inspectable unattempted roles",() => {
  const {canCheckPlatform} = helpers().helpers;
  for (const status of ["new","queued","needs_attention","needs_review","manual_review","held","ineligible","failed"]) assert.equal(canCheckPlatform({...job,status}),true,status);
  for (const status of ["active","filling","submitting","applying","uncertain","submitted","applied_elsewhere","dismissed"]) assert.equal(canCheckPlatform({...job,status}),false,status);
  assert.equal(canCheckPlatform({...job,attempted_at:"2026-10-21T12:00:00Z"}),false);
  assert.equal(canCheckPlatform({...job,attempts:1}),false);
  assert.equal(canCheckPlatform({...job,demo:true}),false);
  assert.equal(canCheckPlatform({...job,closed:true}),false);
  assert.equal(canCheckPlatform({...job,platform_check_available:false}),false);
  assert.equal(canCheckPlatform({...job,platform_support:{...manual,can_inspect:"true"}}),false);
  assert.equal(canCheckPlatform({...job,platform_support:null}),false);
});

test("compatibility catalogue distinguishes automatic, inspection and manual providers",() => {
  const html = helpers().helpers.platformCatalogMarkup([
    {platform:"lever",label:"Lever",automatic_submission_supported:true,can_inspect:true},
    {platform:"greenhouse",label:"Greenhouse",automatic_submission_supported:true,can_inspect:true},
    {...manual,platform:"ashby",label:"Ashby"},manual,
    {...manual,platform:"taleo",label:"Taleo",can_inspect:false},
  ]);
  assert.match(html,/2 automatic · 2 inspection-only · 1 manual/);
  assert.match(html,/Read-only check · manual application/);
  assert.match(html,/Open the employer form to continue manually/);
  assert.doesNotMatch(html,/majority|all platforms|works everywhere/i);
  assert.match(helpers().helpers.platformCatalogMarkup(null),/local server returns its support list/);
});

test("a posting-only report cannot imply that the complete application was tested",() => {
  const html = helpers().helpers.platformReportMarkup({checked_at:"2026-10-21T12:00:00Z",platform:"smartrecruiters",stage:"posting_only",gates:[],fields:[],field_count:0,message:"Only the public posting is visible.",next_action:"Open the application to review subsequent steps."});
  assert.match(html,/Observed stage: <strong>posting only/);
  assert.match(html,/0 observed fields/);
  assert.match(html,/No form fields were observed/);
  assert.match(html,/Later steps may still need review/);
  assert.match(html,/does not fill fields, upload your resume, submit an application or approve your settings/);
  assert.doesNotMatch(html,/data-action="(?:queue|run|approve)/);
});

test("reports show observed gates, required controls and exact choices with HTML escaping",() => {
  const html = helpers().helpers.platformReportMarkup({stage:"login_required",checked_at:"now",message:'<script>bad</script>',gates:[{kind:"login",message:'<img src=x onerror=alert(1)>'}],fields:[{label:'Office <svg>',type:"select",required:true,options:[{label:'Boston & remote',value:"b"},{label:'\"No\"',value:"n"}]}],field_count:1,next_action:'Use <em>the portal</em>'});
  assert.match(html,/login required/);
  assert.match(html,/select · Required/);
  assert.match(html,/Boston &amp; remote · &quot;No&quot;/);
  assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/<script|<img|<svg|<em>/);
});

test("large field reports stay bounded while retaining the reported field count",() => {
  const html = helpers().helpers.platformReportMarkup({field_count:150,fields:Array.from({length:150},(_,i) => ({label:`Field ${i}`,type:"text",required:false,options:[]}))});
  assert.match(html,/150 observed fields · showing 100/);
  assert.match(html,/Field 99/);
  assert.doesNotMatch(html,/Field 100/);
});

test("job details show the backend capability reason and preserve a prior check report",() => {
  const html = helpers().helpers.jobPlatformMarkup({...job,platform_check:{stage:"account_required",gates:[{kind:"login",message:"Sign in is required"}],fields:[],field_count:0}});
  assert.match(html,/Workday/);
  assert.match(html,/Read-only check · manual application/);
  assert.match(html,/Account or employer-specific steps need review/);
  assert.match(html,/Sign in is required/);
  assert.equal(helpers().helpers.jobPlatformMarkup({...job,demo:true}),"");
});

test("compatibility rendering only reads server state and updates its own panel",() => {
  const target = {innerHTML:""};
  const state = {platforms:[manual],approval:{approved:true},profile:{name:"Existing"}};
  const before = JSON.stringify(state);
  const context = helpers({state,$:selector => {assert.equal(selector,"#platform-support-list");return target;}});
  context.helpers.renderPlatforms();
  assert.match(target.innerHTML,/Workday/);
  assert.equal(JSON.stringify(state),before);
  assert.match(index,/id="platform-support-title"/);
  assert.match(index,/id="platform-support-list"/);
});

test("status polling preserves the open catalogue and avoids replacing unchanged content",() => {
  const details = {open:true};
  let writes = 0;
  const target = {querySelector:() => details,set innerHTML(value) {writes++;this.html=value;},get innerHTML() {return this.html;}};
  const state = {platforms:[manual]};
  const context = helpers({state,$:() => target});
  context.helpers.renderPlatforms();
  assert.equal(details.open,true);
  assert.equal(writes,1);
  context.helpers.renderPlatforms();
  assert.equal(writes,1);
  state.platforms=[{...manual,reason:"Changed public adapter support."}];
  context.helpers.renderPlatforms();
  assert.equal(writes,2);
  assert.equal(details.open,true);
});

test("Check platform posts only to the read-only route, encodes its job ID and keeps approval unchanged",async() => {
  const id = "role /?&";
  const state = {jobs:[{...job,id}],approval:{approved:true}};
  const posts = [],notices = [],expanded = new Set(),pending = [];
  const context = helpers({state,id,button:{},expanded,showNotice:(...args) => notices.push(args),post:async(...args) => {posts.push(args);return {};},action:(name,button,task,success) => {assert.equal(name,`platform-${id}`);assert.match(success,/nothing was submitted/);pending.push(task());}});
  const start = source.indexOf('    } else if (name === "check-platform") {');
  const end = source.indexOf('    } else if (name === "search-choices") {',start);
  assert.ok(start >= 0 && end > start);
  const branch = source.slice(start,end).replace('    } else if (name === "check-platform") {','async function clickPlatform() {')+"\n}\nthis.clickPlatform=clickPlatform;";
  vm.runInContext(branch,context);
  await context.clickPlatform();
  await Promise.all(pending);
  assert.deepEqual(posts,[[`/api/jobs/${encodeURIComponent(id)}/platform/check`]]);
  assert.equal(state.approval.approved,true);
  assert.equal(expanded.has(id),true);
  assert.equal(notices.length,0);
  state.jobs[0].status="uncertain";
  await context.clickPlatform();
  assert.equal(posts.length,1);
  assert.match(notices[0][0],/cannot be checked/);
});
