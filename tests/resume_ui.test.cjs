const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const source=fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
const escape=value=>String(value??"").replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const plain=value=>JSON.parse(JSON.stringify(value));
const session="a".repeat(48),pause="b".repeat(32),fingerprint="c".repeat(64);
const job=()=>({id:"example-role",company:"Example",title:"Engineer",status:"needs_attention",attempts:0,resume_available:true,verification:{state:"paused",kind:"verification",session_id:session,pause_id:pause,message:"Complete the displayed verification.",next_action:"Use the separate Jobot browser."}});

function helpers(overrides={}) {
  const context={list:value=>Array.isArray(value)?value:[],escape,isDemo:item=>item.demo===true,
    state:{jobs:[job()],approval:{approved:true,candidate_fingerprint:fingerprint}},dirty:{profile:false,rules:false},onboardingDirty:false,expanded:new Set(),...overrides};
  vm.createContext(context);
  const start=source.indexOf("  function canResumeApplication(");
  const end=source.indexOf("  function platformCatalogMarkup(",start);
  assert.ok(start>=0&&end>start);
  vm.runInContext(source.slice(start,end)+"\nthis.helpers={canResumeApplication,verificationMarkup,renderVerification};",context);
  return context;
}

function resumeButton(html) {
  return html.match(/<button[^>]*data-action="resume-application"[^>]*>/)?.[0]||"";
}

function panel() {
  let html="",writes=0,cards=[];
  return {dataset:{},get innerHTML(){return html;},set innerHTML(value){
    html=value;writes++;
    cards=[...value.matchAll(/class="verification-card" data-id="([^"]*)" data-pause="([^"]*)"/g)].map(match=>({dataset:{id:match[1],pause:match[2]},checkbox:{checked:false},querySelector(selector){assert.equal(selector,".verification-ack");return this.checkbox;}}));
  },querySelectorAll(selector){assert.equal(selector,".verification-card");return cards;},get cards(){return cards;},get writes(){return writes;}};
}

test("a resumable receipt starts with an explicit unchecked acknowledgment",()=>{
  const context=helpers();
  const html=context.helpers.verificationMarkup(context.state.jobs[0]);
  assert.match(html,/I completed this step and reviewed the current answers/);
  assert.match(html,/class="verification-ack"/);
  assert.doesNotMatch(html,/<input[^>]*class="verification-ack"[^>]*checked/);
  assert.doesNotMatch(resumeButton(html),/disabled/);
  assert.match(html,/Show application browser/);
  assert.match(html,/Do not submit manually/);
});

test("resume requires current approved settings, valid receipt IDs and an unattempted live role",()=>{
  const context=helpers();
  const base=job();
  assert.equal(context.helpers.canResumeApplication(base),true);
  for(const change of [{resume_available:false},{resume_available:"true"},{attempted_at:"2026-10-21T12:00:00Z"},{attempts:1},{status:"submitted"},{status:"uncertain"},{status:"applying"},{demo:true},{verification:{...base.verification,state:"interrupted"}},{verification:{...base.verification,session_id:"short"}},{verification:{...base.verification,pause_id:"bad"}}]){
    const candidate={...base,...change};
    assert.equal(context.helpers.canResumeApplication(candidate),false,JSON.stringify(change));
    if(candidate.verification.state==="paused")assert.match(resumeButton(context.helpers.verificationMarkup(candidate)),/disabled/);
  }
  for(const approval of [{approved:false,candidate_fingerprint:fingerprint},{approved:true,candidate_fingerprint:""},{approved:"true",candidate_fingerprint:fingerprint}]){
    context.state.approval=approval;
    assert.equal(context.helpers.canResumeApplication(base),false);
  }
});

test("unsaved profile, rule and onboarding changes prevent resume without changing their drafts",()=>{
  const context=helpers();
  for(const field of ["profile","rules"]){
    context.dirty[field]=true;
    const before=JSON.stringify(context.dirty);
    assert.equal(context.helpers.canResumeApplication(job()),false);
    assert.equal(JSON.stringify(context.dirty),before);
    context.dirty[field]=false;
  }
  context.onboardingDirty=true;
  assert.equal(context.helpers.canResumeApplication(job()),false);
});

