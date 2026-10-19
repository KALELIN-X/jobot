const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function editor(saved = []) {
  const source = fs.readFileSync(path.join(__dirname,"..","static","app.js"),"utf8");
  const start = source.indexOf("  function answerContext(");
  const end = source.indexOf("  function renderActivityQuestions(", start);
  const context = {
    list: value => Array.isArray(value) ? value : [],
    escape: value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char])),
    icon: () => "", externalLink: () => "",
    state: {jobs:[],profile:{answers:{}},answer_memory:{saved,pending:[]}},
    questionSources:new Map(), questionDrafts:new Map(),questionSearches:new Map()
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start,end)+"\nthis.helpers={answerContext,recordedQuestion,questionCard};",context);
  return context;
}
const city = {job_id:"city-job",company:"Example",title:"Engineer",question:"Location (City)",type:"select",required:true,options:[],searchable:true,lookup_required:true};

test("an async dropdown offers search instead of an empty answer selector", () => {
  const {helpers} = editor();
  const html = helpers.questionCard(city);
  assert.match(html,/Search choices/);
  assert.match(html,/name="lookup_query"/);
  assert.doesNotMatch(html,/name="choice"|type="submit"|name="answer"/);
});

test("a legacy empty dropdown can refresh without accepting free text", () => {
  const {helpers} = editor();
  const html = helpers.questionCard({...city,searchable:undefined});
  assert.match(html,/Refresh form choices/);
  assert.doesNotMatch(html,/name="lookup_query"|name="choice"|type="submit"/);
});

test("saved review uses one employer's actual choices and never merges option lists", () => {
  const saved = {question:city.question,answer:"Exact city",sources:[
    {...city,options:[{label:"Exact city",value:"city-1"}]},
    {...city,job_id:"other",company:"Other",options:[{label:"Another city",value:"city-2"}]}]};
  const context = editor([saved]);
  const current = context.helpers.answerContext(saved);
  const html = context.helpers.questionCard(current,saved);
  assert.match(html,/Exact city/);
  assert.match(html,/name="question_source"/);
  assert.doesNotMatch(html,/Another city/);
  assert.match(html,/value="0" checked/);
  context.questionSources.set(city.question,"other");
  assert.equal(context.helpers.answerContext(saved).company,"Other");
  assert.equal(context.helpers.recordedQuestion("other",city.question).options[0].value,"city-2");
});

test("a changed option list discards the old unsaved selection", () => {
  const context = editor();
  context.questionDrafts.set(`${city.job_id}:${city.question}`,{value:"0",signature:JSON.stringify([city.type,[{label:"Old",value:"old"}]])});
  const html = context.helpers.questionCard({...city,options:[{label:"New",value:"new"}]});
  assert.doesNotMatch(html,/checked/);
  assert.match(html,/New/);
});

test("optional multiselect remains manual rather than offering a single-choice answer", () => {
  const {helpers} = editor();
  const html = helpers.questionCard({...city,required:false,multiple:true,options:[{label:"A",value:"a"}]});
  assert.match(html,/Optional questions can stay blank/);
  assert.doesNotMatch(html,/type="submit"|name="choice"/);
});

test("conflicting recorded controls require review instead of free-text confirmation", () => {
  const {helpers} = editor();
  const html = helpers.questionCard({...city,type:"unsupported",conflicting_controls:true});
  assert.match(html,/conflicting or unsupported controls/);
  assert.doesNotMatch(html,/type="submit"|name="answer"|name="choice"/);
});

test("employer labels and typed search terms are escaped in rendered controls", () => {
  const context = editor();
  context.questionSearches.set(`${city.job_id}:${city.question}`,'"><img src=x>');
  const html = context.helpers.questionCard({...city,options:[{label:"<script>bad</script>",value:"a"}]});
  assert.doesNotMatch(html,/<img|<script>/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/&quot;&gt;&lt;img/);
});
