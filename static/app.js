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
  let lastPlatformSignature = "";
  const questionDrafts = new Map();
  const questionSearches = new Map();
  const questionSources = new Map();
  let currentView = "queue";
  let approvalReview = null;
  let lastMemorySignature = "";
  const dirty = {profile:false,rules:false};
  const initialized = {profile:false,rules:false};
  let profileEditorRevision = null;
  const expanded = new Set();
  const busyActions = new Set();
  const viewNames = {queue:"Application queue",setup:"Application profile",profile:"Profile & resume",rules:"Application rules",activity:"Activity"};
  let onboardingSnapshot = null;
  let onboardingDraft = null;
  let onboardingStep = 0;
  let onboardingDirty = false;
  let onboardingLoading = false;
  let onboardingSaving = false;
  let onboardingConflict = false;
  let onboardingError = "";
  let onboardingStarted = false;
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
    if (Array.isArray(detail)) return detail.map(item => typeof item === "string" ? item : `${Array.isArray(item.loc) ? item.loc.filter(part => part !== "body").join(" → ")+": " : ""}${item.msg || "Invalid value"}`).join("; ");
    if (detail && typeof detail === "object" && Array.isArray(detail.fields)) return `${detail.message || "Check your answers."} ${detail.fields.map(item => `${item.field}: ${item.message}`).join("; ")}`;
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
    if (button) { button.disabled = true; button.classList.add("busy"); button.setAttribute("aria-busy","true"); if (name === "queue-matching") button.textContent = "Loading jobs to queue…"; else if (name.startsWith("platform-")) button.textContent = "Checking public form…"; else if (name.startsWith("questions-")) button.textContent = button.dataset.action === "search-choices" ? "Searching choices…" : "Reading form…"; }
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
    if (view === "setup") {
      if (!onboardingSnapshot && !onboardingLoading) loadOnboarding();
      else renderOnboarding();
    }
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
  function platformMode(support) {
    if (!support || typeof support !== "object") return {label:"Support not checked",className:"unknown"};
    if (support.automatic_submission_supported === true && ["lever","greenhouse"].includes(support.platform)) return {label:"Automatic applications",className:"automatic"};
    return support.can_inspect === true ? {label:"Read-only check · manual application",className:"inspection"} : {label:"Manual application",className:"manual"};
  }
  function canCheckPlatform(job) {
    return !isDemo(job) && job.platform_support?.can_inspect === true && job.platform_check_available !== false && job.closed !== true && !job.attempted_at && !(Number(job.attempts) > 0) && ["new","queued","needs_attention","needs_review","manual_review","held","ineligible","failed"].includes(String(job.status || "new").toLowerCase());
  }
  function platformReportMarkup(report) {
    if (!report || typeof report !== "object") return "";
    const fields = list(report.fields).slice(0,100);
    const gates = list(report.gates);
    const observed = Number.isInteger(report.field_count) && report.field_count >= 0 ? report.field_count : fields.length;
    const stage = String(report.stage || "unknown").replaceAll("_"," ");
    return `<section class="platform-report" aria-label="Last read-only platform check"><div class="platform-report-heading"><strong>Last read-only platform check</strong><span>${escape(fullDate(report.checked_at))}</span></div><p class="platform-stage">Observed stage: <strong>${escape(stage)}</strong></p>${report.message ? `<p>${escape(report.message)}</p>` : ""}${gates.length ? `<ul class="platform-gates">${gates.map(gate => `<li><strong>${escape(String(gate.kind || "Review").replaceAll("_"," "))}</strong> · ${escape(gate.message || "Review this step on the employer site.")}</li>`).join("")}</ul>` : '<p class="platform-check-note">No access gate was observed during this check. Later steps may still need review.</p>'}<details class="platform-fields"><summary>${observed} observed field${observed === 1 ? "" : "s"}${observed > fields.length ? ` · showing ${fields.length}` : ""}</summary>${fields.length ? `<ul>${fields.map(field => `<li><strong>${escape(field.label || "Unlabelled field")}</strong><span>${escape(field.type || "unknown control")} · ${field.required === true ? "Required" : "Optional"}</span>${list(field.options).length ? `<p>Observed choices: ${list(field.options).slice(0,100).map(option => escape(typeof option === "object" && option !== null ? option.label || option.value || "Unlabelled choice" : option)).join(" · ")}${list(field.options).length > 100 ? " · Additional choices omitted" : ""}</p>` : ""}</li>`).join("")}</ul>` : '<p>No form fields were observed on this page.</p>'}</details>${report.next_action ? `<p class="platform-next-action"><strong>Next step</strong> ${escape(report.next_action)}</p>` : ""}<p class="platform-check-note">This check does not fill fields, upload your resume, submit an application or approve your settings.</p></section>`;
  }
  function jobPlatformMarkup(job) {
    if (isDemo(job)) return "";
    const support = job.platform_support;
    const mode = platformMode(support);
    return `<section class="job-platform-support" aria-label="Application platform compatibility"><div class="platform-support-heading"><span class="detail-label">${escape(support?.label || String(job.ats || "Unknown platform"))}</span><span class="platform-mode ${mode.className}">${mode.label}</span></div><p>${escape(support?.reason || "Platform compatibility has not been reported. Open the employer form to review it.")}</p>${support?.next_action ? `<p class="platform-next-action">${escape(support.next_action)}</p>` : ""}${platformReportMarkup(job.platform_check)}</section>`;
  }
  function platformCatalogMarkup(platforms) {
    const entries = list(platforms);
    if (!entries.length) return '<p class="platform-check-note">Platform compatibility will appear after the local server returns its support list.</p>';
    const automatic = entries.filter(item => platformMode(item).className === "automatic").length;
    const inspectable = entries.filter(item => platformMode(item).className === "inspection").length;
    return `<details class="platform-catalog"><summary>${automatic} automatic · ${inspectable} inspection-only · ${entries.length - automatic - inspectable} manual <span>View platform details</span></summary><ul>${entries.map(item => {
      const mode = platformMode(item);
      return `<li><div><strong>${escape(item.label || item.platform || "Unknown platform")}</strong><span class="platform-mode ${mode.className}">${mode.label}</span></div><p>${escape(item.reason || "Open the employer form for manual review.")}</p>${item.next_action ? `<p class="platform-next-action">${escape(item.next_action)}</p>` : ""}</li>`;
    }).join("")}</ul></details>`;
  }
  function renderPlatforms() {
    const target = $("#platform-support-list");
    if (!target) return;
    const signature = JSON.stringify(list(state.platforms));
    if (signature === lastPlatformSignature) return;
    const wasOpen = target.querySelector?.(".platform-catalog")?.open === true;
    target.innerHTML = platformCatalogMarkup(state.platforms);
    const details = target.querySelector?.(".platform-catalog");
    if (details) details.open = wasOpen;
    lastPlatformSignature = signature;
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
    const canRefreshQuestions = canRecordApplied && status !== "dismissed" && Boolean(supportedPostingURL(job.apply_url));
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
      <div class="job-detail" id="${escape(detailId)}" ${open ? "" : "hidden"}>${isDemo(job) ? '<div class="demo-banner">Demo uses a local mock employer. No real employer receives this application.</div>' : ""}${job.error && !["submitted","dismissed","applied_elsewhere"].includes(status) ? `<div class="job-error">${escape(job.error)}</div>` : ""}${submissionInfo ? `<div class="submission-confirmation"><div class="detail-label">Submission confirmation</div><p>${escape(submissionInfo)}</p></div>` : ""}${jobPlatformMarkup(job)}${preflight}
        <details class="fit-explanation"><summary>Score evidence & eligibility</summary><div class="detail-columns"><div><div class="detail-label">Resume matches</div>${evidence(detail.matches)}</div><div><div class="detail-label">Not evidenced</div>${evidence(detail.not_evidenced)}</div></div><div class="detail-label">Eligibility checks</div><div class="eligibility-list">${eligibility || '<p class="detail-empty">Eligibility has not been assessed. Review the role before applying.</p>'}</div>${list(detail.reasons).length ? `<div class="detail-label">Scoring notes</div>${evidence(detail.reasons)}` : ""}${sourceEvidence.length ? `<div class="detail-label">Job fact source excerpts</div>${evidence([...new Set(sourceEvidence)])}` : ""}</details>
        ${priorApplicationHelp}<div class="detail-actions">${externalLink(job.detail_url || job.apply_url,"Open role")}${job.apply_url && job.apply_url !== job.detail_url ? externalLink(job.apply_url,"Application form") : ""}${canCheckPlatform(job) ? `<button type="button" class="button button-secondary" data-action="check-platform" data-id="${escape(id)}">Check platform</button>` : ""}${canRefreshQuestions ? `<button type="button" class="button button-secondary" data-action="refresh-questions" data-id="${escape(id)}">Refresh form questions</button>` : ""}${job.browser_capture_available ? `<a class="button button-secondary" href="/api/jobs/${encodeURIComponent(id)}/screenshot" target="_blank" rel="noopener">Last browser capture ${icon("external")}</a>` : ""}${canRecordApplied ? `<button type="button" class="button button-secondary" data-action="applied-elsewhere" data-id="${escape(id)}">Already applied${icon("check")}</button>` : ""}${!hideJobActions && !isSubmitted && status !== "dismissed" ? `<button type="button" class="text-button" data-action="dismiss" data-id="${escape(id)}">Dismiss role</button>` : ""}</div></div></article>`;
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
  function onboardingValue(snapshot, field) {
    return field.storage === "profile" ? snapshot?.profile?.[field.key] : snapshot?.application_profile?.facts?.[field.key];
  }
  function onboardingOption(option) {
    return typeof option === "object" && option !== null ? {value:option.value,label:option.label ?? option.value} : {value:option,label:option};
  }
  function onboardingTypedValue(field, value) {
    if (["list","string_list","tags"].includes(field.type)) return splitLines(value);
    if (value === "" || value == null) return field.storage === "profile" && ["text","email","tel","url","textarea"].includes(field.type) ? "" : null;
    if (["boolean","bool"].includes(field.type)) return value === true || value === "true" ? true : value === false || value === "false" ? false : null;
    if (["integer","number","decimal"].includes(field.type)) return Number(value);
    return String(value).trim();
  }
  function onboardingPayload(snapshot, step, values, records = {}) {
    const body = {step:step.id,expected_revision:snapshot.revision,profile_patch:{},facts:{}};
    for (const field of list(step.fields)) {
      const value = onboardingTypedValue(field,values[field.key]);
      (field.storage === "profile" ? body.profile_patch : body.facts)[field.key] = value;
    }
    for (const key of ["education","employment","availability"]) if (Object.hasOwn(records,key)) body[key] = records[key];
    if (step.id === "review") body.complete = true;
    return body;
  }
  function onboardingDisplay(value) {
    if (value === null || value === undefined || value === "" || Array.isArray(value) && !value.length) return "Not provided";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (Array.isArray(value)) return value.join(", ");
    return String(value);
  }
  function onboardingFieldDisplay(field, value) {
    if (value !== null && value !== undefined && value !== "") {
      const option=list(field?.options).map(onboardingOption).find(option => String(option.value) === String(value));
      if (option) return String(option.label);
    }
    return onboardingDisplay(value);
  }
  function onboardingControl(field, value, prefix = "setup", attributes = "") {
    const id = `${prefix}-${field.key}`;
    const description = field.description ? `<span class="field-hint">${escape(field.description)}</span>` : "";
    const examples = list(field.examples).length ? `<details class="setup-examples"><summary>Questions this can answer</summary><ul>${field.examples.map(example => `<li>${escape(example)}</li>`).join("")}</ul></details>` : "";
    const common = `id="${escape(id)}" name="${escape(field.key)}" ${attributes} ${field.required ? "required" : ""} ${field.description ? `aria-describedby="${escape(id)}-help"` : ""}`;
    let control;
    if (["boolean","bool"].includes(field.type)) control = `<select ${common}><option value="">Not provided / ask me</option><option value="true" ${value === true || value === "true" ? "selected" : ""}>Yes</option><option value="false" ${value === false || value === "false" ? "selected" : ""}>No</option></select>`;
    else if (list(field.options).length) control = `<select ${common}><option value="">Not provided / ask me</option>${field.options.map(raw => { const option=onboardingOption(raw); return `<option value="${escape(option.value)}" ${String(value ?? "") === String(option.value) ? "selected" : ""}>${escape(option.label)}</option>`; }).join("")}</select>`;
    else if (["textarea","list","string_list","tags"].includes(field.type)) control = `<textarea ${common} rows="${field.type === "textarea" ? 3 : 2}" maxlength="4000">${escape(Array.isArray(value) ? value.join("\n") : value ?? "")}</textarea>`;
    else {
      const type = ["month","date","email","tel","url","number"].includes(field.type) ? field.type : ["integer","decimal"].includes(field.type) ? "number" : "text";
      control = `<input ${common} type="${type}" value="${escape(value ?? "")}" ${type === "number" ? `step="${escape(field.step ?? (field.type === "integer" ? "1" : "any"))}"` : 'maxlength="1000"'} ${field.min !== undefined ? `min="${escape(field.min)}"` : ""} ${field.max !== undefined ? `max="${escape(field.max)}"` : ""}>`;
    }
    const provenance=field.confirmed_at ? `<p class="setup-fact-source">Confirmed by you · ${escape(fullDate(field.confirmed_at))}</p>` : "";
    return `<div class="setup-field"><label class="field" for="${escape(id)}"><span>${escape(field.label || field.key)}</span><span id="${escape(id)}-help">${description}</span>${control}</label>${provenance}${examples}</div>`;
  }
  function onboardingRecordSections(step) {
    return list(step.records || step.sections).filter(section => ["education","employment","availability"].includes(section.key || section.id));
  }
  function onboardingNewRecord(section, id) {
    // Primary is a UI selection preference. Every biographical fact still
    // starts unknown, including whether an employment record is current.
    return {id,...Object.fromEntries(list(section.fields).map(field => [field.key,field.key === "primary_for_applications" ? false : null]))};
  }
  function onboardingFieldsMarkup(step, confirmed = {}) {
    const fields=list(step.fields);
    const essentials=new Set(["name","first_name","last_name","preferred_first_name","email","phone","location"]);
    const render=items => items.map(field => onboardingControl({...field,confirmed_at:confirmed[field.storage === "facts" ? "facts."+field.key : field.key]},onboardingDraft.values[field.key])).join("");
    if (step.id !== "identity") return `<div class="setup-field-grid">${render(fields)}</div>`;
    const primary=fields.filter(field => essentials.has(field.key));
    const optional=fields.filter(field => !essentials.has(field.key));
    return `<div class="setup-field-grid">${render(primary)}</div>${optional.length ? `<details class="setup-additional-details"><summary>More identity & professional details <span>Optional · ${optional.length} fields</span></summary><p>Professional links, preferred full name, pronouns and additional contact facts. Unanswered fields stay unknown.</p><div class="setup-field-grid">${render(optional)}</div></details>` : ""}`;
  }
  function onboardingRecordCards(step) {
    return onboardingRecordSections(step).map(section => {
      const key = section.key || section.id;
      const records = list(onboardingDraft?.records?.[key]);
      return `<section class="setup-record-section"><div class="setup-record-heading"><div><h3>${escape(section.title || section.label || key)}</h3><p>${escape(section.description || "Add only records you can confirm. Dates may stay unknown.")}</p></div><button type="button" class="button button-secondary" data-setup-action="add-record" data-record="${key}">Add ${key === "education" ? "education" : key === "employment" ? "experience" : "availability"} +</button></div>${records.length ? records.map((record,index) => `<fieldset class="setup-record" data-record="${key}" data-index="${index}"><legend>${escape(section.item_label || (key === "education" ? "Education" : key === "employment" ? "Experience" : "Availability"))} ${index+1}</legend><div class="setup-record-actions"><button type="button" class="text-button" data-setup-action="remove-record" data-record="${key}" data-index="${index}">Remove this record</button></div><div class="setup-field-grid">${list(section.fields).map(field => onboardingControl(field,record[field.key],`setup-${key}-${index}`,`data-record-key="${key}" data-record-index="${index}"`)).join("")}</div></fieldset>`).join("") : `<p class="setup-record-empty">No ${escape(key)} records added. Leave this empty if you prefer to answer later.</p>`}</section>`;
    }).join("");
  }
  function onboardingReview(snapshot) {
    const profile = snapshot.application_profile || {};
    const facts = list(snapshot.catalog).flatMap(step => list(step.fields).map(field => ({field,value:onboardingValue(snapshot,field)})));
    const recordDefinitions=Object.fromEntries(list(snapshot.catalog).flatMap(step => onboardingRecordSections(step).map(section => [section.key || section.id,section.fields])));
    return `<div class="setup-review-intro">${icon("check")}<div><strong>Your facts, in one place.</strong><p>Confirm what you know. An unanswered field stays unknown; Jobot will ask only when an application needs it.</p></div></div><dl class="setup-review-list">${facts.map(({field,value}) => `<div><dt>${escape(field.label)}</dt><dd class="${value == null || value === "" ? "is-unknown" : ""}">${escape(onboardingFieldDisplay(field,value))}</dd></div>`).join("")}</dl>${["education","employment","availability"].map(key => `<section class="setup-record-summary"><h3>${escape(key.charAt(0).toUpperCase()+key.slice(1))}</h3>${list(profile[key]).length ? list(profile[key]).map(record => `<p>${Object.entries(record).filter(([field,value]) => field !== "id" && value !== null && value !== "").map(([field,value]) => `<span>${escape(field.replaceAll("_"," "))}: ${escape(onboardingFieldDisplay(list(recordDefinitions[key]).find(item => item.key === field),value))}</span>`).join(" · ")}</p>`).join("") : '<p class="muted">Not provided</p>'}</section>`).join("")}<p class="setup-review-note">This saves your application profile. It does not approve a queue or submit an application. Required agreements, verification challenges, conflicting facts, and unsupported forms still pause for review.</p>`;
  }
  function beginOnboardingStep(index) {
    const catalog = list(onboardingSnapshot?.catalog);
    onboardingStep = Math.max(0,Math.min(index,catalog.length-1));
    const step = catalog[onboardingStep];
    if (!step) return;
    onboardingDraft = {values:Object.fromEntries(list(step.fields).map(field => [field.key,onboardingValue(onboardingSnapshot,field) ?? ""])),records:{}};
    for (const section of onboardingRecordSections(step)) {
      const key=section.key || section.id;
      onboardingDraft.records[key]=JSON.parse(JSON.stringify(list(onboardingSnapshot.application_profile?.[key])));
    }
    onboardingDirty=false;
    onboardingConflict=false;
    onboardingError="";
    renderOnboarding();
  }
  function renderOnboarding() {
    const catalog = list(onboardingSnapshot?.catalog);
    const completion=onboardingSnapshot?.completion || {};
    const completed=list(completion.completed_steps || onboardingSnapshot?.application_profile?.completed_steps);
    const total=catalog.length || completion.total_steps || 6;
    const done=Math.min(total,completed.length);
    $("#setup-progress-label").textContent=completion.complete ? "Application profile ready" : "Build your application profile";
    $("#setup-progress-count").textContent=`${done}/${total} saved`;
    $("#setup-progress-fill").style.width=`${done/total*100}%`;
    $("#nav-setup-progress").textContent=`${done}/${total}`;
    $("#nav-setup-progress").hidden=!onboardingSnapshot;
    $("#setup-banner").hidden=!onboardingSnapshot || completion.complete===true;
    $("#setup-banner-copy").textContent=`${done} of ${total} steps saved. Confirm common facts once, then reuse them across supported question wording.`;
    $("#setup-error").hidden=!onboardingError;
    $("#setup-error").innerHTML=onboardingError ? `<p>${escape(onboardingError)}</p>${onboardingConflict ? '<button type="button" class="button button-secondary" data-setup-action="reload">Reload saved answers (discard this unsaved step)</button>' : !onboardingSnapshot ? '<button type="button" class="text-button" data-setup-action="retry">Try loading again</button>' : '<p>Your draft is still here. Correct the issue described above, then save again.</p>'}` : "";
    if (!onboardingSnapshot) return;
    const step=catalog[onboardingStep];
    if (!step || !onboardingDraft) return;
    $("#setup-step-list").innerHTML=catalog.map((item,index) => `<button type="button" data-setup-action="step" data-step="${index}" class="setup-step ${index===onboardingStep ? "active" : ""}" ${index===onboardingStep ? 'aria-current="step"' : ""} ${onboardingSaving ? "disabled" : ""}><span class="setup-step-number ${completed.includes(item.id) ? "saved" : ""}">${completed.includes(item.id) ? icon("check") : index+1}</span><span><strong>${escape(item.title)}</strong><small>${completed.includes(item.id) ? "Saved · edit anytime" : "Not saved yet"}</small></span></button>`).join("");
    const confirmed=onboardingSnapshot.application_profile?.confirmed_at || {};
    const sourceText=list(step.fields).some(field => confirmed[field.storage === "facts" ? "facts."+field.key : field.key]) ? "Saved by you. Review and update whenever your situation changes." : "Only answers you save here become reusable facts.";
    $("#setup-step-content").innerHTML=`<div class="setup-step-eyebrow">STEP ${onboardingStep+1} OF ${total}<span id="setup-draft-status" ${onboardingDirty ? "" : "hidden"}>Unsaved changes</span></div><h2 id="setup-step-title" tabindex="-1">${escape(step.title)}</h2><p class="setup-step-description">${escape(step.description || "Add facts you want Jobot to use on applications.")}</p><form id="onboarding-form" novalidate>${onboardingFieldsMarkup(step,confirmed)}${onboardingRecordCards(step)}${step.id === "identity" ? '<div class="setup-resume-link">'+icon("briefcase")+'<p>Your resume is managed separately. <button type="button" class="text-button" data-view="profile">Upload or review your resume →</button></p></div>' : ""}${step.id === "review" ? onboardingReview(onboardingSnapshot) : `<p class="setup-reuse-note">${icon("check")}<span>${escape(sourceText)} Unknown answers are never turned into “No.”</span></p>`}<div class="setup-form-footer"><button type="button" class="button button-secondary" data-setup-action="back" ${onboardingStep===0 || onboardingSaving ? "disabled" : ""}>← Back</button><div><button type="button" class="text-button" data-setup-action="skip" ${onboardingSaving ? "disabled" : ""}>${onboardingStep===catalog.length-1 ? "Back to queue" : "Answer later"}</button><button type="submit" class="button button-primary" ${onboardingSaving || onboardingConflict ? "disabled" : ""}>${onboardingSaving ? "Saving…" : step.id === "review" ? "Confirm application profile" : "Save & continue"}${icon("arrow")}</button></div></div></form>`;
    if (onboardingSaving) {
      $("#onboarding-form").setAttribute("aria-busy","true");
      $$("input,select,textarea,button",$("#onboarding-form")).forEach(control => {control.disabled=true;});
    }
  }
  async function loadOnboarding(force = false) {
    if (onboardingLoading || onboardingDirty && !force) return;
    onboardingLoading=true;
    try {
      onboardingSnapshot=await api("/api/onboarding");
      const catalog=list(onboardingSnapshot.catalog);
      const completed=list(onboardingSnapshot.completion?.completed_steps);
      const first=Math.max(0,catalog.findIndex(step => !completed.includes(step.id)));
      beginOnboardingStep(force ? onboardingStep : first);
      if (!onboardingStarted) {
        onboardingStarted=true;
        const empty=!state.profile?.name && !state.profile?.email && !state.resume?.loaded && !list(state.jobs).length;
        if (empty && (!location.hash || location.hash==="#queue")) setView("setup");
      }
    } catch(error) {
      onboardingError=error.message || "Could not load your saved application profile.";
      renderOnboarding();
    } finally {onboardingLoading=false;}
  }
  async function saveOnboarding() {
    if (onboardingSaving || onboardingConflict || !onboardingSnapshot) return;
    const form=$("#onboarding-form");
    if (!form.reportValidity()) return;
    const step=list(onboardingSnapshot.catalog)[onboardingStep];
    const body=onboardingPayload(onboardingSnapshot,step,onboardingDraft.values,onboardingDraft.records);
    onboardingSaving=true;
    onboardingError="";
    renderOnboarding();
    try {
      onboardingSnapshot=await post("/api/onboarding",body);
      onboardingDirty=false;
      await refresh(true);
      if (step.id === "review") {beginOnboardingStep(onboardingStep);setView("rules");showNotice("Application profile confirmed. Review your search rules and authorize the queue when ready.");}
      else beginOnboardingStep(onboardingStep+1);
    } catch(error) {
      onboardingError=error.message || "Your answers could not be saved. Your draft is still here.";
      onboardingConflict=/changed|stale|revision|conflict/i.test(onboardingError);
    } finally {
      onboardingSaving=false;
      renderOnboarding();
      if (onboardingError) $("#setup-error").scrollIntoView({block:"start",behavior:"smooth"});
      else if (currentView === "setup") {$("#setup-step-title").focus({preventScroll:true});$("#view-setup").scrollIntoView({block:"start",behavior:"smooth"});}
    }
  }
  function answerContext(saved) {
    const sources = list(saved.sources);
    const selection=questionSources.get(savedAnswerIdentity(saved)) ?? (!saved.scope ? questionSources.get(saved.question) : undefined);
    const source = sources.find(item => item.job_id === selection) || sources[0];
    return source ? {...saved,...source,question:saved.question,answer:saved.answer} : null;
  }
  function savedAnswerIdentity(item) {
    return JSON.stringify([item.question,item.scope || "legacy",item.company || "",item.job_id || "",item.intent_id || ""]);
  }
  function recordedQuestion(jobId, question, bindingKey = null) {
    if (bindingKey) {
      const saved=list(state.answer_memory?.saved).find(item => savedAnswerIdentity(item) === bindingKey);
      const source=list(saved?.sources).find(item => item.job_id === jobId);
      return saved && source && saved.question === question ? {...saved,...source,question:saved.question,answer:saved.answer} : null;
    }
    return list(state.answer_memory?.pending).find(item => item.job_id === jobId && item.question === question)
      || list(state.answer_memory?.saved).flatMap(saved => list(saved.sources).map(source => ({...saved,...source,question:saved.question}))).find(item => item.job_id === jobId && item.question === question);
  }
  function questionIntentLabel(intent) {
    const labels={"eligibility.work_authorized_us":"U.S. work authorization","eligibility.sponsorship_us":"Sponsorship now or in the future","eligibility.sponsorship_now_us":"Current sponsorship","eligibility.sponsorship_future_us":"Future sponsorship","eligibility.us_citizen":"U.S. citizenship","experience.months":"Professional experience in months","experience.completed_internships":"Completed internships","compensation.target_base":"Base salary target","availability.immediate":"Immediate availability","availability.full_time_after_internship":"Full-time work after an internship","history.nda":"Nondisclosure agreements","history.employment_restrictions":"Employment restrictions","location.self_funded_relocation":"Relocation at your own expense"};
    return labels[intent] || String(intent || "Common question").split(".").at(-1).replaceAll("_"," ");
  }
  function answerSourceLabel(source) {
    if (!source) return "";
    if (typeof source === "object") return source.label || "Confirmed profile answer";
    if (source.includes("saved_answer_bindings") || source.includes("answers")) return "Answer you previously confirmed";
    if (source.includes("education")) return "Your saved education record";
    if (source.includes("employment")) return "Your saved work history";
    if (source.includes("availability")) return "Your confirmed availability";
    if (source.includes("application_profile")) return "Your common application profile";
    return "Your saved profile";
  }
  function questionCard(item, saved = null) {
    const bindingKey=saved ? savedAnswerIdentity(saved) : "";
    const key = saved?.scope ? `${bindingKey}:${item.job_id}` : `${item.job_id}:${item.question}`;
    const signature = JSON.stringify([item.type,item.options]);
    const draft = questionDrafts.get(key);
    const options = item.type === "checkbox" ? [{label:"Yes",value:true},{label:"No",value:false}] : list(item.options);
    const previous = saved ? options.findIndex(option => option.label === String(saved.answer) || String(option.value) === String(saved.answer)) : -1;
    const value = draft?.signature === signature ? draft.value : previous >= 0 ? String(previous) : "";
    const multiple = item.multiple === true || /(?:mark|select|choose) all (?:that )?apply/i.test(item.question);
    const choice = ["select","radio"].includes(item.type);
    const unsupported = item.type === "unsupported" || item.conflicting_controls === true;
    const unavailable = unsupported || choice && (!options.length || multiple);
    const searchable = item.type === "select" && item.searchable === true && !multiple;
    const searchValue = questionSearches.get(key) ?? (typeof state.profile?.answers?.[item.question] === "string" ? state.profile.answers[item.question] : "");
    const searchControl = searchable ? `<div class="choice-search"><label class="field">Search the employer’s choices<input name="lookup_query" type="text" maxlength="240" value="${escape(searchValue)}" placeholder="Type to search"></label><button type="button" class="button button-secondary" data-action="search-choices">Search choices</button></div><p class="field-hint">Search first, then choose an actual returned option. Search text is not saved as your answer.</p>` : "";
    let controls;
    if (unavailable) controls = `<p class="question-unavailable">${unsupported ? "This question has conflicting or unsupported controls and needs manual review on the employer form." : multiple ? "This control allows multiple selections and needs manual review. Optional questions can stay blank." : searchable ? "Type a search to load the employer’s available choices." : "Choices have not been loaded. Refresh the form questions to read the current dropdown."}</p>`;
    else if (options.length && options.length <= 6) controls = `<fieldset class="question-options"><legend>Choose your answer</legend>${options.map((option,index) => `<label><input type="radio" name="choice" value="${index}" ${value === String(index) ? "checked" : ""} required><span>${escape(option.label)}</span></label>`).join("")}</fieldset>`;
    else if (options.length) controls = `<label class="field">Your answer<select name="choice" required><option value="">Choose an available answer</option>${options.map((option,index) => `<option value="${index}" ${value === String(index) ? "selected" : ""}>${escape(option.label)}</option>`).join("")}</select></label>`;
    else controls = `<label class="field">Your answer<textarea name="answer" rows="2" maxlength="4000" required>${escape(draft?.signature === signature ? draft.value : saved?.answer ?? "")}</textarea></label>`;
    const context = saved && list(saved.sources).length > 1 ? `<label class="field">Application choices<select name="question_source" data-question="${escape(item.question)}" data-answer-key="${escape(bindingKey)}">${saved.sources.map(source => `<option value="${escape(source.job_id)}" ${source.job_id === item.job_id ? "selected" : ""}>${escape(source.company)} · ${escape(source.title)}</option>`).join("")}</select></label>` : "";
    const scope=draft?.scope || saved?.scope || (item.intent_id && item.scope !== "employer" && item.scope !== "application" ? "global" : item.job_id ? "application" : "global");
    const employerOnly=item.scope === "employer" || item.scope === "application";
    const scopeControl=!unavailable ? `<label class="field question-scope">Reuse this answer<select name="answer_scope"><option value="application" ${scope==="application" ? "selected" : ""}>Only this application</option><option value="employer" ${scope==="employer" ? "selected" : ""}>Only this employer</option>${!employerOnly ? `<option value="global" ${scope==="global" ? "selected" : ""}>${item.intent_id ? "Common fact across supported wording" : "Across employers for the same question"}</option>` : ""}</select></label>${item.intent_id && !employerOnly ? `<p class="question-group-context">Common fact: <strong>${escape(questionIntentLabel(item.intent_id))}</strong>. Confirming with common-fact reuse saves this value to your application profile. Choose a narrower scope if the answer is specific to this employer or application.</p>` : ""}` : "";
    const job=list(state.jobs).find(job => job.id === item.job_id);
    const canContinue=!unavailable && item.job_id && state.approval?.approved === true && state.approval?.candidate_fingerprint && job && !job.attempted_at && !["submitted","applied_elsewhere","uncertain","active","filling","submitting","applying"].includes(job.status);
    const source=item.answer_source ? `<p class="question-group-context">Answer source: ${escape(answerSourceLabel(item.answer_source))}</p>` : "";
    return `<form class="activity-answer-form" data-job-id="${escape(item.job_id)}" data-question="${escape(item.question)}" data-binding-key="${escape(bindingKey)}" data-draft-key="${escape(key)}" data-options-signature="${escape(signature)}"><div class="answer-context">${escape(item.company)} · ${escape(item.title)} · ${item.required ? "Required" : "Optional"}</div><h3>${escape(item.question)}</h3>${source}${context}${searchControl}${controls}${scopeControl}<div class="question-editor-actions">${!unavailable ? `<button type="submit" name="intent" value="save" class="button button-primary">Confirm &amp; remember answer ${icon("check")}</button>${canContinue ? '<button type="submit" name="intent" value="continue" class="button button-secondary">Confirm &amp; run approved queue</button>' : ""}` : ""}<button type="button" class="text-button" data-action="refresh-questions" data-id="${escape(item.job_id)}">Refresh form choices</button>${unavailable ? externalLink(job?.apply_url,"Open employer form") : ""}</div><p class="field-hint">${canContinue ? "Confirming saves the answer in the selected scope. “Confirm & run” adds this answer to your already approved profile and rules, then starts a one-time queue run. Scheduling stays off." : "Save the answer, then review the updated application settings before running."} Searching or refreshing never submits an application.</p></form>`;
  }
  function groupedQuestionCards(pending) {
    const groups=list(state.answer_memory?.groups);
    if (!groups.length) return pending.map(item => questionCard(item)).join("");
    const visible=new Set(pending.map(item => `${item.job_id}:${item.question}`));
    const included=new Set();
    const html=groups.map(group => {
      const questions=list(group.questions).filter(item => visible.has(`${item.job_id}:${item.question}`));
      if (!questions.length) return "";
      const primary=questions[0];
      // Choice menus belong to individual forms. Group wording without ever
      // borrowing another employer's options or confirming their answers.
      const sameText=questions.every(item => !["select","radio","checkbox"].includes(item.type)) && questions.length>1;
      const cards=sameText ? [primary] : questions;
      questions.forEach(item => included.add(`${item.job_id}:${item.question}`));
      return `<section class="question-group">${questions.length>1 ? `<p class="question-group-context">${questions.length} related questions · ${escape(group.intent_id ? questionIntentLabel(group.intent_id) : "Same question")}. ${sameText ? "Confirm a common fact to reuse it across supported wording. An application-scoped answer only resolves the form shown below." : "Each form keeps its own actual choices."}</p>` : ""}${cards.map(item => questionCard(item)).join("")}${sameText ? `<details><summary>See the original questions and employers</summary><ul>${questions.map(item => `<li>${escape(item.company)}: ${escape(item.question)}</li>`).join("")}</ul></details>` : ""}</section>`;
    }).join("");
    return html+pending.filter(item => !included.has(`${item.job_id}:${item.question}`)).map(item => questionCard(item)).join("");
  }
  function reviewActionCards(actions) {
    const labels={agreement:"Review an agreement",captcha:"Complete verification",login:"Sign in on the employer site",form:"Review the employer form",uncertain:"Check whether it was received"};
    const descriptions={agreement:"Review the employer's actual terms before accepting. An answer saved in Jobot does not accept an agreement.",captcha:"Complete the challenge on the employer site. Jobot does not bypass verification.",login:"Continue in your employer account. Passwords and verification codes are not saved in your answer profile.",form:"The form needs an action that this adapter cannot complete automatically.",uncertain:"A submission may already have reached the employer. Verify its status before any further action; Jobot will not automatically retry."};
    return list(actions).map(item => `<article class="site-action-card" data-job-id="${escape(item.job_id)}"><div class="site-action-symbol">${icon(item.kind === "uncertain" ? "info" : "lock")}</div><div class="site-action-copy"><div class="answer-context">${escape(item.company)} · ${escape(item.title)}</div><h3>${escape(labels[item.kind] || labels.form)}</h3><p class="site-action-reason">${escape(item.message || "This application needs your review on the employer site.")}</p><p>${escape(descriptions[item.kind] || descriptions.form)}</p></div><div class="site-action-link">${externalLink(item.apply_url,item.kind === "uncertain" ? "Check employer status" : "Open employer form")}</div></article>`).join("");
  }
  function renderReviewActions() {
    const actions=list(state.review_actions);
    $("#activity-site-actions").hidden=!actions.length;
    $("#site-action-count").textContent=actions.length;
    $("#site-action-list").innerHTML=reviewActionCards(actions);
  }
  function renderActivityQuestions() {
    const pending = list(state.answer_memory?.pending);
    const saved = list(state.answer_memory?.saved);
    const signature = JSON.stringify([pending,saved,state.answer_memory?.groups,state.review_actions,state.approval?.approved,state.approval?.candidate_fingerprint,list(state.jobs).map(job => [job.id,job.status,job.attempted_at]),[...questionSources]]);
    if (signature === lastInboxSignature) return;
    lastInboxSignature = signature;
    const attentionCount=pending.length+list(state.review_actions).length;
    $("#nav-activity-questions").textContent = attentionCount;
    $("#nav-activity-questions").hidden = !attentionCount;
    const optionalOpen = $("#activity-question-list details")?.open === true;
    const openSaved = new Set($$("#rules-answer-list .saved-answer-editor[open]").map(node => node.dataset.bindingKey));
    const required = pending.filter(item => item.required);
    const optional = pending.filter(item => !item.required);
    $("#activity-question-count").textContent = `${required.length} required · ${optional.length} optional`;
    $("#activity-question-list").innerHTML = pending.length ? groupedQuestionCards(required) + (optional.length ? `<details class="optional-form-questions" ${optionalOpen ? "open" : ""}><summary>${optional.length} optional questions — you can leave these blank</summary>${groupedQuestionCards(optional)}</details>` : "") : '<p class="answer-empty">No unanswered application questions. New questions appear when a form needs your input.</p>';
    $("#activity-saved-answer-list").innerHTML = `<details class="saved-answer-review"><summary>Saved answers (${saved.length})</summary>${saved.map((item,index) => `<div class="saved-question-row"><div><strong>${escape(item.question)}</strong><p>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}</p></div><button type="button" class="text-button" data-action="edit-answer" data-index="${index}">Review choices</button></div>`).join("")}</details>`;
    $("#rules-answer-list").innerHTML = saved.length ? saved.map((item,index) => {
      const observed = answerContext(item);
      const identity=savedAnswerIdentity(item);
      return observed ? `<details class="saved-answer-editor" data-binding-key="${escape(identity)}" ${openSaved.has(identity) ? "open" : ""}><summary>${escape(item.question)}<span>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}${item.company ? ` · ${escape(item.company)}` : ""}</span></summary>${questionCard(observed,item)}</details>` : `<div class="saved-question-row" data-binding-key="${escape(identity)}"><div><strong>${escape(item.question)}</strong><p>${escape(item.answer)}${item.company ? ` · ${escape(item.company)}` : ""}</p>${item.scope ? '<p class="field-hint">Refresh the original employer form before editing this scoped answer.</p>' : ""}</div><button type="button" class="text-button" data-action="edit-answer" data-index="${index}">Edit answer</button></div>`;
    }).join("") : '<p class="answer-empty">No confirmed answers yet. Answer employer questions in Activity as they appear.</p>';
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
    const applicationProfile=profile.application_profile || {};
    const factDefinitions=Object.fromEntries(list(onboardingSnapshot?.catalog).flatMap(step => list(step.fields).map(field => [field.key,field])));
    const recordDefinitions=Object.fromEntries(list(onboardingSnapshot?.catalog).flatMap(step => onboardingRecordSections(step).map(section => [section.key || section.id,section.fields])));
    const commonReview=Object.entries(applicationProfile.facts || {}).filter(([,value]) => value !== null && value !== "").map(([key,value]) => `<div class="approval-row"><span>${escape(factDefinitions[key]?.label || key.replaceAll("_"," "))}</span><strong>${escape(onboardingFieldDisplay(factDefinitions[key],value))}</strong></div>`).join("")+["education","employment","availability"].map(key => list(applicationProfile[key]).map(record => `<div class="approval-row"><span>${escape(key.charAt(0).toUpperCase()+key.slice(1))}</span><strong>${Object.entries(record).filter(([field,value]) => field !== "id" && value !== null && value !== "").map(([field,value]) => `${escape(field.replaceAll("_"," "))}: ${escape(onboardingFieldDisplay(list(recordDefinitions[key]).find(item => item.key === field),value))}`).join(" · ")}</strong></div>`).join("")).join("");
    const bindingReview=list(applicationProfile.saved_answer_bindings).map(binding => `<div class="approval-row"><span>${escape(binding.question)}<small> · ${escape(binding.scope === "application" ? "This application" : binding.scope === "employer" ? binding.company || "This employer" : "Across employers")}</small></span><strong>${escape(onboardingDisplay(binding.answer))}</strong></div>`).join("");
    const stale = Boolean(approvalReview.fingerprint && approvalReview.fingerprint !== fingerprint);
    $("#approval-summary").innerHTML = fields.map(([key,value]) => `<div class="approval-row"><span>${key}</span><strong>${escape(value)}</strong></div>`).join("") + (selected && companies.length ? `<div class="approval-companies">Companies: ${escape(companies.join(" · "))}</div>` : "") + `<details class="approval-companies"><summary>Applicant details</summary>${applicantReview}</details><details class="approval-companies"><summary>Common facts & history</summary>${commonReview || "No additional common facts saved."}</details>` + `<details class="approval-companies"><summary>Reusable answers (${Object.keys(profile.answers || {}).length+list(applicationProfile.saved_answer_bindings).length})</summary>${answerReview+bindingReview || "No saved reusable answers."}</details>` + `<div class="approval-companies">${stale ? "Saved configuration has changed. The details above have been preserved for your review." : "Review the saved details above before acknowledging."}</div><button type="button" class="text-button" data-action="review-refresh">Refresh saved review ${icon("refresh")}</button>`;
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
    $("#saved-answers").innerHTML = saved.length ? saved.map((item,index) => `<article class="answer-item saved-answer">${item.scope ? `<div class="answer-context">${escape(item.scope === "application" ? "Only this application" : item.scope === "employer" ? `Only ${item.company || "this employer"}` : "Across employers")}${item.intent_id ? ` · ${escape(questionIntentLabel(item.intent_id))}` : ""}</div>` : ""}<h3>${escape(item.question)}</h3><p>${escape(typeof item.answer === "boolean" ? item.answer ? "Yes" : "No" : item.answer)}</p><div class="detail-actions"><button type="button" class="text-button" data-action="edit-answer" data-index="${index}">Edit answer</button><button type="button" class="text-button" data-action="forget-answer" data-index="${index}">Forget</button></div></article>`).join("") : '<p class="answer-empty">No reusable answers saved. Add one below or answer a question found during an application.</p>';
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
    const observed = pending ? item : answerContext(item);
    if (observed && (pending || item.scope || ["select","radio","checkbox"].includes(observed.type))) {
      if (pending) { setView("activity"); const form = [...document.querySelectorAll("#activity-question-list .activity-answer-form")].find(form => form.dataset.jobId === observed.job_id && form.dataset.question === observed.question); if (form?.closest("details")) form.closest("details").open=true; form?.scrollIntoView({block:"center",behavior:"smooth"}); }
      else { setView("rules"); const details = [...document.querySelectorAll("#rules-answer-list .saved-answer-editor")].find(node => node.dataset.bindingKey === savedAnswerIdentity(item)); if (details) { details.open=true; details.scrollIntoView({block:"center",behavior:"smooth"}); } }
      return;
    }
    if (item.scope) {
      setView("rules");
      const row=$$("#rules-answer-list [data-binding-key]").find(node => node.dataset.bindingKey === savedAnswerIdentity(item));
      row?.scrollIntoView({block:"center",behavior:"smooth"});
      showNotice("Refresh the original employer form's questions before editing this scoped answer. Its saved scope and answer have not changed.",true);
      return;
    }
    resetAnswerForm();
    setView("profile");
    const form = $("#answer-memory-form");
    form.elements.question.value = item.question;
    form.elements.question.readOnly = true;
    form.elements.job_id.value = pending ? item.job_id : item.scope === "application" ? item.job_id || "" : "";
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
      profileEditorRevision = state.profile_revision || null;
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
    renderPlatforms();
    renderJobs();
    renderAnswerMemory();
    renderActivity();
    renderProgress();
    renderActivityQuestions();
    renderReviewActions();
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
        if (!onboardingStarted && !onboardingLoading) loadOnboarding();
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
    if (event.target.closest("#onboarding-form")) {
      const step=list(onboardingSnapshot?.catalog)[onboardingStep];
      const key=event.target.name;
      if (event.target.dataset.recordKey) {
        const recordKey=event.target.dataset.recordKey;
        const section=onboardingRecordSections(step).find(item => (item.key || item.id)===recordKey);
        const field=list(section?.fields).find(item => item.key===key);
        if (field) onboardingDraft.records[recordKey][Number(event.target.dataset.recordIndex)][key]=onboardingTypedValue(field,event.target.value);
      } else onboardingDraft.values[key]=event.target.value;
      onboardingDirty=true;
      $("#setup-draft-status").hidden=false;
      return;
    }
    const form = event.target.closest(".activity-answer-form");
    if (!form) return;
    if (event.target.name === "lookup_query") { questionSearches.set(form.dataset.draftKey || `${form.dataset.jobId}:${form.dataset.question}`,event.target.value); return; }
    const value = form.elements.choice?.value ?? form.elements.answer?.value ?? "";
    questionDrafts.set(form.dataset.draftKey || `${form.dataset.jobId}:${form.dataset.question}`,{value,scope:form.elements.answer_scope?.value,signature:form.dataset.optionsSignature});
  });
  document.addEventListener("change",event => {
    if (event.target.name !== "question_source") return;
    questionSources.set(event.target.dataset.answerKey || event.target.dataset.question,event.target.value);
    lastInboxSignature="";
    renderActivityQuestions();
  });
  document.addEventListener("submit",event => {
    if (event.target.id === "onboarding-form") {event.preventDefault();saveOnboarding();return;}
    const form = event.target.closest(".activity-answer-form");
    if (!form) return;
    event.preventDefault();
    if (dirty.profile) { showNotice("Save or discard your unsaved profile changes before confirming an application answer.",true); return; }
    const item = recordedQuestion(form.dataset.jobId,form.dataset.question,form.dataset.bindingKey || null);
    if (!item) { showNotice("This question changed. Refresh Activity before confirming an answer.",true); return; }
    if (form.dataset.optionsSignature !== JSON.stringify([item.type,item.options])) { showNotice("The available choices changed. Review them and choose your answer again.",true); return; }
    let answer, choiceLabel;
    if (form.elements.choice) {
      const selected = Number(form.elements.choice.value);
      const options = item.type === "checkbox" ? [{value:true},{value:false}] : list(item.options);
      if (form.elements.choice.value === "" || !Number.isInteger(selected) || !options[selected]) return;
      answer = options[selected].value;
      if (["select","radio"].includes(item.type)) choiceLabel = options[selected].label;
    } else answer = form.elements.answer.value.trim();
    const continueAfter=event.submitter?.value === "continue";
    const scope=form.elements.answer_scope?.value || (item.job_id ? "application" : "global");
    const expectedFingerprint=state.approval?.candidate_fingerprint;
    if (continueAfter && (!expectedFingerprint || state.approval?.approved !== true || dirty.rules || dirty.profile)) {showNotice("Save this answer, then review and approve your current profile and rules before running the queue.",true);return;}
    action(`question-${item.job_id}-${item.id || item.question}`,event.submitter || form.querySelector('button[type="submit"]'),async () => {
      const result = await post("/api/answers",{question:item.question,answer,job_id:item.job_id,scope,...(item.intent_id && scope === "global" ? {intent_id:item.intent_id,confirm_intent:true} : {}),...(choiceLabel === undefined ? {} : {choice_label:choiceLabel}),...(continueAfter ? {approve_and_continue:true,expected_fingerprint:expectedFingerprint} : {})});
      questionDrafts.delete(form.dataset.draftKey || `${item.job_id}:${item.question}`);
      if (onboardingSnapshot && !onboardingDirty) {onboardingStarted=false;loadOnboarding();}
      if (continueAfter) {
        if (!result.continue_ready) throw new Error("The answer was saved, but the queue was not cleared to run. Refresh status and review the remaining questions.");
        try {
          if (!Number.isSafeInteger(result.stop_generation) || result.stop_generation < 0) throw new Error("The continuation is missing its cancellation check. Use Run when you are ready.");
          await post(`/api/run?expected_stop_generation=${result.stop_generation}`);setView("queue");
        }
        catch(error) {throw new Error(`The answer was saved. The queue did not start: ${error.message}`);}
      }
      return result;
    },continueAfter ? "Answer confirmed. A one-time approved queue run has started; follow observed progress below." : "Answer confirmed in the selected scope. Review and approve the updated details before running again.");
  });
  document.addEventListener("click", event => {
    const setupButton=event.target.closest("[data-setup-action]");
    if (setupButton) {
      const actionName=setupButton.dataset.setupAction;
      if (onboardingSaving) return;
      if (["retry","reload"].includes(actionName)) {loadOnboarding(actionName==="reload");return;}
      if (["step","back","skip"].includes(actionName)) {
        if (onboardingDirty) {showNotice("Save this step before moving on. Your unsaved answers are still here.",true);return;}
        const next=actionName==="step" ? Number(setupButton.dataset.step) : onboardingStep+(actionName==="back" ? -1 : 1);
        if (next>=list(onboardingSnapshot?.catalog).length) setView("queue"); else beginOnboardingStep(next);
        return;
      }
      const key=setupButton.dataset.record;
      if (actionName==="add-record") {
        const section=onboardingRecordSections(list(onboardingSnapshot.catalog)[onboardingStep]).find(item => (item.key || item.id)===key);
        if (!section) return;
        onboardingDraft.records[key].push(onboardingNewRecord(section,crypto.randomUUID()));
      } else if (actionName==="remove-record") onboardingDraft.records[key].splice(Number(setupButton.dataset.index),1);
      else return;
      onboardingDirty=true;
      renderOnboarding();
      return;
    }
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
    } else if (name === "refresh-questions") {
      const form = button.closest(".activity-answer-form");
      action(`questions-${id}`,button,() => form ? post(`/api/jobs/${encodeURIComponent(id)}/questions/options`,{question:form.dataset.question,query:""}) : post(`/api/jobs/${encodeURIComponent(id)}/questions/refresh`),result => result.message);
    } else if (name === "check-platform") {
      const job = list(state.jobs).find(item => String(item.id) === id);
      if (!job || !canCheckPlatform(job)) { showNotice("This role cannot be checked in its current state. Refresh its recorded status.",true); return; }
      expanded.add(id);
      action(`platform-${id}`,button,() => post(`/api/jobs/${encodeURIComponent(id)}/platform/check`),"Read-only platform check completed. Review the observed stage and next step in the role details; nothing was submitted.");
    } else if (name === "search-choices") {
      const form = button.closest(".activity-answer-form");
      const query = form?.elements.lookup_query?.value.trim();
      if (!query) { showNotice("Enter a search term first.",true); return; }
      action(`questions-${form.dataset.jobId}`,button,() => post(`/api/jobs/${encodeURIComponent(form.dataset.jobId)}/questions/options`,{question:form.dataset.question,query}),result => result.message);
    } else if (name === "stop-run") {
      action("stop-run",button,() => post("/api/run/stop"),"Stop requested before the next submission. Scheduling is off; an already sent application cannot be undone.");
    } else if (name === "refresh-worker") {
      action("refresh-worker",button,() => refresh(true));
    } else if (name === "open-questions") {
      setView("activity");
      const form = id ? [...document.querySelectorAll("#activity-question-list .activity-answer-form")].find(item => item.dataset.jobId === id) : null;
      if (form?.closest("details")) form.closest("details").open=true;
      const siteAction=id ? $$("#site-action-list .site-action-card").find(item => item.dataset.jobId===id) : null;
      (form || siteAction || $("#activity-questions")).scrollIntoView({block:"start",behavior:"smooth"});
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
      action("answer-memory",button,() => api("/api/answers",{method:"DELETE",body:JSON.stringify({question:item.question,...(item.scope ? {scope:item.scope,company:item.company || undefined,job_id:item.job_id || undefined} : {})})}),"Answer removed. Review and approve your updated profile before running applications.");
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
    const body = {question:form.elements.question.value.trim(),answer,scope:form.elements.job_id.value ? "application" : "global"};
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
    const body = {...state.profile,expected_revision:profileEditorRevision || state.profile_revision};
    ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company"].forEach(name => { body[name] = form.elements[name].value.trim(); });
    body.skills = splitLines(form.elements.skills.value);
    body.approved_work_locations = splitLines(form.elements.approved_work_locations.value);
    body.experience_months = form.elements.experience_months.value === "" ? null : Number(form.elements.experience_months.value);
    body.graduation_date = form.elements.graduation_date.value || null;
    body.graduation_month = form.elements.graduation_month.value || null;
    body.work_authorized = booleanValue(form.elements.work_authorized.value);
    body.needs_sponsorship = booleanValue(form.elements.needs_sponsorship.value);
    body.answers = answers;
    action("profile",$("button[type=submit]",form),async () => { await post("/api/profile",body); markSaved("profile"); if (!onboardingDirty) onboardingStarted=false; },"Profile saved. Review and approve your rules before running applications.");
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