test("public site messages and receipt attributes are escaped",()=>{
  const candidate={...job(),id:'role"><svg>',verification:{...job().verification,message:'<script>bad()</script>',next_action:'<img src=x onerror=bad()>',pause_id:'"><svg>'}};
  const html=helpers().helpers.verificationMarkup(candidate);
  assert.match(html,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.match(html,/&lt;img/);
  assert.match(html,/data-id="role&quot;&gt;&lt;svg&gt;"/);
  assert.doesNotMatch(html,/<script|<img|<svg/);
});

test("interrupted and resuming receipts cannot start a second browser or submission",()=>{
  const context=helpers();
  for(const state of ["interrupted","resuming"]){
    const html=context.helpers.verificationMarkup({...job(),verification:{...job().verification,state}});
    assert.doesNotMatch(html,/data-action="(?:resume|focus|cancel)-application"/);
  }
  assert.equal(context.helpers.verificationMarkup({...job(),verification:null}),"");
  assert.equal(context.helpers.verificationMarkup({...job(),verification:{state:"submitted"}}),"");
});

test("an expired paused browser stays visible for closing but cannot resume",()=>{
  const html=helpers().helpers.verificationMarkup({...job(),resume_available:false});
  assert.match(resumeButton(html),/disabled/);
  assert.match(html,/data-action="cancel-application"/);
  assert.match(html,/Close an expired browser receipt/);
});

test("Activity polling preserves acknowledgment only for the same job and pause",()=>{
  const target=panel();
  const context=helpers({$:selector=>{assert.equal(selector,"#application-verification-list");return target;}});
  context.helpers.renderVerification();
  assert.equal(target.writes,1);
  target.cards[0].checkbox.checked=true;
  context.helpers.renderVerification();
  assert.equal(target.writes,1);
  context.state.jobs[0].verification.message="Same current gate, updated status";
  context.helpers.renderVerification();
  assert.equal(target.writes,2);
  assert.equal(target.cards[0].checkbox.checked,true);
  context.state.jobs[0].verification.pause_id="d".repeat(32);
  context.helpers.renderVerification();
  assert.equal(target.cards[0].checkbox.checked,false);
  context.state.jobs=[];
  context.helpers.renderVerification();
  assert.match(target.innerHTML,/No application browsers are waiting/);
});

function clickHarness(actionName,overrides={}) {
  const posts=[],notices=[],pending=[];
  const checkbox={checked:true};
  const card={dataset:{pause},querySelector:selector=>{assert.equal(selector,".verification-ack");return checkbox;}};
  const id="role /?&";
  const context=helpers({id,button:{closest:selector=>{assert.equal(selector,".verification-card");return card;}},
    post:async(...args)=>{posts.push(plain(args));return{message:"Observed result"};},
    showNotice:(...args)=>notices.push(args),action:(name,button,task)=>pending.push(task()),...overrides});
  context.state.jobs=[{...job(),id}];
  const start=source.indexOf(`    } else if (name === "${actionName}") {`);
  const end=source.indexOf('    } else if (name === "',start+10);
  assert.ok(start>=0&&end>start);
  const branch=source.slice(start,end).replace(`    } else if (name === "${actionName}") {`,"async function clickAction() {")+"\n}\nthis.clickAction=clickAction;";
  vm.runInContext(branch,context);
  return {context,posts,notices,pending,checkbox,card,id,async click(){await context.clickAction();await Promise.all(pending);}};
}

test("Resume posts only the exact encoded job, session, pause and reviewed fingerprint",async()=>{
  const h=clickHarness("resume-application");
  await h.click();
  assert.deepEqual(h.posts,[[`/api/jobs/${encodeURIComponent(h.id)}/application/resume`,{session_id:session,pause_id:pause,acknowledge:true,expected_fingerprint:fingerprint}]]);
  assert.equal(h.context.state.approval.approved,true);
  assert.doesNotMatch(JSON.stringify(h.posts),/api\/(?:run|approval)|profile|password|email/);
});

test("Resume refuses unchecked, stale, unapproved or dirty state before making a request",async()=>{
  for(const mutate of [h=>h.checkbox.checked=false,h=>h.card.dataset.pause="d".repeat(32),h=>h.context.state.approval.approved=false,h=>h.context.dirty.profile=true,h=>h.context.dirty.rules=true,h=>h.context.onboardingDirty=true,h=>h.context.state.jobs[0].attempts=1]){
    const h=clickHarness("resume-application");mutate(h);await h.click();
    assert.equal(h.posts.length,0);
    assert.equal(h.notices.length,1);
  }
});

test("Focus and cancel bind their exact current session and cannot target a stale card",async()=>{
  for(const actionName of ["focus-application","cancel-application"]){
    const h=clickHarness(actionName);
    await h.click();
    const action=actionName.split("-")[0];
    assert.deepEqual(h.posts,[[`/api/jobs/${encodeURIComponent(h.id)}/application/${action}`,{session_id:session,pause_id:pause}]]);
    h.card.dataset.pause="d".repeat(32);
    await h.click();
    assert.equal(h.posts.length,1);
    assert.match(h.notices[0][0],/step changed/);
  }
});

test("job-detail polling retains same-pause checks and expanded roles without editing drafts",()=>{
  const target=panel(),elements=new Map();
  const get=selector=>{if(selector==="#job-list")return target;if(!elements.has(selector))elements.set(selector,{setAttribute(){}});return elements.get(selector);};
  const context=helpers({$:get,state:{jobs:[job()],rules:{},approval:{approved:true,candidate_fingerprint:fingerprint}},lastJobSignature:"",filter:"all",search:"",experienceFilterEnabled:false,
    experienceLimit:()=>null,experienceVisible:()=>true,jobAttention:()=>true,compareJobs:()=>0,formatNumber:String,icon:()=>"",expanded:new Set([job().id])});
  context.jobCard=candidate=>context.helpers.verificationMarkup(candidate);
  const start=source.indexOf("  function renderJobs(");
  const end=source.indexOf("  function queueCardAction(",start);
  vm.runInContext(source.slice(start,end)+"\nthis.renderJobs=renderJobs;",context);
  context.renderJobs();
  target.cards[0].checkbox.checked=true;
  context.dirty.profile=true;
  context.renderJobs();
  assert.equal(target.cards[0].checkbox.checked,true);
  assert.equal(context.expanded.has(job().id),true);
  assert.equal(context.dirty.profile,true);
  context.state.jobs[0].verification.pause_id="d".repeat(32);
  context.renderJobs();
  assert.equal(target.cards[0].checkbox.checked,false);
});
