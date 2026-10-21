const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));

function onboardingHelpers() {
  const start = source.indexOf("  function onboardingValue(");
  const end = source.indexOf("  function beginOnboardingStep(", start);
  assert.ok(start >= 0 && end > start);
  const context = {list:value => Array.isArray(value) ? value : [],escape,icon:()=>"",splitLines:value => [...new Set(String(value || "").split(/[\n,]/).map(item=>item.trim()).filter(Boolean))],onboardingDraft:null};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.helpers={onboardingValue,onboardingTypedValue,onboardingPayload,onboardingControl,onboardingReview,onboardingRecordSections,onboardingNewRecord,onboardingFieldsMarkup,onboardingFieldDisplay};",context);
  return context;
}
const plain = value => JSON.parse(JSON.stringify(value));

test("onboarding preserves explicit No, unknown and zero as different facts", () => {
  const {helpers:h}=onboardingHelpers();
  assert.equal(h.onboardingTypedValue({type:"boolean"},"false"),false);
  assert.equal(h.onboardingTypedValue({type:"boolean"},""),null);
  assert.equal(h.onboardingTypedValue({type:"number"},"0"),0);
  assert.equal(h.onboardingTypedValue({type:"number"},""),null);
  assert.deepEqual(plain(h.onboardingTypedValue({type:"list"},"")),[]);
});

test("a step sends only catalog-owned patches and its records with the saved revision", () => {
  const {helpers:h}=onboardingHelpers();
  const snapshot={revision:"saved-revision",profile:{name:"Existing"}};
  const step={id:"work_authorization",fields:[{key:"work_authorized",type:"boolean",storage:"profile"},{key:"sponsorship_future",type:"boolean",storage:"facts"}]};
  const result=h.onboardingPayload(snapshot,step,{work_authorized:"true",sponsorship_future:"false",name:"Do not overwrite",untrusted:"Ignore"});
  assert.deepEqual(plain(result),{step:"work_authorization",expected_revision:"saved-revision",profile_patch:{work_authorized:true},facts:{sponsorship_future:false}});
  assert.equal(snapshot.profile.name,"Existing");
});

test("record identifiers, incoming education and month precision survive the payload", () => {
  const {helpers:h}=onboardingHelpers();
  const education=[{id:"record-a",institution:"Example University",status:"incoming",start_month:"2027-01",end_month:"2028-05",gpa:null,gpa_scale:null}];
  const payload=h.onboardingPayload({revision:"r"},{id:"education",fields:[]},{},{education});
  assert.deepEqual(plain(payload.education),education);
  assert.equal(payload.complete,undefined);
  assert.equal(h.onboardingPayload({revision:"r"},{id:"review",fields:[]},{},{}).complete,true);
});

test("review uses catalog enum labels without changing No, unknown or zero", () => {
  const {helpers:h}=onboardingHelpers();
  const salary={key:"salary_mode",label:"Salary-answer preference",storage:"facts",options:[{value:"posted_range",label:"Use the posted range when available"}]};
  const status={key:"status",label:"Enrollment status",options:[{value:"incoming",label:"Incoming, not yet enrolled"}]};
  const html=h.onboardingReview({catalog:[{id:"preferences",fields:[salary],records:[]},{id:"education",fields:[],records:[{key:"education",fields:[status]}]}],application_profile:{facts:{salary_mode:"posted_range"},education:[{id:"e",status:"incoming"}]}});
  assert.match(html,/Use the posted range when available/);
  assert.match(html,/Incoming, not yet enrolled/);
  assert.doesNotMatch(html,/>posted_range<|status: incoming</);
  assert.equal(h.onboardingFieldDisplay({type:"boolean"},false),"No");
  assert.equal(h.onboardingFieldDisplay({type:"boolean"},null),"Not provided");
  assert.equal(h.onboardingFieldDisplay({type:"number"},0),"0");
});

test("unknown booleans are not rendered as an implicit No and false survives a rerender", () => {
  const {helpers:h}=onboardingHelpers();
  const field={key:"authorized",label:"Authorized?",type:"boolean"};
  const unknown=h.onboardingControl(field,null);
  assert.match(unknown,/Not provided \/ ask me/);
  assert.doesNotMatch(unknown,/<option value="(?:true|false)" selected/);
  assert.match(h.onboardingControl(field,false),/<option value="false" selected/);
  assert.match(h.onboardingControl(field,"false"),/<option value="false" selected/);
});

