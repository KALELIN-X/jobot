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
  vm.runInContext(source.slice(start, end) + "\nthis.helpers = {experienceMatches, experienceVisible, experienceLabel, newGradPriority, compareJobs};", context);
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
