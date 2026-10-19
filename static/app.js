"use strict";

(() => {
  const $ = (selector, context = document) => context.querySelector(selector);
  const $$ = (selector, context = document) => [...context.querySelectorAll(selector)];
  const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const list = value => Array.isArray(value) ? value : [];
  const formatNumber = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "0";
  const splitLines = value => [...new Set(String(value || "").split(/[\n,]/).map(item => item.trim()).filter(Boolean))];
  const booleanValue = value => value === "true" ? true : value === "false" ? false : null;
  let state = {profile:{},resume:{loaded:false},rules:{},approval:{approved:false},jobs:[],events:[],stats:{},automation:{}};
  let loaded = false;
  let filter = "all";
  let search = "";
  let experienceFilterEnabled = false;
  let pollTimer;
  let refreshing = false;
  let refreshInFlight = null;
  let noticeTimer;
  let lastJobSignature = "";
  let lastEventSignature = "";
  let sourceMessage = "";
  let lastSyncAt = null;
  let connectionLost = false;
  let lastInboxSignature = "";
  const questionDrafts = new Map();
  let currentView = "queue";
  let approvalReview = null;
  let lastMemorySignature = "";
  const dirty = {profile:false,rules:false};
  const initialized = {profile:false,rules:false};
  const expanded = new Set();
  const busyActions = new Set();
  const viewNames = {queue:"Application queue",profile:"Profile & resume",rules:"Application rules",activity:"Activity"};
  const attentionStatuses = new Set(["needs_attention","needs_review","review","failed","blocked","error","ineligible","manual_review","held","uncertain"]);

  function safeURL(value) {
    try { const url = new URL(value); return ["http:","https:"].includes(url.protocol) ? url.href : null; } catch { return null; }
  }
  function supportedPostingURL(value) {
    if (typeof value !== "string" || /[\s\\]/.test(value)) return null;
    try {
      const url = new URL(value);
      const posting = /^\/[A-Za-z0-9][A-Za-z0-9_-]*\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\/apply)?\/?$/i;
      const greenhouse = ["boards.greenhouse.io","job-boards.greenhouse.io","boards.eu.greenhouse.io","job-boards.eu.greenhouse.io"].includes(url.hostname) && /^\/[A-Za-z0-9][A-Za-z0-9_-]*\/jobs\/[0-9]+\/?$/.test(url.pathname);
      const lever = ["jobs.lever.co","jobs.eu.lever.co"].includes(url.hostname) && posting.test(url.pathname);
      return url.protocol === "https:" && (lever || greenhouse) && !url.username && !url.password && (!url.port || url.port === "443") ? url.href : null;
    } catch { return null; }
  }
  function externalLink(url, label, className = "button button-secondary") {
    const safe = safeURL(url);
    return safe ? `<a class="${className}" href="${escape(safe)}" target="_blank" rel="noopener noreferrer">${escape(label)}${icon("external")}</a>` : "";
  }
  function dateObject(value) {
    // Date.parse treats yearless labels such as "Oct 18" as a date in 2001.
    // The API supplies ISO dates; refuse ambiguous or invalid calendar labels.
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?)?$/.test(value)) return null;
    const [year,month,day] = value.slice(0,10).split("-").map(Number);
    const calendar = new Date(Date.UTC(year,month-1,day));
    if (calendar.getUTCFullYear() !== year || calendar.getUTCMonth() !== month-1 || calendar.getUTCDate() !== day) return null;
    const result = new Date(value);
    return Number.isNaN(result.getTime()) ? null : result;
  }
  function relativeDate(value, prefix = "") {
    const date = dateObject(value);
    if (!date) return "Listing date unknown";
    const days = Math.max(0,Math.floor((Date.now() - date.getTime()) / 86400000));
    return `${prefix}${days === 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`}`;
  }
  function fullDate(value) {
    const date = dateObject(value);
    return date ? date.toLocaleString(undefined,{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "Time unknown";
  }
  function showNotice(message, error = false) {
    clearTimeout(noticeTimer);
    $("#notice-text").textContent = message;
    $("#notice").classList.toggle("error",error);
    $("#notice").hidden = false;
    if (!error) noticeTimer = setTimeout(() => { $("#notice").hidden = true; },7500);
  }
  function errorMessage(data, fallback) {
    const detail = data?.detail ?? data?.error ?? data?.message;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => typeof item === "string" ? item : item.msg || JSON.stringify(item)).join("; ");
    return fallback;
  }
  async function api(path, options = {}) {
    let response;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(),path === "/api/state" ? 6000 : 90000);
    try { response = await fetch(path,{...options,signal:controller.signal,headers:{"X-JobRadar-Client":"dashboard",...(options.body instanceof FormData ? {} : {"Content-Type":"application/json"}),...(options.headers || {})}}); }
    catch { clearTimeout(timeout); throw new Error(path === "/api/state" ? "The local server did not return a fresh status. Check that Jobot is running and refresh status." : "The request could not be confirmed. Check Activity and the current status before trying again."); }
    let data;
    try { data = await response.json(); } catch { if (controller.signal.aborted) throw new Error("The local server stopped responding before the response finished. Refresh status before retrying."); data = {}; }
    finally { clearTimeout(timeout); }
    if (!response.ok) throw new Error(errorMessage(data,`Request could not be completed (${response.status}).`));
    return data;
  }
  async function post(path, body = {}) { return api(path,{method:"POST",body:body instanceof FormData ? body : JSON.stringify(body)}); }
  async function action(name, button, task, success) {
    if (busyActions.has(name)) return;
    busyActions.add(name);
    const previousHTML = button?.innerHTML;
    if (button) { button.disabled = true; button.classList.add("busy"); button.setAttribute("aria-busy","true"); if (name === "queue-matching") button.textContent = "Loading jobs to queue…"; }
    try {
      const result = await task();
      await refresh(true);
      if (success) showNotice(typeof success === "function" ? success(result) : success);
      return result;
    } catch (error) { showNotice(error.message || "The action could not be completed.",true); }
    finally {
      busyActions.delete(name);
      if (button) { button.classList.remove("busy"); button.removeAttribute("aria-busy"); button.disabled = false; if (previousHTML) button.innerHTML = previousHTML; }
      renderControls();
    }
  }
  function setView(view, updateHash = true) {
    if (!viewNames[view]) view = "queue";
    const enteringRules = view === "rules" && currentView !== "rules";
    currentView = view;
    $$(".view").forEach(section => { section.hidden = section.id !== `view-${view}`; });
    $$(".nav-button").forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle("active",active);
      if (active) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
    });
    $("#breadcrumb-current").textContent = viewNames[view];
    renderSummary(enteringRules);
    renderControls();
    if (updateHash) {
      history.replaceState(null,"",`#${view}`);
      const heading = $(`#view-${view} h1`);
      heading.setAttribute("tabindex","-1");
      heading.focus({preventScroll:true});
      window.scrollTo(0,0);
    }
  }
  function isDemo(job) {
    return job.demo === true || job.is_demo === true || job.source === "demo" || job.ats === "demo" || /local mock|demo employer/i.test(`${job.company} ${job.title}`);
  }
  function statusInfo(job) {
    const status = String(job.status || "discovered").toLowerCase();
    const known = {discovered:["Discovered",""],new:["Discovered",""],queued:["Queued","success"],submitted:["Submitted","success"],dismissed:["Dismissed",""],applied_elsewhere:["Applied elsewhere",""],needs_attention:["Needs attention","warning"],needs_review:["Needs review","warning"],review:["Needs review","warning"],manual_review:["Manual review","warning"],blocked:["Held for review","warning"],held:["Held for review","warning"],ineligible:["Eligibility mismatch","warning"],failed:["Submission failed","danger"],error:["Action failed","danger"],active:["In progress","warning"],filling:["Filling","warning"],submitting:["Submitting","warning"],applying:["Applying","warning"],uncertain:["Check submission","warning"]};
    return known[status] || [status.replaceAll("_"," "),""];
  }
  function jobAttention(job) {
    const status = String(job.status).toLowerCase();
    return !["submitted","dismissed","applied_elsewhere"].includes(status) && (attentionStatuses.has(status) || Boolean(job.error));
  }
  function experienceLimit(rules = state.rules) {
    const value = rules?.max_required_experience_months;
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  }
  function experienceMatches(job, maximum) {
    const minimum = job.score_detail?.experience_requirement?.minimum_months;
    return typeof minimum === "number" && Number.isFinite(minimum) && minimum >= 0 && minimum <= maximum;
  }
  function newGradExperienceUnknown(job) {
    const minimum = job.score_detail?.experience_requirement?.minimum_months;
    return job.score_detail?.is_new_grad === true && (typeof minimum !== "number" || !Number.isFinite(minimum) || minimum < 0);
  }
  function experienceVisible(job, maximum) {
    // Discovery can show explicit new-grad roles for review without treating
    // an unspecified requirement as zero years or authorizing submission.
    return experienceMatches(job,maximum) || state.rules?.prefer_new_grad === true && newGradExperienceUnknown(job);
  }
  function experienceLabel(requirement = {}, isNewGrad = false) {
    requirement = requirement || {};
    const minimum = requirement.minimum_months;
    const maximum = requirement.maximum_months;
    if (typeof minimum !== "number" || !Number.isFinite(minimum) || minimum < 0) return isNewGrad ? "Experience: unknown · review needed" : "Experience: unknown";
    const years = minimum % 12 === 0 && (maximum == null || typeof maximum === "number" && maximum % 12 === 0);
    const divisor = years ? 12 : 1;
    const unit = years ? "yr" : "mo";
    const range = typeof maximum === "number" && Number.isFinite(maximum) && maximum >= minimum;
    return range ? `Experience: ${minimum/divisor}${maximum !== minimum ? `–${maximum/divisor}` : ""} ${unit}` : `Experience: ${minimum/divisor}+ ${unit}`;
  }
  function newGradPriority(job) {
    const maximum = experienceLimit();
    return job.score_detail?.is_new_grad === true && (maximum === null || experienceVisible(job,maximum));
  }
  function compareJobs(a,b) {
    const selected = Number(b.status === "queued") - Number(a.status === "queued");
    if (selected) return selected;
    const questions = Number(Boolean(b.unanswered_question_count || b.conflicting_answer_count)) - Number(Boolean(a.unanswered_question_count || a.conflicting_answer_count));
    if (questions) return questions;
    const ready = Number(b.submission_check?.eligible === true) - Number(a.submission_check?.eligible === true);
    if (ready) return ready;
    if (state.rules?.prefer_new_grad) {
      const priority = Number(newGradPriority(b)) - Number(newGradPriority(a));
      if (priority) return priority;
    }
    return (Number.isFinite(Number(b.score)) && b.score !== null ? Number(b.score) : -1) - (Number.isFinite(Number(a.score)) && a.score !== null ? Number(a.score) : -1);
  }
  function renderJobs(force = false) {
    const signature = JSON.stringify([state.jobs,state.rules,state.answer_memory?.pending,state.run?.updated_at,filter,search,experienceFilterEnabled]);
    if (!force && signature === lastJobSignature) return;
    lastJobSignature = signature;
    const allJobs = list(state.jobs);
    const maximum = experienceLimit();
    $("#experience-filter-bar").hidden = maximum === null;
    $("#experience-filter").checked = experienceFilterEnabled;
    $(".sort-label").innerHTML = state.rules?.prefer_new_grad ? "Ready, new grads, fit <span>↓</span>" : "Ready, then fit <span>↓</span>";
    const jobs = allJobs.filter(job => {
      const status = String(job.status || "").toLowerCase();
      if (filter === "ready" && job.submission_check?.matches_rules !== true) return false;
      if (filter === "queued" && status !== "queued") return false;
      if (filter === "attention" && !jobAttention(job)) return false;
      if (filter === "submitted" && status !== "submitted") return false;
      if (maximum !== null && experienceFilterEnabled && status !== "queued" && !experienceVisible(job,maximum)) return false;
      return !search || `${job.company} ${job.title} ${job.location}`.toLowerCase().includes(search);
    }).sort(compareJobs);
    $("#job-count").textContent = jobs.length === allJobs.length ? formatNumber(allJobs.length) : `${formatNumber(jobs.length)} / ${formatNumber(allJobs.length)}`;
    $("#job-count").setAttribute("aria-label",`${jobs.length} of ${allJobs.length} roles shown`);
    $("#experience-filter-detail").textContent = maximum === null ? "" : `${jobs.length} of ${allJobs.length} roles shown · ${maximum === 0 ? "No experience required" : `${maximum/12} year${maximum === 12 ? "" : "s"} maximum required`}${experienceFilterEnabled ? ". Selected queued jobs stay visible. Turn off to see all experience levels." : ". Showing all experience levels; your saved application rules still apply."}`;
    if (!allJobs.length) {
      $("#job-list").innerHTML = `<div class="empty-state"><div class="empty-radar"><span></span><i></i></div><div class="empty-eyebrow">A LITTLE SETUP. A LOT OF POSSIBILITY.</div><h3>Your next opportunity starts here</h3><p>Add your resume and choose where you want to apply.<br>We’ll help you turn a promising role into a thoughtful application.</p><button class="button button-primary" type="button" data-view="profile">Set up your profile${icon("arrow")}</button></div>`;
      return;
    }
    if (!jobs.length) {
      $("#job-list").innerHTML = `<div class="simple-empty">${icon("search")}<h3>No roles in this view</h3><p>${search ? "Try another role or company name." : maximum !== null && experienceFilterEnabled ? "Turn off the experience filter or choose another view to review more roles, including unknown requirements." : "Choose All roles to see the rest of your opportunities."}</p><button type="button" class="text-button" data-action="clear-filter" style="margin-top:18px">Show all roles</button></div>`;
      return;
    }
    $("#job-list").innerHTML = jobs.map(jobCard).join("");
  }
  function queueCardAction(job) {
    if (job.queue_actions?.can_add === true) return {kind:"add",label:"Add to queue",reason:""};
    if (job.queue_actions?.can_remove === true) return {kind:"remove",label:"Remove from queue",reason:""};
    return {kind:"unavailable",label:"Add to queue",reason:job.queue_actions?.reason || (isDemo(job) ? "Local demo records stay separate from your application queue." : job.status === "queued" ? "This role is already in your queue." : "This application cannot be queued again. Review its recorded outcome.")};
  }
  function jobFactsMarkup(job) {
    const facts = job.facts || {};
    const entries = [
      ["location","Location",job.location || "Not listed"],
      ["experience","Experience",experienceLabel(job.score_detail?.experience_requirement,job.score_detail?.is_new_grad === true).replace(/^Experience: /,"")],
      ["pay","Pay","Not listed"],
      ["degree","Education","Not listed"],
      ["graduation","Graduation window","Not listed"]
    ];
    return `<dl class="job-facts">${entries.map(([key,label,fallback]) => {
      const fact = facts[key] || {};
      const summary = typeof fact.summary === "string" && fact.summary.trim() ? fact.summary : fallback;
      return `<div class="job-fact ${key === "location" ? "fact-location" : ""}"><dt>${escape(label)}</dt><dd class="${fact.status === "not_stated" || summary === "Not listed" ? "not-listed" : ""}">${escape(summary)}</dd></div>`;
    }).join("")}</dl>`;
  }
  function jobCard(job) {
    const id = String(job.id);
    const score = job.score !== null && job.score !== undefined && Number.isFinite(Number(job.score)) ? Math.round(Number(job.score)) : null;
    const scoreClass = score === null ? "unknown" : score < Number(state.rules.min_score || 0) ? "low" : "";
    const [statusLabel,statusClass] = statusInfo(job);
    const detail = job.score_detail || {};
    const initials = String(job.company || "?").split(/\s+/).filter(Boolean).slice(0,2).map(word => word[0]).join("").toUpperCase();
    const detailId = `job-detail-${id.replace(/[^a-zA-Z0-9_-]/g,"-")}`;
    const open = expanded.has(id);
    const evidence = items => list(items).length ? `<ul class="detail-list">${list(items).map(item => `<li>${escape(typeof item === "object" ? item.label || item.detail || JSON.stringify(item) : item)}</li>`).join("")}</ul>` : `<p class="detail-empty">${state.resume?.loaded ? "None identified." : "Upload your resume to assess fit."}</p>`;
    const eligibility = list(detail.eligibility).map(item => {
      const status = String(item.status || "unknown").toLowerCase();
      const cls = status === "waived" ? "waived" : ["pass","eligible","met","yes","true","confirmed"].includes(status) ? "pass" : ["fail","ineligible","not_met","no","false"].includes(status) ? "fail" : "unknown";
      return `<div class="eligibility-item ${cls}"><span class="status-dot"></span><span><strong>${escape(item.label || "Eligibility")}</strong> · ${escape(item.detail || status.replaceAll("_"," "))}</span></div>`;
    }).join("");
    const status = String(job.status).toLowerCase();
    const isSubmitted = status === "submitted";
    const hideJobActions = isDemo(job) || Boolean(job.attempted_at) || ["active","filling","submitting","applying","uncertain","applied_elsewhere"].includes(status);
    const canRecordApplied = !hideJobActions && !isSubmitted;
    const priorApplicationHelp = canRecordApplied && !supportedPostingURL(job.apply_url) ? '<p class="history-help">Already applied? Without an exact supported posting URL, recording this role also holds future listings with the same company and title for review.</p>' : "";
    const checks = list(job.submission_check?.blockers);
    const preflight = !isDemo(job) && !isSubmitted ? `<div class="detail-label">Submission checks</div>${checks.length ? `<ul class="queue-blockers">${checks.map(problem => `<li>${escape(problem)}</li>`).join("")}</ul>` : '<p class="detail-empty">This role passes saved job rules. Configuration approval and daily limits still apply.</p>'}${list(state.answer_memory?.pending).some(item => item.job_id === job.id) ? '<button type="button" class="text-button" data-action="open-questions" data-id="' + escape(id) + '">Answer new application questions →</button>' : ""}` : "";
    const pendingCount = list(state.answer_memory?.pending).filter(item => item.job_id === job.id).length;
    const queueAction = queueCardAction(job);
    const selectionButton = queueAction.kind === "unavailable" ? `<button type="button" class="button button-secondary" disabled title="${escape(queueAction.reason)}">${queueAction.label}</button><span class="queue-action-reason">${escape(queueAction.reason)}</span>` : `<button type="button" class="button ${queueAction.kind === "add" ? "button-primary" : "button-secondary"}" data-action="${queueAction.kind === "add" ? "queue" : "unqueue"}" data-id="${escape(id)}">${queueAction.label}${queueAction.kind === "add" ? icon("arrow") : ""}</button>`;
    const quickActions = `<div class="job-quick-actions">${selectionButton}${pendingCount ? `<button type="button" class="button button-secondary" data-action="open-questions" data-id="${escape(id)}">Review ${pendingCount} question${pendingCount === 1 ? "" : "s"}</button>` : ""}</div>`;
    const active = state.run?.active_job?.id === job.id && ["running","stopping"].includes(state.run?.status);
    const observed = active ? `<div class="card-worker-stage">${icon("activity")} ${escape(state.run.message)} · ${escape(fullDate(state.run.updated_at))}</div>` : "";
    const submissionInfo = isSubmitted ? [...new Set([job.message,job.confirmation,job.error].filter(Boolean).map(value => typeof value === "object" ? JSON.stringify(value) : String(value)))].join(" ") : "";
    const sourceEvidence = Object.values(job.facts || {}).flatMap(fact => list(fact.evidence));
    return `<article class="job-card" data-job-id="${escape(id)}">
      <div class="job-card-top"><div class="company-avatar" aria-hidden="true">${escape(initials)}</div><div class="job-heading"><div class="job-company">${escape(job.company || "Company not provided")}</div><h3 class="job-title">${escape(job.title || "Untitled role")}</h3><div class="job-meta">${job.work_model ? `<span>${escape(job.work_model)}</span>` : ""}<span title="Date listed by the discovery feed; the original posting date may differ.">${escape(relativeDate(job.date_posted,"Feed listed "))}</span></div></div><div class="job-score ${scoreClass}"><strong>${score === null ? "—" : score}</strong><small>${score === null ? "FIT UNKNOWN" : "RESUME FIT"}</small></div></div>
      ${!isDemo(job) ? jobFactsMarkup(job) : ""}
      <div class="job-card-bottom"><div class="job-tags"><span class="tag ${statusClass}">${escape(statusLabel)}</span>${job.submission_check?.matches_rules === true ? '<span class="tag success">Matches rules</span>' : ""}${job.ats && !isDemo(job) ? `<span class="tag">${escape(String(job.ats).toUpperCase())}</span>` : ""}${detail.is_new_grad === true ? '<span class="tag">New grad / entry level</span>' : ""}${isDemo(job) ? '<span class="tag demo">LOCAL DEMO</span>' : ""}</div><button type="button" class="details-toggle" data-action="details" data-id="${escape(id)}" aria-expanded="${open}" aria-controls="${escape(detailId)}">${open ? "Less detail" : "Checks & details"}${icon("chevron")}</button></div>
      ${quickActions}${observed}
      <div class="job-detail" id="${escape(detailId)}" ${open ? "" : "hidden"}>${isDemo(job) ? '<div class="demo-banner">Demo uses a local mock employer. No real employer receives this application.</div>' : ""}${job.error && !["submitted","dismissed","applied_elsewhere"].includes(status) ? `<div class="job-error">${escape(job.error)}</div>` : ""}${submissionInfo ? `<div class="submission-confirmation"><div class="detail-label">Submission confirmation</div><p>${escape(submissionInfo)}</p></div>` : ""}${preflight}
        <details class="fit-explanation"><summary>Score evidence & eligibility</summary><div class="detail-columns"><div><div class="detail-label">Resume matches</div>${evidence(detail.matches)}</div><div><div class="detail-label">Not evidenced</div>${evidence(detail.not_evidenced)}</div></div><div class="detail-label">Eligibility checks</div><div class="eligibility-list">${eligibility || '<p class="detail-empty">Eligibility has not been assessed. Review the role before applying.</p>'}</div>${list(detail.reasons).length ? `<div class="detail-label">Scoring notes</div>${evidence(detail.reasons)}` : ""}${sourceEvidence.length ? `<div class="detail-label">Job fact source excerpts</div>${evidence([...new Set(sourceEvidence)])}` : ""}</details>
        ${priorApplicationHelp}<div class="detail-actions">${externalLink(job.detail_url || job.apply_url,"Open role")}${job.apply_url && job.apply_url !== job.detail_url ? externalLink(job.apply_url,"Application form") : ""}${job.browser_capture_available ? `<a class="button button-secondary" href="/api/jobs/${encodeURIComponent(id)}/screenshot" target="_blank" rel="noopener">Last browser capture ${icon("external")}</a>` : ""}${canRecordApplied ? `<button type="button" class="button button-secondary" data-action="applied-elsewhere" data-id="${escape(id)}">Already applied${icon("check")}</button>` : ""}${!hideJobActions && !isSubmitted && status !== "dismissed" ? `<button type="button" class="text-button" data-action="dismiss" data-id="${escape(id)}">Dismiss role</button>` : ""}</div></div></article>`;
  }
  function renderReadiness() {
    const profileReady = Boolean(state.profile?.name && state.profile?.email);
    const resumeReady = Boolean(state.resume?.loaded);
    const companies = list(state.rules?.companies);
    const anyCompany = companyScope(state.rules) === "any";
    const rulesReady = (anyCompany || companies.length > 0) && list(state.rules?.allowed_ats).some(ats => ["lever","greenhouse"].includes(ats));
    const approved = Boolean(state.approval?.approved);
    $("#readiness-title").textContent = approved ? "Ready when you are" : "Before your first run";
    $("#readiness-description").textContent = approved ? "Your saved rules are approved. You decide when to put them to work." : "A few essentials keep every application intentional.";
    const steps = [
      {title:"Add your profile & resume",description:profileReady && resumeReady ? "Your application essentials are saved." : "Give every application a solid foundation.",done:profileReady && resumeReady,view:"profile"},
      {title:"Choose your preferences",description:rulesReady ? anyCompany ? "Any fetched company, within your job preferences." : `${companies.length} selected ${companies.length === 1 ? "company" : "companies"}.` : "Choose your preferences and enable supported applications.",done:rulesReady,view:"rules"},
      {title:"Review & approve",description:approved ? "Approved for applications within your rules." : "Give explicit approval before anything runs.",done:approved,view:"rules"}
    ];
    $("#readiness-steps").innerHTML = steps.map((step,index) => `<button type="button" class="readiness-step" data-view="${step.view}"><span class="step-number ${step.done ? "done" : ""}">${step.done ? icon("check") : String(index+1).padStart(2,"0")}</span><span class="step-copy"><strong>${step.title}</strong><small>${escape(step.description)}</small></span><span class="step-arrow">↗</span></button>`).join("");
  }
  function companyScope(rules = {}) {
    return rules.company_scope || (list(rules.companies).length ? "selected" : "any");
  }
  function exactLines(value) {
    return [...new Set(String(value || "").split(/\r?\n/).map(item => item.trim()).filter(Boolean))];
  }
  function locationMode(locations) {
    const values = list(locations).map(value => value.toLowerCase().replace(/[^a-z]/g,""));
    if (!values.length) return "any";
    if (values.every(value => ["unitedstates","unitedstatesofamerica","usa","us"].includes(value))) return "us";
    if (values.length === 1 && values[0] === "remote") return "remote";
    return "custom";
  }
  function renderRuleForm() {
    const form = $("#rules-form");
    const selected = form.elements.company_scope.value === "selected";
    $("#selected-companies-field").hidden = !selected;
    form.elements.companies.disabled = !selected;
    form.elements.companies.required = selected;
    const custom = form.elements.location_mode.value === "custom";
    $("#custom-locations-field").hidden = !custom;
    form.elements.locations.disabled = !custom;
    form.elements.locations.required = custom;
    const capped = form.elements.daily_limit_enabled.checked;
    $("#daily-limit-field").hidden = !capped;
    form.elements.daily_limit.disabled = !capped;
    form.elements.daily_limit.required = capped;
    const experience = form.elements.max_required_experience_months;
    const location = form.elements.location_mode;
    $("#rules-preview").textContent = `${selected ? "Selected companies" : "Any fetched company"} · ${location.selectedOptions[0]?.textContent || "Any location"} · ${experience.selectedOptions[0]?.textContent || "Any experience level"} · ${capped ? `${form.elements.daily_limit.value || "Choose a cap"} per day` : "No daily cap"}`;
  }
  function bulkQueueControl(snapshot, ready, busy = false, unsavedRules = false) {
    const cutoff = snapshot.rules?.min_score ?? 60;
    return {disabled:!ready || busy,
      label:busy ? "Loading jobs to queue…" : `Load jobs to queue (${snapshot.queue?.loadable ?? 0})`,
      help:!ready ? "Loading saved jobs…" : `${unsavedRules ? "You have unsaved rule edits. This uses your SAVED " : "Uses your saved "}score cutoff of ${cutoff}+. Loads every available matching job across the fetched list. ${unsavedRules ? "Save preferences to use a different cutoff. " : ""}Loading selects jobs; it does not start applications.`};
  }
  function attemptBudgetAvailable(queue = {}) {
    return queue.remaining_today === null || queue.remaining_today > 0;
  }
  function dailyLimitValue(form) {
    return form.elements.daily_limit_enabled.checked ? Number(form.elements.daily_limit.value) : null;
  }
  function queueControlState(snapshot, unsaved = false) {
    const approved = snapshot.approval?.approved === true && !unsaved;
    const running = snapshot.automation?.running === true;
    const ready = snapshot.queue?.ready ?? 0;
    return {
      approved, running, ready,
      canRun: approved && snapshot.queue?.can_run === true && !running,
      label: running ? "Run in progress" : !approved ? "Review & start queue" : ready ? "Run approved queue" : "Review queue blockers",
      needsReview: !approved || snapshot.queue?.can_run !== true,
    };
  }
  function setupDestination(snapshot) {
    if (!snapshot.profile?.name || !snapshot.profile?.email) return {view:"profile",selector:'#profile-form [name="name"]'};
    if (!snapshot.resume?.loaded) return {view:"profile",selector:"#resume-file"};
    const companies = list(snapshot.rules?.companies);
    if (companyScope(snapshot.rules) === "selected" && !companies.length) return {view:"rules",selector:'#rules-form [name="companies"]'};
    if (!list(snapshot.rules?.allowed_ats).some(ats => ["lever","greenhouse"].includes(ats))) return {view:"rules",selector:'#rules-form [name="allowed_ats"]'};
    return {view:"rules",selector:snapshot.approval?.approved ? '#rules-form [name="min_score"]' : "#approval-acknowledge"};
  }
  function openQueueReview() {
    if (!dirty.profile && !dirty.rules && !list(state.setup_problems).length && state.approval?.approved && !state.queue?.queued) {
      setView("queue");
      showNotice("Your selected queue is empty. Add a role individually, or choose Load jobs to queue for jobs that meet your score cutoff.");
      return;
    }
    const target = dirty.profile ? {view:"profile",selector:'#profile-form button[type="submit"]'} : dirty.rules ? {view:"rules",selector:'#rules-form button[type="submit"]'} : setupDestination(state);
    setView(target.view);
    const field = $(target.selector);
    const details = field?.closest("details");
    if (details) details.open = true;
    field?.scrollIntoView({block:"center",behavior:"smooth"});
    field?.focus({preventScroll:true});
  }
  function renderQueueStatus() {
    const queue = state.queue || {};
    const control = queueControlState(state,dirty.profile || dirty.rules);
    const problems = list(queue.blockers);
    const setup = list(state.setup_problems);
    $("#queue-run-title").textContent = control.running ? "Your queue is running" : setup.length ? "Finish your application setup" : !control.approved ? "Review your rules to start" : control.ready ? `${control.ready} role${control.ready === 1 ? " is" : "s are"} ready to apply` : "No eligible applications yet";
    $("#queue-run-description").textContent = control.running ? "The live worker panel below shows the employer and actual browser step. Questions appear in Activity." : setup.length ? "Complete the missing settings, then select the jobs you want." : !control.approved ? "Approval authorizes the saved profile, resume and answers. Saving a change resets it. Add jobs to your queue, then review and approve those details to run." : control.ready ? "Only selected queued jobs run. Scheduling can select future matching jobs when you explicitly enable it." : "Add a role using its Add to queue button. Held roles need their listed questions or requirements resolved before running.";
    const visibleProblems = dirty.profile || dirty.rules ? ["Save your profile or rule changes before approving.",...problems] : problems;
    $("#queue-blockers").innerHTML = visibleProblems.map(problem => `<li>${escape(problem)}</li>`).join("");
    $("#queue-blockers").hidden = !visibleProblems.length || control.running;
    $("#queue-health").textContent = loaded ? `${queue.queued ?? 0} selected · ${queue.ready ?? 0} ready · ${queue.blocked ?? 0} held · ${queue.loadable ?? 0} available above score cutoff · ${queue.remaining_today === null ? "No daily cap" : `${queue.remaining_today ?? "—"} attempts left today (UTC)`}` : "Loading queue checks…";
    $("#queue-review-button").textContent = setup.length ? "Complete setup →" : control.approved ? "Review rules →" : "Review & approve →";
    $("#queue-review-button").disabled = control.running;
  }
  function elapsedLabel(value) {
    const stamp = dateObject(value);
    if (!stamp) return "not recorded";
    const seconds = Math.max(0,Math.floor((Date.now()-stamp.getTime())/1000));
    return seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds/60)}m ago`;
  }
  function progressPresentation(run = {}, disconnected = false) {
    const active = ["running","stopping"].includes(run.status);
    const statuses = {running:"Working",stopping:"Stopping",completed:"Finished",paused:"Paused",failed:"Failed",interrupted:"Interrupted",idle:"Idle"};
    return {active,label:disconnected ? "Connection lost" : active && !run.worker_alive ? "Worker update delayed" : statuses[run.status] || "Idle",
      title:disconnected ? "Showing the last received worker status" : run.active_job ? `${active ? "Working on" : "Last application"}: ${run.active_job.company}` : active ? "Starting the worker" : "No active application"};
  }
  function renderProgress() {
    const run = state.run || {};
    const view = progressPresentation(run,connectionLost);
    $("#worker-title").textContent = view.title;
    $("#worker-state").textContent = view.label;
    $("#worker-panel").classList.toggle("is-running",view.active && !connectionLost && run.worker_alive === true);
    $("#worker-panel").classList.toggle("is-offline",connectionLost);
    $("#worker-mode").textContent = run.mode === "demo" ? " · LOCAL DEMO" : "";
    $("#stop-run-button").hidden = !view.active;
    $("#stop-run-button").disabled = connectionLost || run.status === "stopping" || busyActions.has("stop-run");
    if (!run.id) {
      $("#worker-content").innerHTML = `<p>No application is being worked on. Add roles to your queue, approve your saved details, then run.</p><p class="worker-sync">${connectionLost ? "The local server is not responding. Start Jobot or refresh status." : "Connected to the local server."}</p>${list(state.answer_memory?.pending).length ? '<button type="button" class="text-button" data-action="open-questions">Answer questions from the previous run →</button>' : ""}`;
      return;
    }
    const job = list(state.jobs).find(item => item.id === run.active_job?.id);
    const counts = Number.isInteger(run.field_count) ? `<span>${run.completed_fields ?? 0} / ${run.field_count} fields filled</span>` : "";
    const pending = list(state.answer_memory?.pending).filter(item => item.job_id === run.active_job?.id).length;
    const timeline = list(run.timeline).slice(-7).map(item => `<li><time>${escape(fullDate(item.at))}</time><span>${escape(item.message)}</span></li>`).join("");
    const timelineOpen = $("#worker-content details")?.open === true;
    const capture = job?.browser_capture_available ? `<a href="/api/jobs/${encodeURIComponent(job.id)}/screenshot" class="text-button" target="_blank" rel="noopener">View last browser capture ↗</a>` : "";
    $("#worker-content").innerHTML = `${run.active_job ? `<p class="worker-role">${escape(run.active_job.title)}</p>` : ""}<div class="worker-current-step">${icon("activity")}<strong>${escape(run.message || "Waiting for an observed worker step")}</strong></div><div class="worker-counts">${counts}<span>${run.processed ?? 0} / ${run.total ?? 0} selected roles processed</span><span>${run.submitted ?? 0} confirmed · ${run.needs_attention ?? 0} need review · ${run.failed ?? 0} failed · ${run.uncertain ?? 0} uncertain</span></div><p class="worker-sync">${connectionLost ? `Connection lost. Last server contact ${elapsedLabel(new Date(lastSyncAt).toISOString())}. This is a saved status, not proof that the worker is still running.` : `${view.active ? `Worker heartbeat ${escape(elapsedLabel(run.heartbeat_at))}. ` : ""}Last observed step ${escape(elapsedLabel(run.updated_at))}.`}</p>${run.status === "stopping" ? '<p class="worker-sync">Stopping before the next submission. An application already sent cannot be undone.</p>' : ""}${job?.error ? `<p class="worker-error">${escape(job.error)}</p>` : ""}${pending ? `<button type="button" class="button button-secondary" data-action="open-questions" data-id="${escape(job.id)}">Review ${pending} question${pending === 1 ? "" : "s"} in Activity</button>` : ""}${capture}<details class="worker-timeline" ${timelineOpen ? "open" : ""}><summary>Observed steps from this run</summary><ol>${timeline || '<li>No browser step has been reported yet.</li>'}</ol></details>`;
  }
  function renderActivityQuestions() {
    const pending = list(state.answer_memory?.pending);
    const saved = list(state.answer_memory?.saved);
    const signature = JSON.stringify([pending,saved]);
    if (signature === lastInboxSignature) return;
    lastInboxSignature = signature;
    $("#activity-question-count").textContent = pending.length;
    $("#nav-activity-questions").textContent = pending.length;
    $("#nav-activity-questions").hidden = !pending.length;
    const optionalOpen = $("#activity-question-list details")?.open === true;
    const questionCard = item => {
      const key = `${item.job_id}:${item.question}`;
      const optionSignature = JSON.stringify([item.type,item.options]);
      const storedDraft = questionDrafts.get(key);
      const draft = storedDraft?.signature === optionSignature ? storedDraft.value : "";
      const options = item.type === "checkbox" ? [{label:"Yes",value:"true"},{label:"No",value:"false"}] : list(item.options);
      const multiple = /(?:mark|select|choose) all (?:that )?apply/i.test(item.question);
      const unavailableChoice = ["select","radio"].includes(item.type) && (!options.length || multiple);
      let controls;
      if (unavailableChoice) controls = `<p class="question-unavailable">${multiple ? "This is a multiple-choice control the automatic worker cannot answer yet." : "The form did not expose supported choices for this control."} ${item.required ? "Open the employer form to review it manually." : "This question is optional and can be left blank."}</p>`;
      else if (options.length && options.length <= 6) controls = `<fieldset class="question-options"><legend>Choose your answer</legend>${options.map((option,index) => `<label><input type="radio" name="choice" value="${index}" ${draft === String(index) ? "checked" : ""} required><span>${escape(option.label)}</span></label>`).join("")}</fieldset>`;
      else if (options.length) controls = `<label class="field">Your answer<select name="choice" required><option value="">Choose an available answer</option>${options.map((option,index) => `<option value="${index}" ${draft === String(index) ? "selected" : ""}>${escape(option.label)}</option>`).join("")}</select></label>`;
      else controls = `<label class="field">Your answer<textarea name="answer" rows="2" maxlength="4000" required placeholder="Enter an accurate answer to this question">${escape(draft)}</textarea></label>`;
      return `<form class="activity-answer-form" data-job-id="${escape(item.job_id)}" data-question="${escape(item.question)}" data-options-signature="${escape(optionSignature)}"><div class="answer-context">${escape(item.company)} · ${escape(item.title)} · ${item.required ? "Required" : "Optional"}</div><h3>${escape(item.question)}</h3>${controls}<p class="field-hint">Saved for this exact question and shown in Rules. Review the updated approval before running again; confirming does not retry or submit the application.</p>${unavailableChoice ? externalLink(list(state.jobs).find(job => job.id === item.job_id)?.apply_url,"Open employer form") : `<button type="submit" class="button button-primary">Confirm &amp; remember answer ${icon("check")}</button>`}</form>`;
    };
    const required = pending.filter(item => item.required);
    const optional = pending.filter(item => !item.required);
    $("#activity-question-count").textContent = `${required.length} required · ${optional.length} optional`;
    $("#activity-question-list").innerHTML = pending.length ? required.map(questionCard).join("") + (optional.length ? `<details class="optional-form-questions" ${optionalOpen ? "open" : ""}><summary>${optional.length} optional questions — you can leave these blank</summary>${optional.map(questionCard).join("")}</details>` : "") : '<p class="answer-empty">No unanswered application questions. New questions will appear here when a form needs your input.</p>';
    $("#activity-saved-answer-list").innerHTML = `<details class="saved-answer-review"><summary>Confirmed answers (${saved.length})</summary>${saved.map((item,index) => `<div class="saved-question-row"><div><strong>${escape(item.question)}</strong><p>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}</p></div><button type="button" class="text-button" data-action="edit-answer" data-index="${index}">Edit</button></div>`).join("") || '<p class="answer-empty">No answers confirmed yet.</p>'}</details>`;
    $("#rules-answer-list").innerHTML = saved.length ? saved.map(item => `<div class="saved-question-row"><div><strong>${escape(item.question)}</strong><p>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}</p></div></div>`).join("") : '<p class="answer-empty">No confirmed answers yet. Answer employer questions in Activity as they appear.</p>';
  }
  function renderControls() {
    const approved = Boolean(state.approval?.approved);
    const running = Boolean(state.automation?.running);
    const enabled = Boolean(state.automation?.enabled);
    $("#profile-status").textContent = "";
    $("#profile-status").innerHTML = `<span class="status-dot"></span>${approved ? "Rules approved" : "Approval needed"}`;
    $("#profile-status").classList.toggle("ready",approved);
    $("#automation-status").innerHTML = `<span class="status-dot"></span>${running ? "Run in progress" : enabled ? "Automation on" : "Automation off"}`;
    $("#automation-status").classList.toggle("enabled",enabled || running);
    const bulk = bulkQueueControl(state,loaded,busyActions.has("queue-matching"),dirty.rules);
    $("#queue-matching-button").disabled = bulk.disabled;
    $("#queue-matching-button").textContent = bulk.label;
    $("#queue-matching-button").title = bulk.help;
    $("#bulk-queue-help").textContent = bulk.help;
    const control = queueControlState(state,dirty.profile || dirty.rules);
    $("#run-button").disabled = !loaded || running || busyActions.has("run");
    $("#run-button").title = control.canRun ? "Run eligible applications once; scheduling stays separate" : "Review your setup and the visible queue blockers";
    if (!busyActions.has("run")) $("#run-button").innerHTML = `${icon(running ? "refresh" : control.canRun ? "play" : "arrow")}${control.label}`;
    $("#run-button").classList.toggle("busy",running || busyActions.has("run"));
    $("#fetch-button").disabled = busyActions.has("fetch") || running || Boolean(state.automation?.fetching);
    $("#automation-toggle").disabled = (!approved && !enabled) || busyActions.has("automation");
    $("#automation-toggle").setAttribute("aria-checked",String(enabled));
    $("#automation-toggle").setAttribute("aria-label",enabled ? "Disable scheduled applications" : "Enable scheduled applications");
    $("#automation-card-label").textContent = running ? "A run is in progress" : enabled ? "Schedule is on" : "Schedule is off";
    $("#automation-card-detail").textContent = enabled ? `Runs every ${state.rules?.interval_minutes || "—"} minutes while Jobot is open.` : approved ? "Enable for recurring runs. Run once works separately." : list(state.setup_problems)[0] || "Review and approve your saved rules first.";
    $("#automation-setup").hidden = approved;
    const reviewReady = Boolean(approvalReview?.fingerprint);
    const reviewStale = reviewReady && approvalReview.fingerprint !== state.approval?.candidate_fingerprint;
    if (reviewStale) $("#approval-acknowledge").checked = false;
    const ack = $("#approval-acknowledge").checked;
    const setupProblems = list(state.setup_problems);
    const approveDisabled = !ack || !reviewReady || reviewStale || approved || dirty.profile || dirty.rules || setupProblems.length > 0 || busyActions.has("approve") || running;
    $("#approve-button").disabled = approveDisabled;
    $("#approve-run-button").disabled = (approved ? running || dirty.profile || dirty.rules || busyActions.has("approve") : approveDisabled) || !state.queue?.ready || setupProblems.length > 0 || !attemptBudgetAvailable(state.queue);
    $("#approve-run-button").innerHTML = `${approved ? "Run approved queue once" : "Approve &amp; run once"}${icon("play")}`;
    $("#approval-run-help").textContent = setupProblems.length ? "Complete the missing settings above to unlock approval." : !state.queue?.queued ? "Choose jobs using Add to queue first. You can save approval separately." : !state.queue?.ready ? "Your selected roles are held. Answer their questions in Activity or review their checks." : !attemptBudgetAvailable(state.queue) ? "Today's attempt limit has been reached (UTC)." : "This runs your selected eligible roles once. Scheduled applications stay a separate choice.";
    $("#approval-blockers").hidden = !setupProblems.length;
    $("#approval-blockers").innerHTML = `<strong>Complete these before approval</strong><ul>${setupProblems.map(problem => `<li>${escape(problem)}</li>`).join("")}</ul><button type="button" class="text-button" data-action="review-queue">Go to the next missing setting →</button>`;
    $("#approval-title").textContent = reviewStale ? "This review is out of date" : approved ? "Your rules are approved" : "Review before you run";
    $("#approval-status").textContent = reviewStale ? "Saved configuration changed. Refresh the saved review, check its details, then acknowledge again." : dirty.profile || dirty.rules ? "Save your pending changes before approving." : !reviewReady ? "Waiting for a saved configuration snapshot from the local server." : approved ? `Approved ${fullDate(state.approval.approved_at)}. Changes require a new approval.` : "This review stays fixed while Application rules is open.";
    $("#approval-acknowledge").disabled = approved || reviewStale || !reviewReady;
    renderQueueStatus();
  }
  function renderSummary(force = false) {
    const fingerprint = state.approval?.candidate_fingerprint || null;
    // Freeze the review while it is visible. Polling may discover a new saved
    // configuration, but only an explicit refresh can replace these details.
    if (force || !approvalReview?.fingerprint || currentView !== "rules") {
      if (approvalReview?.fingerprint !== fingerprint) $("#approval-acknowledge").checked = false;
      approvalReview = JSON.parse(JSON.stringify({fingerprint,profile:state.profile || {},resume:state.resume || {},rules:state.rules || {}}));
    }
    const rules = approvalReview.rules;
    const profile = approvalReview.profile;
    const companies = list(rules.companies);
    const selected = companyScope(rules) === "selected";
    const fields = [["Applicant",profile.name || "Not saved"],["Resume",approvalReview.resume.loaded ? approvalReview.resume.filename || "Uploaded resume" : "Not uploaded"],["Company scope",selected ? `${companies.length} selected companies` : "Any company from fetched jobs, including future matches"],["Location",list(rules.locations).length ? locationMode(rules.locations) === "us" ? "Anywhere in the U.S." : list(rules.locations).join(", ") : "Any location"],["Experience required",experienceLimit(rules) === null ? "Any level" : experienceLimit(rules) === 0 ? "None" : `Up to ${experienceLimit(rules)/12} year${experienceLimit(rules) === 12 ? "" : "s"}`],["New grads first",rules.prefer_new_grad ? "Yes" : "No"],["Graduation windows",rules.enforce_graduation_window === false ? "Not enforced; actual dates used" : "Enforced"],["Supported forms",list(rules.allowed_ats).join(", ") || "Disabled"],["Minimum match",`${rules.min_score ?? "—"} / 100`],["Daily cap",rules.daily_limit == null ? "Unlimited — all selected eligible jobs" : `${rules.daily_limit} attempts (UTC day)`],["Schedule interval",`${rules.interval_minutes ?? "—"} minutes`],["Unclear eligibility",rules.require_known_eligibility === false ? "Review not required" : "Pause for review"]];
    const applicantFields = [["Legal first name",profile.first_name],["Legal last name",profile.last_name],["Preferred name",profile.preferred_first_name],["Email",profile.email],["Phone",profile.phone],["Current location",profile.location],["Approved work locations",list(profile.approved_work_locations).join("; ")],["LinkedIn",profile.linkedin],["GitHub",profile.github],["Portfolio",profile.portfolio],["Skills",list(profile.skills).join(", ")],["Experience months",profile.experience_months],["Graduation",profile.graduation_date || profile.graduation_month],["Work authorized",profile.work_authorized == null ? "Not provided" : profile.work_authorized ? "Yes" : "No"],["Sponsorship needed",profile.needs_sponsorship == null ? "Not provided" : profile.needs_sponsorship ? "Yes" : "No"]];
    const applicantReview = applicantFields.map(([key,value]) => `<div class="approval-row"><span>${key}</span><strong>${escape(value ?? "Not provided")}</strong></div>`).join("");
    const answerReview = Object.entries(profile.answers || {}).map(([question,answer]) => `<div class="approval-row"><span>${escape(question)}</span><strong>${escape(typeof answer === "boolean" ? answer ? "Yes" : "No" : answer)}</strong></div>`).join("");
    const stale = Boolean(approvalReview.fingerprint && approvalReview.fingerprint !== fingerprint);
    $("#approval-summary").innerHTML = fields.map(([key,value]) => `<div class="approval-row"><span>${key}</span><strong>${escape(value)}</strong></div>`).join("") + (selected && companies.length ? `<div class="approval-companies">Companies: ${escape(companies.join(" · "))}</div>` : "") + `<details class="approval-companies"><summary>Applicant details</summary>${applicantReview}</details>` + `<details class="approval-companies"><summary>Reusable answers (${Object.keys(profile.answers || {}).length})</summary>${answerReview || "No saved reusable answers."}</details>` + `<div class="approval-companies">${stale ? "Saved configuration has changed. The details above have been preserved for your review." : "Review the saved details above before acknowledging."}</div><button type="button" class="text-button" data-action="review-refresh">Refresh saved review ${icon("refresh")}</button>`;
  }
  function renderAnswerMemory() {
    const memory = state.answer_memory || {pending:[],saved:Object.entries(state.profile?.answers || {}).map(([question,answer]) => ({question,answer}))};
    const signature = JSON.stringify(memory);
    if (signature === lastMemorySignature) return;
    lastMemorySignature = signature;
    const pending = list(memory.pending);
    const saved = list(memory.saved);
    $("#memory-count").textContent = `${saved.length} saved`;
    $("#pending-answer-count").textContent = pending.length;
    $("#nav-memory-count").textContent = pending.length || saved.length;
    $("#pending-answers").innerHTML = pending.length ? pending.map((item,index) => `<article class="answer-item"><div class="answer-context">${escape(item.company)} · ${escape(item.title)}${item.required ? " · Required" : " · Optional"}</div><h3>${escape(item.question)}</h3>${item.reason ? `<p>${escape(item.reason)}</p>` : ""}<button type="button" class="text-button" data-action="answer-question" data-index="${index}">Answer & remember →</button></article>`).join("") : '<p class="answer-empty">No new questions yet. When a supported form needs an unfamiliar answer, it will appear here.</p>';
    $("#saved-answers").innerHTML = saved.length ? saved.map((item,index) => `<article class="answer-item saved-answer"><h3>${escape(item.question)}</h3><p>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}</p><div class="detail-actions"><button type="button" class="text-button" data-action="edit-answer" data-index="${index}">Edit answer</button><button type="button" class="text-button" data-action="forget-answer" data-index="${index}">Forget</button></div></article>`).join("") : '<p class="answer-empty">No reusable answers saved. Add one below or answer a question found during an application.</p>';
  }
  function resetAnswerForm() {
    const form = $("#answer-memory-form");
    form.reset();
    form.elements.job_id.value = "";
    form.elements.question.readOnly = false;
    form.elements.answer.required = true;
    form.elements.answer.disabled = false;
    form.elements.choice.disabled = true;
    form.elements.choice.required = false;
    form.dataset.kind = "text";
    $("#memory-text-field").hidden = false;
    $("#memory-choice-field").hidden = true;
    $("#memory-form-context").hidden = true;
    $("#memory-form-title").textContent = "Add a reusable answer";
  }
  function editAnswer(item, pending = false) {
    resetAnswerForm();
    setView("profile");
    const form = $("#answer-memory-form");
    form.elements.question.value = item.question;
    form.elements.question.readOnly = true;
    form.elements.job_id.value = pending ? item.job_id : "";
    form.dataset.kind = item.type || (typeof item.answer === "boolean" ? "checkbox" : "text");
    const choice = ["checkbox","radio","select"].includes(form.dataset.kind);
    if (choice) {
      const options = form.dataset.kind === "checkbox" ? [{label:"Yes",value:"true"},{label:"No",value:"false"}] : list(item.options);
      form.elements.choice.innerHTML = '<option value="">Choose your answer</option>' + options.map(option => `<option value="${escape(option.value)}">${escape(option.label)}</option>`).join("");
      form.elements.choice.disabled = false;
      form.elements.choice.required = true;
      form.elements.answer.required = false;
      form.elements.answer.disabled = true;
      if (item.answer !== undefined) form.elements.choice.value = String(item.answer);
    } else {
      form.elements.answer.value = item.answer ?? "";
    }
    $("#memory-text-field").hidden = choice;
    $("#memory-choice-field").hidden = !choice;
    $("#memory-form-title").textContent = pending ? "Answer a new question" : "Update saved answer";
    $("#memory-form-context").textContent = pending ? `Found at ${item.company}: ${item.title}. Saving does not retry this application. Review it and add it to the queue after answering.` : "This updates the answer reused across future applications with the same exact question.";
    $("#memory-form-context").hidden = false;
    form.scrollIntoView({block:"center",behavior:"smooth"});
    (choice ? form.elements.choice : form.elements.answer).focus({preventScroll:true});
  }
  function renderActivity() {
    const events = list(state.events);
    const signature = JSON.stringify(events);
    if (signature === lastEventSignature) return;
    lastEventSignature = signature;
    if (!events.length) {
      $("#activity-list").innerHTML = `<div class="simple-empty">${icon("activity")}<h3>A fresh start</h3><p>Your workspace activity will appear here as you configure your search.</p></div>`;
      return;
    }
    $("#activity-list").innerHTML = [...events].sort((a,b) => (dateObject(b.created_at)?.getTime() || 0) - (dateObject(a.created_at)?.getTime() || 0)).map(event => {
      const error = /error|fail|block/i.test(event.type || "");
      return `<div class="activity-item ${error ? "error" : ""}"><div class="activity-symbol">${icon(error ? "info" : /submit|approv|sav/i.test(event.type || "") ? "check" : "activity")}</div><div class="activity-content"><div class="activity-type">${escape(String(event.type || "Workspace update").replaceAll("_"," "))}</div><p>${escape(event.message)}</p></div><time class="activity-time" ${dateObject(event.created_at) ? `datetime="${escape(event.created_at)}"` : ""}>${escape(fullDate(event.created_at))}</time></div>`;
    }).join("");
  }
  function hydrateForms() {
    const profileForm = $("#profile-form");
    const rulesForm = $("#rules-form");
    if (!dirty.profile && (!profileForm.contains(document.activeElement) || !initialized.profile)) {
      const profile = state.profile || {};
      ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company","experience_months","graduation_date","graduation_month"].forEach(name => { profileForm.elements[name].value = profile[name] ?? ""; });
      ["work_authorized","needs_sponsorship"].forEach(name => { profileForm.elements[name].value = profile[name] === true ? "true" : profile[name] === false ? "false" : ""; });
      profileForm.elements.skills.value = list(profile.skills).join("\n");
      profileForm.elements.approved_work_locations.value = list(profile.approved_work_locations).join("\n");
      profileForm.elements.answers.value = profile.answers && Object.keys(profile.answers).length ? JSON.stringify(profile.answers,null,2) : "";
      initialized.profile = true;
    }
    if (!dirty.rules && (!rulesForm.contains(document.activeElement) || !initialized.rules)) {
      const rules = state.rules || {};
      rulesForm.elements.companies.value = list(rules.companies).join("\n");
      rulesForm.elements.company_scope.value = companyScope(rules);
      rulesForm.elements.location_mode.value = locationMode(rules.locations);
      for (const name of ["max_required_experience_months","interval_minutes"]) {
        const control = rulesForm.elements[name];
        const value = rules[name] ?? "";
        if (![...control.options].some(option => option.value === String(value))) {
          const option = document.createElement("option");
          option.value = String(value);
          option.textContent = name === "interval_minutes" ? `${value} minutes` : `${value} months`;
          control.append(option);
        }
      }
      rulesForm.elements.locations.value = list(rules.locations).join("\n");
      ["min_score","interval_minutes"].forEach(name => { if (rules[name] !== undefined && rules[name] !== null) rulesForm.elements[name].value = rules[name]; });
      rulesForm.elements.daily_limit_enabled.checked = rules.daily_limit != null;
      rulesForm.elements.daily_limit.value = rules.daily_limit ?? "";
      rulesForm.elements.max_required_experience_months.value = rules.max_required_experience_months ?? "";
      rulesForm.elements.prefer_new_grad.checked = rules.prefer_new_grad === true;
      rulesForm.elements.enforce_graduation_window.checked = rules.enforce_graduation_window !== false;
      rulesForm.elements.require_known_eligibility.checked = rules.require_known_eligibility !== false;
      rulesForm.elements.allowed_ats.checked = list(rules.allowed_ats).some(ats => ["lever","greenhouse"].includes(ats));
      $("#score-output").value = rulesForm.elements.min_score.value;
      initialized.rules = true;
    }
    renderRuleForm();
  }
  function renderState() {
    const jobs = list(state.jobs);
    const stats = state.stats || {};
    $("#stat-total").textContent = formatNumber(stats.total ?? jobs.length);
    $("#stat-queued").textContent = formatNumber(state.queue?.ready ?? 0);
    $("#stat-queue-caption").textContent = `${state.queue?.queued ?? stats.queued ?? 0} selected · ${state.queue?.blocked ?? 0} held by rules`;
    $("#stat-submitted").textContent = formatNumber(stats.submitted ?? jobs.filter(job => job.status === "submitted").length);
    $("#stat-attention").textContent = formatNumber(stats.needs_attention ?? jobs.filter(jobAttention).length);
    $("#nav-queue-count").textContent = formatNumber(state.queue?.pending ?? stats.queued ?? 0);
    $("#resume-status").textContent = state.resume?.loaded ? state.resume.filename || "Resume uploaded" : "No resume uploaded";
    $("#resume-status").classList.toggle("loaded",Boolean(state.resume?.loaded));
    const discoveryMessage = sourceMessage || (state.discovery?.count ? `Last fetch: ${state.discovery.count} listings · ${state.discovery.verified_destinations} verified supported links. Queue counts above reflect your current selection.` : "");
    $("#source-message").textContent = discoveryMessage;
    $("#source-message").hidden = !discoveryMessage;
    hydrateForms();
    renderSummary();
    renderControls();
    renderReadiness();
    renderJobs();
    renderAnswerMemory();
    renderActivity();
    renderProgress();
    renderActivityQuestions();
  }
  function refresh(manual = false) {
    if (refreshing) return manual ? refreshInFlight.then(() => refresh(true)) : refreshInFlight;
    refreshing = true;
    clearTimeout(pollTimer);
    refreshInFlight = (async () => {
      try {
        const next = await api("/api/state");
        const wasApproved = Boolean(state.approval?.approved);
        state = {...state,...next};
        lastSyncAt = Date.now();
        connectionLost = false;
        loaded = true;
        if (wasApproved && !state.approval?.approved) $("#approval-acknowledge").checked = false;
        renderState();
        return true;
      } catch (error) {
        connectionLost = true;
        renderProgress();
        if (!loaded || manual) showNotice(error.message,true);
        return false;
      } finally {
        refreshing = false;
        pollTimer = setTimeout(() => refresh(),state.automation?.running ? 1000 : 5000);
      }
    })();
    return refreshInFlight;
  }
  function markDirty(name) {
    dirty[name] = true;
    $(`#${name}-dirty`).hidden = false;
    $("#approval-acknowledge").checked = false;
    renderControls();
  }
  function markSaved(name) {
    dirty[name] = false;
    $(`#${name}-dirty`).hidden = true;
    $("#approval-acknowledge").checked = false;
  }
  document.addEventListener("input",event => {
    const form = event.target.closest(".activity-answer-form");
    if (!form) return;
    const value = form.elements.choice?.value ?? form.elements.answer?.value ?? "";
    questionDrafts.set(`${form.dataset.jobId}:${form.dataset.question}`,{value,signature:form.dataset.optionsSignature});
  });
  document.addEventListener("submit",event => {
    const form = event.target.closest(".activity-answer-form");
    if (!form) return;
    event.preventDefault();
    if (dirty.profile) { showNotice("Save or discard your unsaved profile changes before confirming an application answer.",true); return; }
    const item = list(state.answer_memory?.pending).find(candidate => candidate.job_id === form.dataset.jobId && candidate.question === form.dataset.question);
    if (!item) { showNotice("This question changed. Refresh Activity before confirming an answer.",true); return; }
    if (form.dataset.optionsSignature !== JSON.stringify([item.type,item.options])) { showNotice("The available choices changed. Review them and choose your answer again.",true); return; }
    let answer;
    if (form.elements.choice) {
      const selected = Number(form.elements.choice.value);
      const options = item.type === "checkbox" ? [{value:true},{value:false}] : list(item.options);
      if (form.elements.choice.value === "" || !Number.isInteger(selected) || !options[selected]) return;
      answer = options[selected].value;
    } else answer = form.elements.answer.value.trim();
    action(`question-${item.job_id}-${item.id || item.question}`,form.querySelector('button[type="submit"]'),async () => {
      const result = await post("/api/answers",{question:item.question,answer,job_id:item.job_id});
      questionDrafts.delete(`${item.job_id}:${item.question}`);
      return result;
    },"Answer confirmed and remembered for this exact question. It is now shown in Rules → Saved application answers. Review and approve the updated details before running again.");
  });
  document.addEventListener("click", event => {
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) { setView(viewButton.dataset.view); return; }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const name = button.dataset.action;
    const id = button.dataset.id;
    if (name === "review-queue") {
      openQueueReview();
    } else if (name === "queue-matching") {
      action("queue-matching",button,async () => { const result = await post("/api/queue/matching"); filter = "queued"; search = ""; $("#job-search").value = ""; updateFilters(); return result; },result => result.message);
    } else if (name === "stop-run") {
      action("stop-run",button,() => post("/api/run/stop"),"Stop requested before the next submission. Scheduling is off; an already sent application cannot be undone.");
    } else if (name === "refresh-worker") {
      action("refresh-worker",button,() => refresh(true));
    } else if (name === "open-questions") {
      setView("activity");
      const form = id ? [...document.querySelectorAll(".activity-answer-form")].find(item => item.dataset.jobId === id) : null;
      (form || $("#activity-questions")).scrollIntoView({block:"start",behavior:"smooth"});
    } else if (name === "open-memory") {
      setView("profile");
      $("#answer-memory").scrollIntoView({block:"start",behavior:"smooth"});
    } else if (name === "answer-question") {
      const item = list(state.answer_memory?.pending)[Number(button.dataset.index)];
      if (item) editAnswer(item,true);
    } else if (name === "edit-answer") {
      const item = list(state.answer_memory?.saved)[Number(button.dataset.index)];
      if (item) editAnswer(item);
    } else if (name === "forget-answer") {
      const item = list(state.answer_memory?.saved)[Number(button.dataset.index)];
      if (!item) return;
      if (dirty.profile) { showNotice("Save your pending profile changes before editing answer memory.",true); return; }
      action("answer-memory",button,() => api("/api/answers",{method:"DELETE",body:JSON.stringify({question:item.question})}),"Answer removed. Review and approve your updated profile before running applications.");
    } else if (name === "details") {
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      const detail = document.getElementById(button.getAttribute("aria-controls"));
      const open = expanded.has(id);
      button.setAttribute("aria-expanded",String(open));
      button.innerHTML = `${open ? "Less detail" : "Checks & details"}${icon("chevron")}`;
      if (detail) detail.hidden = !open;
    } else if (name === "queue" || name === "unqueue" || name === "dismiss") {
      action(`job-${id}`,button,() => post(`/api/jobs/${encodeURIComponent(id)}/status`,{status:name === "queue" ? "queued" : name === "unqueue" ? "new" : "dismissed"}),name === "queue" ? "Role added to your selected queue. Nothing is submitted until you run with approved details." : name === "unqueue" ? "Role removed from the queue and returned to discovery." : "Role dismissed.");
    } else if (name === "applied-elsewhere") {
      const job = list(state.jobs).find(item => String(item.id) === id);
      if (!job || isDemo(job) || job.attempted_at || ["submitted","applied_elsewhere","active","filling","submitting","applying","uncertain"].includes(String(job.status).toLowerCase())) return;
      const canonical = Boolean(supportedPostingURL(job.apply_url));
      action(`job-${id}`,button,() => post("/api/applied-elsewhere",{job_id:id}),canonical ? "Previous application recorded. Jobot will skip this exact Lever posting." : "Previous application recorded. Listings with the same company and title will be held for review.");
    } else if (name === "review-refresh") {
      action("review-refresh",button,async () => {
        if (!await refresh(true)) throw new Error("Could not refresh the saved review. Check the local server and try again.");
        $("#approval-acknowledge").checked = false;
        renderSummary(true);
        renderControls();
      },"Saved review refreshed. Read the updated details before acknowledging approval.");
    } else if (name === "clear-filter") {
      filter = "all"; search = ""; experienceFilterEnabled = false; $("#job-search").value = ""; updateFilters(); renderJobs(true);
    }
  });
  function updateFilters() { $$("[data-filter]").forEach(button => { const active = button.dataset.filter === filter; button.classList.toggle("active",active); button.setAttribute("aria-pressed",String(active)); }); }
  $$("[data-filter]").forEach(button => button.addEventListener("click",() => { filter = button.dataset.filter; updateFilters(); renderJobs(true); }));
  $("#job-search").addEventListener("input",event => { search = event.target.value.toLowerCase().trim(); renderJobs(true); });
  $("#experience-filter").addEventListener("change",event => { experienceFilterEnabled = event.target.checked; renderJobs(true); });
  $("#notice-close").addEventListener("click",() => { $("#notice").hidden = true; });
  $("#memory-reset").addEventListener("click",resetAnswerForm);
  $("#answer-memory-form").addEventListener("submit",event => {
    event.preventDefault();
    if (dirty.profile) { showNotice("Save your pending profile changes before editing answer memory.",true); return; }
    const form = event.currentTarget;
    if (!form.elements.acknowledge.checked) return;
    const choice = !form.elements.choice.disabled;
    const answer = choice ? form.dataset.kind === "checkbox" ? form.elements.choice.value === "true" : form.elements.choice.value : form.elements.answer.value.trim();
    const body = {question:form.elements.question.value.trim(),answer};
    if (form.elements.job_id.value) body.job_id = form.elements.job_id.value;
    action("answer-memory",$("button[type=submit]",form),async () => { const result = await post("/api/answers",body); resetAnswerForm(); return result; },"Answer remembered locally for this exact question. Review and approve your updated profile before running applications.");
  });
  $("#history-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    const input = $("#history-url");
    const url = supportedPostingURL(input.value.trim());
    if (!url) { showNotice("Enter an exact HTTPS Lever or Greenhouse posting URL, including its posting ID.",true); input.focus(); return; }
    action("history",$("button[type=submit]",form),async () => { await post("/api/applied-elsewhere",{url}); input.value = ""; },"Previous application recorded locally. This exact employer posting will be skipped.");
  });
  $("#profile-form").addEventListener("input",() => markDirty("profile"));
  $("#rules-form").addEventListener("input",() => { markDirty("rules"); $("#score-output").value = $("#min-score").value; renderRuleForm(); });
  $("#approval-acknowledge").addEventListener("change",renderControls);
  $("#profile-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    let answers = {};
    try {
      answers = form.elements.answers.value.trim() ? JSON.parse(form.elements.answers.value) : {};
      if (!answers || Array.isArray(answers) || typeof answers !== "object" || Object.values(answers).some(value => !["string","boolean"].includes(typeof value))) throw new Error();
    } catch { showNotice('Approved answers must be a JSON object of exact questions and text or true/false answers, for example: {"Question?": "Answer"}.',true); form.elements.answers.focus(); return; }
    const body = {};
    ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company"].forEach(name => { body[name] = form.elements[name].value.trim(); });
    body.skills = splitLines(form.elements.skills.value);
    body.approved_work_locations = splitLines(form.elements.approved_work_locations.value);
    body.experience_months = form.elements.experience_months.value === "" ? null : Number(form.elements.experience_months.value);
    body.graduation_date = form.elements.graduation_date.value || null;
    body.graduation_month = form.elements.graduation_month.value || null;
    body.work_authorized = booleanValue(form.elements.work_authorized.value);
    body.needs_sponsorship = booleanValue(form.elements.needs_sponsorship.value);
    body.answers = answers;
    action("profile",$("button[type=submit]",form),async () => { await post("/api/profile",body); markSaved("profile"); },"Profile saved. Review and approve your rules before running applications.");
  });
  $("#rules-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    const selected = form.elements.company_scope.value === "selected";
    const where = form.elements.location_mode.value;
    const locations = where === "us" ? ["United States","USA","U.S.","US"] : where === "remote" ? ["Remote"] : where === "custom" ? exactLines(form.elements.locations.value) : [];
    const body = {company_scope:selected ? "selected" : "any",companies:selected ? exactLines(form.elements.companies.value) : [],company_boards:state.rules?.company_boards || {},locations,allowed_ats:form.elements.allowed_ats.checked ? ["lever","greenhouse"] : [],min_score:Number(form.elements.min_score.value),daily_limit:dailyLimitValue(form),interval_minutes:Number(form.elements.interval_minutes.value),require_known_eligibility:form.elements.require_known_eligibility.checked};
    body.max_required_experience_months = form.elements.max_required_experience_months.value === "" ? null : Number(form.elements.max_required_experience_months.value);
    body.prefer_new_grad = form.elements.prefer_new_grad.checked;
    body.enforce_graduation_window = form.elements.enforce_graduation_window.checked;
    action("rules",$("button[type=submit]",form),async () => { const result = await post("/api/rules",body); markSaved("rules"); return result; },"Preferences saved. Review the updated summary to approve.").then(result => {
      if (!result?.ok) return;
      renderSummary(true);
      renderControls();
      if (currentView === "rules") $("#approval-title").scrollIntoView({block:"start",behavior:"smooth"});
    });
  });
  $("#resume-file").addEventListener("change",event => { $("#resume-file-name").textContent = event.target.files[0]?.name || ""; });
  $("#resume-form").addEventListener("submit",event => {
    event.preventDefault();
    const file = $("#resume-file").files[0];
    if (!file) { showNotice("Choose a resume file first.",true); return; }
    const form = new FormData(); form.append("file",file);
    action("resume",$("button[type=submit]",event.currentTarget),async () => { await post("/api/resume",form); $("#resume-file").value = ""; $("#resume-file-name").textContent = ""; $("#approval-acknowledge").checked = false; },"Resume saved locally. Review your profile and approve your rules before submitting.");
  });
  $("#approval-form").addEventListener("submit",event => {
    event.preventDefault();
    const runAfter = event.submitter?.value === "run";
    const alreadyApproved = state.approval?.approved === true;
    const expectedFingerprint = approvalReview?.fingerprint;
    if (dirty.profile || dirty.rules || (!alreadyApproved && (!$("#approval-acknowledge").checked || !expectedFingerprint || expectedFingerprint !== state.approval?.candidate_fingerprint))) return;
    action("approve",event.submitter || $("#approve-button"),async () => {
      try {
        if (!alreadyApproved) await post("/api/approve",{acknowledge:true,expected_fingerprint:expectedFingerprint});
        if (runAfter) { const result = await post("/api/run"); setView("queue"); return result; }
      } catch (error) { await refresh(true); throw error; }
    },runAfter ? "One-time queue run started. Scheduled applications are a separate setting. Follow progress in Activity." : "Approval saved. You can run the queue once or enable scheduled applications.");
  });
  $("#automation-toggle").addEventListener("click",event => {
    const enabled = !state.automation?.enabled;
    action("automation",event.currentTarget,() => post("/api/automation",{enabled}),enabled ? "Scheduled applications enabled within your approved rules." : "Scheduled applications turned off.");
  });
  $("#fetch-button").addEventListener("click",event => {
    sourceMessage = "Fetching roles from your configured source…"; renderState();
    action("fetch",event.currentTarget,async () => {
      try { const result = await post("/api/fetch"); sourceMessage = result.message || ""; return result; }
      catch (error) { sourceMessage = error.message; throw error; }
      finally { $("#source-message").textContent = sourceMessage; $("#source-message").hidden = !sourceMessage; }
    },result => result.message || "Job fetch completed. Newly discovered roles are ranked by resume fit.");
  });
  $("#run-button").addEventListener("click",event => {
    const control = queueControlState(state,dirty.profile || dirty.rules);
    if (!control.canRun) { openQueueReview(); return; }
    action("run",event.currentTarget,() => post("/api/run"),"One-time queue run started. Follow the results in Activity.");
  });
  $("#refresh-activity").addEventListener("click",event => action("activity",event.currentTarget,() => refresh(true),"Activity refreshed."));
  window.addEventListener("hashchange",() => setView(location.hash.slice(1),false));
  document.addEventListener("visibilitychange",() => { if (!document.hidden) refresh(); });
  $$("svg").forEach(svg => { svg.setAttribute("aria-hidden","true"); svg.setAttribute("focusable","false"); });
  setView(location.hash.slice(1) || "queue",false);
  renderReadiness();
  refresh();
})();