test("onboarding labels, choices and values are escaped instead of becoming HTML", () => {
  const {helpers:h}=onboardingHelpers();
  const html=h.onboardingControl({key:"name",label:'<script>label</script>',type:"select",description:'<img src=x>',examples:['<script>question</script>'],options:[{value:'"><svg>',label:'<img src=y>'}]},'"><svg>');
  assert.doesNotMatch(html,/<script>|<img|<svg>/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/&quot;&gt;&lt;svg&gt;/);
});

test("record field requirements and numeric constraints come from the schema catalog", () => {
  const {helpers:h}=onboardingHelpers();
  const html=h.onboardingControl({key:"weeks",label:"Weeks",type:"number",required:true,min:1,max:104,step:1},12);
  assert.match(html,/required/);
  assert.match(html,/min="1"/);
  assert.match(html,/max="104"/);
  assert.match(html,/step="1"/);
  assert.deepEqual(plain(h.onboardingRecordSections({records:[{key:"employment"},{key:"untrusted"}]})),[{key:"employment"}]);
});

test("new work records do not turn unknown employment status into false", () => {
  const {helpers:h}=onboardingHelpers();
  const record=h.onboardingNewRecord({fields:[{key:"company"},{key:"current"},{key:"primary_for_applications"}]},"new-record");
  assert.deepEqual(plain(record),{id:"new-record",company:null,current:null,primary_for_applications:false});
});

test("collapsing optional identity fields keeps their saved values in the editor and payload", () => {
  const context=onboardingHelpers();
  const step={id:"identity",fields:[{key:"name",label:"Legal name",type:"text",storage:"profile"},{key:"github",label:"GitHub",type:"url",storage:"profile"}]};
  context.onboardingDraft={values:{name:"Sample Candidate",github:"https://github.com/example"}};
  const html=context.helpers.onboardingFieldsMarkup(step);
  assert.ok(html.indexOf('name="name"')<html.indexOf('<details class="setup-additional-details"'));
  assert.ok(html.indexOf('name="github"')>html.indexOf('<details class="setup-additional-details"'));
  assert.match(html,/value="https:\/\/github.com\/example"/);
  assert.equal(context.helpers.onboardingPayload({revision:"r"},step,context.onboardingDraft.values).profile_patch.github,"https://github.com/example");
});

test("background onboarding loads cannot overwrite an unsaved step", async () => {
  const start=source.indexOf("  async function loadOnboarding(");
  const end=source.indexOf("  async function saveOnboarding(",start);
  let requests=0;
  const context={onboardingLoading:false,onboardingDirty:true,api:async()=>{requests++;},renderOnboarding(){}};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.load=loadOnboarding;",context);
  await context.load();
  assert.equal(requests,0);
  assert.equal(context.onboardingDirty,true);
});

test("traditional profile edits retain their original revision and draft when another tab changes the profile", async () => {
  const controls={};
  for (const key of ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company","experience_months","graduation_date","graduation_month","work_authorized","needs_sponsorship","skills","approved_work_locations","answers"]) controls[key]={value:""};
  let submit,pending,request;
  const form={elements:controls,contains:()=>false,addEventListener(type,handler){if(type==="submit")submit=handler;}};
  const context={state:{profile:{name:"Saved name",email:"saved@example.test",application_profile:{facts:{pronouns:"they/them"}}},profile_revision:"revision-a"},dirty:{profile:false,rules:true},initialized:{profile:false,rules:true},document:{activeElement:null},profileEditorRevision:null,onboardingDirty:false,onboardingStarted:true,
    $:selector=>selector==="#profile-form" ? form : {},list:value=>Array.isArray(value)?value:[],splitLines:value=>String(value || "").split(/[\n,]/).filter(Boolean),booleanValue:value=>value==="true" ? true : value==="false" ? false : null,renderRuleForm(){},showNotice(){},markSaved(){context.dirty.profile=false;},post:async(_url,body)=>{request=body;throw new Error("Your profile changed in another tab.");},action(_name,_button,task){pending=task().catch(()=>{});}};
  vm.createContext(context);
  const hydrateStart=source.indexOf("  function hydrateForms(");
  const hydrateEnd=source.indexOf("  function renderState(",hydrateStart);
  const submitStart=source.indexOf('  $("#profile-form").addEventListener("submit"');
  const submitEnd=source.indexOf('  $("#rules-form").addEventListener("submit"',submitStart);
  vm.runInContext(source.slice(hydrateStart,hydrateEnd)+source.slice(submitStart,submitEnd)+"\nthis.hydrate=hydrateForms;",context);
  context.hydrate();
  assert.equal(context.profileEditorRevision,"revision-a");
  controls.name.value="My unsaved edit";
  context.dirty.profile=true;
  context.state={...context.state,profile_revision:"revision-b",profile:{...context.state.profile,name:"Other tab's saved name"}};
  context.hydrate();
  assert.equal(context.profileEditorRevision,"revision-a");
  assert.equal(controls.name.value,"My unsaved edit");
  submit({preventDefault(){},currentTarget:form});
  await pending;
  assert.equal(request.expected_revision,"revision-a");
  assert.equal(request.name,"My unsaved edit");
  assert.deepEqual(plain(request.application_profile),{facts:{pronouns:"they/them"}});
  assert.equal(context.dirty.profile,true);
  assert.equal(controls.name.value,"My unsaved edit");
});

function questionHelpers() {
  const start=source.indexOf("  function answerContext(");
  const end=source.indexOf("  function renderActivityQuestions(",start);
  const context={list:value=>Array.isArray(value)?value:[],escape,icon:()=>"",externalLink:()=>"",questionDrafts:new Map(),questionSearches:new Map(),questionSources:new Map(),state:{profile:{},jobs:[{id:"a",status:"needs_attention",attempted_at:null}],approval:{approved:true,candidate_fingerprint:"reviewed"},answer_memory:{pending:[],saved:[],groups:[]}}};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.helpers={questionCard,groupedQuestionCards,answerContext,savedAnswerIdentity,recordedQuestion};",context);
  return context;
}

test("canonical facts use explicit common-fact reuse while employer questions default to one application", () => {
  const {helpers:h}=questionHelpers();
  const common=h.questionCard({job_id:"a",company:"Example",question:"Authorized?",type:"text",intent_id:"work_authorized",scope:"global"});
  assert.match(common,/value="global" selected/);
  assert.match(common,/Common fact across supported wording/);
  assert.match(common,/Confirm &amp; run approved queue/);
  const employer=h.questionCard({job_id:"a",company:"Example",question:"Worked for this company?",type:"text",scope:"employer"});
  assert.match(employer,/value="application" selected/);
  assert.doesNotMatch(employer,/value="global"/);
});

test("a polled but unapproved configuration cannot offer quick continuation", () => {
  const context=questionHelpers();
  const question={job_id:"a",company:"Example",question:"Authorized?",type:"text",intent_id:"work_authorized",scope:"global"};
  assert.match(context.helpers.questionCard(question),/Confirm &amp; run approved queue/);
  context.state.approval={approved:false,candidate_fingerprint:"new-unapproved-rules"};
  const html=context.helpers.questionCard(question);
  assert.doesNotMatch(html,/name="intent" value="continue"/);
  assert.match(html,/Confirm &amp; remember answer/);
  assert.match(html,/Save the answer, then review the updated application settings/);
});

test("attempted and uncertain applications never expose quick continuation", () => {
  const context=questionHelpers();
  const question={job_id:"a",company:"Example",question:"Authorized?",type:"text"};
  for (const status of ["submitted","applied_elsewhere","uncertain","active","filling","submitting"]) {
    context.state.jobs=[{id:"a",status}];
    assert.doesNotMatch(context.helpers.questionCard(question),/name="intent" value="continue"/);
  }
  context.state.jobs=[{id:"a",status:"needs_attention",attempted_at:"2026-10-20T00:00:00Z"}];
  assert.doesNotMatch(context.helpers.questionCard(question),/name="intent" value="continue"/);
});

test("grouped questions retain each employer's actual choice menu", () => {
  const context=questionHelpers();
  const a={job_id:"a",company:"A",question:"Favorite language?",type:"select",options:[{label:"Python",value:"py"}]};
  const b={job_id:"b",company:"B",question:"Choose language",type:"select",options:[{label:"Java",value:"jv"}]};
  context.state.answer_memory.groups=[{group_key:"language",questions:[a,b]}];
  const html=context.helpers.groupedQuestionCards([a,b]);
  const cards=html.split('<form class="activity-answer-form"').slice(1);
  assert.equal(cards.length,2);
  assert.match(cards[0],/Python/);
  assert.doesNotMatch(cards[0],/Java/);
  assert.match(cards[1],/Java/);
  assert.doesNotMatch(cards[1],/Python/);
});

test("identical wording in two employer bindings has independent sources and opens the exact saved editor", () => {
  const context=questionHelpers();
  const question="Have you worked here before?";
  const make=(company,prefix,answer)=>({question,answer,scope:"employer",company,intent_id:"employer.previous_employment",sources:[1,2].map(index=>({job_id:`${prefix}${index}`,company,title:"Engineer",question,type:"select",options:[{label:"Yes",value:"yes"},{label:"No",value:"no"}]}))});
  const a=make("Employer A","a","Yes"),b=make("Employer B","b","No");
  context.state.answer_memory.saved=[a,b];
  const aKey=context.helpers.savedAnswerIdentity(a),bKey=context.helpers.savedAnswerIdentity(b);
  assert.notEqual(aKey,bKey);
  context.questionSources.set(aKey,"a2");
  context.questionSources.set(bKey,"b1");
  assert.equal(context.helpers.answerContext(a).job_id,"a2");
  assert.equal(context.helpers.answerContext(b).job_id,"b1");
  assert.equal(context.helpers.recordedQuestion("b1",question,bKey).answer,"No");
  assert.equal(context.helpers.recordedQuestion("a1",question,bKey),null);
  const nodes=[aKey,bKey].map(bindingKey=>({dataset:{bindingKey},open:false,scrollIntoView(){},querySelector:()=>({dataset:{question}})}));
  context.document={querySelectorAll:()=>nodes};
  context.setView=()=>{};
  context.showNotice=()=>{};
  context.resetAnswerForm=()=>{throw new Error("Scoped edit must not reach the legacy form");};
  const editStart=source.indexOf("  function editAnswer(");
  const editEnd=source.indexOf("  function renderActivity(",editStart);
  vm.runInContext(source.slice(editStart,editEnd)+"\nthis.edit=editAnswer;",context);
  context.edit(b);
  assert.equal(nodes[0].open,false);
  assert.equal(nodes[1].open,true);
  const aHtml=context.helpers.questionCard(context.helpers.answerContext(a),a);
  const bHtml=context.helpers.questionCard(context.helpers.answerContext(b),b);
  assert.notEqual(aHtml.match(/data-answer-key="([^"]+)"/)[1],bHtml.match(/data-answer-key="([^"]+)"/)[1]);
});

test("a scoped answer without observed form metadata cannot fall back to a global legacy editor", () => {
  const context=questionHelpers();
  const item={question:"Employer history?",answer:"No",scope:"employer",company:"Employer B",sources:[]};
  let notice="",legacyOpened=false;
  context.setView=()=>{};
  context.$$=()=>[];
  context.showNotice=message=>{notice=message;};
  context.resetAnswerForm=()=>{legacyOpened=true;};
  const editStart=source.indexOf("  function editAnswer(");
  const editEnd=source.indexOf("  function renderActivity(",editStart);
  vm.runInContext(source.slice(editStart,editEnd)+"\nthis.edit=editAnswer;",context);
  context.edit(item);
  assert.equal(legacyOpened,false);
  assert.match(notice,/Refresh the original employer form/);
  assert.equal(item.scope,"employer");
  assert.equal(item.answer,"No");
});

test("site actions keep legal, verification and uncertain delivery separate from ordinary answers", () => {
  const start=source.indexOf("  function reviewActionCards(");
  const end=source.indexOf("  function renderReviewActions(",start);
  const context={list:value=>Array.isArray(value)?value:[],escape,icon:()=>"",externalLink:(_url,label)=>`<a>${escape(label)}</a>`};
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.cards=reviewActionCards;",context);
  const html=context.cards([{kind:"agreement",company:"Employer",title:"Engineer",message:"Accept the <terms>",apply_url:"https://example.com"},{kind:"uncertain",company:"Second",title:"Intern",message:"No receipt",apply_url:"https://example.com"}]);
  assert.match(html,/Review an agreement/);
  assert.match(html,/Accept the &lt;terms&gt;/);
  assert.match(html,/Check employer status/);
  assert.match(html,/will not automatically retry/);
  assert.doesNotMatch(html,/activity-answer-form|type="submit"|Auto-accept|name="answer"/);
});
