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
  let experienceFilterEnabled = true;
  let pollTimer;
  let refreshing = false;
  let refreshInFlight = null;
  let noticeTimer;
  let lastJobSignature = "";
  let lastEventSignature = "";
  let sourceMessage = "";
  let currentView = "queue";
  let approvalReview = null;
  const dirty = {profile:false,rules:false};
  const initialized = {profile:false,rules:false};
  const expanded = new Set();
  const busyActions = new Set();
  const viewNames = {queue:"Application queue",profile:"Profile & resume",rules:"Application rules",activity:"Activity"};
  const attentionStatuses = new Set(["needs_attention","needs_review","review","failed","blocked","error","ineligible","manual_review","held","uncertain"]);

  function safeURL(value) {
    try { const url = new URL(value); return ["http:","https:"].includes(url.protocol) ? url.href : null; } catch { return null; }
  }
  function leverPostingURL(value) {
    if (typeof value !== "string" || /[\s\\]/.test(value)) return null;
    try {
      const url = new URL(value);
      const posting = /^\/[A-Za-z0-9][A-Za-z0-9_-]*\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\/apply)?\/?$/i;
      return url.protocol === "https:" && ["jobs.lever.co","jobs.eu.lever.co"].includes(url.hostname) && !url.username && !url.password && (!url.port || url.port === "443") && posting.test(url.pathname) ? url.href : null;
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
    try { response = await fetch(path,{...options,headers:{"X-JobRadar-Client":"dashboard",...(options.body instanceof FormData ? {} : {"Content-Type":"application/json"}),...(options.headers || {})}}); }
    catch { throw new Error("The local Jobot server is not responding. Check that it is running, then try again."); }
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) throw new Error(errorMessage(data,`Request could not be completed (${response.status}).`));
    return data;
  }
  async function post(path, body = {}) { return api(path,{method:"POST",body:body instanceof FormData ? body : JSON.stringify(body)}); }
  async function action(name, button, task, success) {
    if (busyActions.has(name)) return;
    busyActions.add(name);
    const previousHTML = button?.innerHTML;
    if (button) { button.disabled = true; button.classList.add("busy"); button.setAttribute("aria-busy","true"); }
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
    if (state.rules?.prefer_new_grad) {
      const priority = Number(newGradPriority(b)) - Number(newGradPriority(a));
      if (priority) return priority;
    }
    return (Number.isFinite(Number(b.score)) && b.score !== null ? Number(b.score) : -1) - (Number.isFinite(Number(a.score)) && a.score !== null ? Number(a.score) : -1);
  }
  function renderJobs(force = false) {
    const signature = JSON.stringify([state.jobs,state.rules,filter,search,experienceFilterEnabled]);
    if (!force && signature === lastJobSignature) return;
    lastJobSignature = signature;
    const allJobs = list(state.jobs);
    const maximum = experienceLimit();
    $("#experience-filter-bar").hidden = maximum === null;
    $("#experience-filter").checked = experienceFilterEnabled;
    $(".sort-label").innerHTML = state.rules?.prefer_new_grad ? "New grads, then fit <span>↓</span>" : "Highest fit first <span>↓</span>";
    const jobs = allJobs.filter(job => {
      const status = String(job.status || "").toLowerCase();
      if (filter === "queued" && !["new","queued"].includes(status)) return false;
      if (filter === "attention" && !jobAttention(job)) return false;
      if (filter === "submitted" && status !== "submitted") return false;
      if (maximum !== null && experienceFilterEnabled && !experienceVisible(job,maximum)) return false;
      return !search || `${job.company} ${job.title} ${job.location}`.toLowerCase().includes(search);
    }).sort(compareJobs);
    $("#job-count").textContent = jobs.length === allJobs.length ? formatNumber(allJobs.length) : `${formatNumber(jobs.length)} / ${formatNumber(allJobs.length)}`;
    $("#job-count").setAttribute("aria-label",`${jobs.length} of ${allJobs.length} roles shown`);
    $("#experience-filter-detail").textContent = maximum === null ? "" : `${jobs.length} of ${allJobs.length} roles shown · ${maximum === 0 ? "No experience required" : `${maximum/12} year${maximum === 12 ? "" : "s"} maximum required`}${experienceFilterEnabled ? state.rules?.prefer_new_grad ? ". New grads with unknown experience are kept for review; other unknowns are hidden. Turn off to review all." : ". Unknown requirements are hidden; turn off to review all." : ". Experience view filter is off."}`;
    if (!allJobs.length) {
      $("#job-list").innerHTML = `<div class="empty-state"><div class="empty-radar"><span></span><i></i></div><div class="empty-eyebrow">A LITTLE SETUP. A LOT OF POSSIBILITY.</div><h3>Your next opportunity starts here</h3><p>Add your resume and choose where you want to apply.<br>We’ll help you turn a promising role into a thoughtful application.</p><button class="button button-primary" type="button" data-view="profile">Set up your profile${icon("arrow")}</button><div class="demo-link-row"><button class="text-button" type="button" data-action="demo">Try a local demo <span>↗</span></button><small>Demo uses a local mock employer.</small></div></div>`;
      return;
    }
    if (!jobs.length) {
      $("#job-list").innerHTML = `<div class="simple-empty">${icon("search")}<h3>No roles in this view</h3><p>${search ? "Try another role or company name." : maximum !== null && experienceFilterEnabled ? "Turn off the experience filter or choose another view to review more roles, including unknown requirements." : "Choose All roles to see the rest of your opportunities."}</p><button type="button" class="text-button" data-action="clear-filter" style="margin-top:18px">Show all roles</button></div>`;
      return;
    }
    $("#job-list").innerHTML = jobs.map(jobCard).join("");
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
      const cls = ["pass","eligible","met","yes","true","confirmed"].includes(status) ? "pass" : ["fail","ineligible","not_met","no","false"].includes(status) ? "fail" : "unknown";
      return `<div class="eligibility-item ${cls}"><span class="status-dot"></span><span><strong>${escape(item.label || "Eligibility")}</strong> · ${escape(item.detail || status.replaceAll("_"," "))}</span></div>`;
    }).join("");
    const status = String(job.status).toLowerCase();
    const isSubmitted = status === "submitted";
    const hideJobActions = isDemo(job) || Boolean(job.attempted_at) || ["active","filling","submitting","applying","uncertain","applied_elsewhere"].includes(status);
    const canRecordApplied = !hideJobActions && !isSubmitted;
    const priorApplicationHelp = canRecordApplied && !leverPostingURL(job.apply_url) ? '<p class="history-help">Already applied? Without an exact Lever posting URL, recording this role also holds future listings with the same company and title for review.</p>' : "";
    const submissionInfo = isSubmitted ? [...new Set([job.message,job.confirmation,job.error].filter(Boolean).map(value => typeof value === "object" ? JSON.stringify(value) : String(value)))].join(" ") : "";
    return `<article class="job-card" data-job-id="${escape(id)}"><div class="job-card-top"><div class="company-avatar" aria-hidden="true">${escape(initials)}</div><div class="job-heading"><div class="job-company">${escape(job.company || "Company not provided")}</div><h3 class="job-title">${escape(job.title || "Untitled role")}</h3><div class="job-meta"><span>${escape(job.location || "Location not provided")}</span>${job.work_model ? `<span>${escape(job.work_model)}</span>` : ""}<span title="Date listed by the discovery feed; the original posting date may differ.">${escape(relativeDate(job.date_posted,"Feed listed "))}</span></div></div><div class="job-score ${scoreClass}"><strong>${score === null ? "—" : score}</strong><small>${score === null ? "FIT UNKNOWN" : "RESUME FIT"}</small></div></div><div class="job-card-bottom"><div class="job-tags"><span class="tag ${statusClass}">${escape(statusLabel)}</span>${job.ats && !isDemo(job) ? `<span class="tag">${escape(String(job.ats).toUpperCase())}</span>` : ""}${!isDemo(job) ? `<span class="tag" title="${escape(list(detail.experience_requirement?.evidence).join(" "))}">${escape(experienceLabel(detail.experience_requirement,detail.is_new_grad === true))}</span>${detail.is_new_grad === true ? '<span class="tag">New grad / entry level</span>' : ""}` : ""}${isDemo(job) ? '<span class="tag demo">LOCAL DEMO</span>' : ""}</div><button type="button" class="details-toggle" data-action="details" data-id="${escape(id)}" aria-expanded="${open}" aria-controls="${escape(detailId)}">${open ? "Less detail" : "View details"}${icon("chevron")}</button></div><div class="job-detail" id="${escape(detailId)}" ${open ? "" : "hidden"}>${isDemo(job) ? '<div class="demo-banner">Demo uses a local mock employer. No real employer receives this application.</div>' : ""}${job.error && !["submitted","dismissed","applied_elsewhere"].includes(status) ? `<div class="job-error">${escape(job.error)}</div>` : ""}${submissionInfo ? `<div class="submission-confirmation"><div class="detail-label">Submission confirmation</div><p>${escape(submissionInfo)}</p></div>` : ""}<div class="detail-columns"><div><div class="detail-label">Resume matches</div>${evidence(detail.matches)}</div><div><div class="detail-label">Not evidenced</div>${evidence(detail.not_evidenced)}</div></div><div class="detail-label">Eligibility checks</div><div class="eligibility-list">${eligibility || '<p class="detail-empty">Eligibility has not been assessed. Review the role before applying.</p>'}</div>${list(detail.reasons).length ? `<div class="detail-label">Scoring notes</div>${evidence(detail.reasons)}` : ""}${job.description ? `<div class="detail-label">About the role</div><div class="job-description">${escape(job.description)}</div>` : ""}${priorApplicationHelp}<div class="detail-actions">${externalLink(job.detail_url || job.apply_url,"Open role")}${job.apply_url && job.apply_url !== job.detail_url ? externalLink(job.apply_url,"Application form") : ""}${!hideJobActions && !isSubmitted && !["new","queued"].includes(status) ? `<button type="button" class="button button-secondary" data-action="queue" data-id="${escape(id)}">Add to queue${icon("arrow")}</button>` : ""}${canRecordApplied ? `<button type="button" class="button button-secondary" data-action="applied-elsewhere" data-id="${escape(id)}">Already applied${icon("check")}</button>` : ""}${!hideJobActions && !isSubmitted && status !== "dismissed" ? `<button type="button" class="text-button" data-action="dismiss" data-id="${escape(id)}">Dismiss role</button>` : ""}</div></div></article>`;
  }
  function renderReadiness() {
    const profileReady = Boolean(state.profile?.name && state.profile?.email);
    const resumeReady = Boolean(state.resume?.loaded);
    const companies = list(state.rules?.companies);
    const boardsReady = companies.length > 0 && companies.every(company => Boolean((state.rules?.company_boards || {})[company]));
    const rulesReady = companies.length > 0 && boardsReady;
    const approved = Boolean(state.approval?.approved);
    $("#readiness-title").textContent = approved ? "Ready when you are" : "Before your first run";
    $("#readiness-description").textContent = approved ? "Your saved rules are approved. You decide when to put them to work." : "A few essentials keep every application intentional.";
    const steps = [
      {title:"Add your profile & resume",description:profileReady && resumeReady ? "Your application essentials are saved." : "Give every application a solid foundation.",done:profileReady && resumeReady,view:"profile"},
      {title:"Set your application rules",description:rulesReady ? `${companies.length} ${companies.length === 1 ? "company" : "companies"} with official Lever boards saved.` : companies.length ? "Map your companies to official Lever boards." : "Choose companies, locations, and limits.",done:rulesReady,view:"rules"},
      {title:"Review & approve",description:approved ? "Approved for applications within your rules." : "Give explicit approval before anything runs.",done:approved,view:"rules"}
    ];
    $("#readiness-steps").innerHTML = steps.map((step,index) => `<button type="button" class="readiness-step" data-view="${step.view}"><span class="step-number ${step.done ? "done" : ""}">${step.done ? icon("check") : String(index+1).padStart(2,"0")}</span><span class="step-copy"><strong>${step.title}</strong><small>${escape(step.description)}</small></span><span class="step-arrow">↗</span></button>`).join("");
  }
  function renderControls() {
    const approved = Boolean(state.approval?.approved);
    const running = Boolean(state.automation?.running);
    const enabled = Boolean(state.automation?.enabled);
    $("#profile-status").textContent = "";
    $("#profile-status").innerHTML = `<span class="status-dot"></span>${approved ? "Profile approved" : "Profile unverified"}`;
    $("#profile-status").classList.toggle("ready",approved);
    $("#automation-status").innerHTML = `<span class="status-dot"></span>${running ? "Run in progress" : enabled ? "Automation on" : "Automation off"}`;
    $("#automation-status").classList.toggle("enabled",enabled || running);
    $("#run-button").disabled = !approved || running || busyActions.has("run");
    $("#run-button").title = approved ? running ? "A run is already in progress" : "Submit eligible queued jobs within your approved rules" : "Save and approve your application rules first";
    if (!busyActions.has("run")) $("#run-button").innerHTML = `${icon(running ? "refresh" : "play")}${running ? "Run in progress" : "Run approved queue"}`;
    $("#run-button").classList.toggle("busy",running || busyActions.has("run"));
    $("#fetch-button").disabled = busyActions.has("fetch") || running || Boolean(state.automation?.fetching);
    $("#automation-toggle").disabled = (!approved && !enabled) || busyActions.has("automation");
    $("#automation-toggle").setAttribute("aria-checked",String(enabled));
    $("#automation-toggle").setAttribute("aria-label",enabled ? "Disable scheduled applications" : "Enable scheduled applications");
    $("#automation-card-label").textContent = running ? "A run is in progress" : enabled ? "Automation is on" : "Automation is off";
    $("#automation-card-detail").textContent = enabled ? `Runs every ${state.rules?.interval_minutes || "—"} minutes.` : approved ? "Ready to follow your saved rules." : "Approve your rules to enable.";
    const reviewReady = Boolean(approvalReview?.fingerprint);
    const reviewStale = reviewReady && approvalReview.fingerprint !== state.approval?.candidate_fingerprint;
    if (reviewStale) $("#approval-acknowledge").checked = false;
    const ack = $("#approval-acknowledge").checked;
    $("#approve-button").disabled = !ack || !reviewReady || reviewStale || approved || dirty.profile || dirty.rules || busyActions.has("approve");
    $("#approval-title").textContent = reviewStale ? "This review is out of date" : approved ? "Your rules are approved" : "Review before you run";
    $("#approval-status").textContent = reviewStale ? "Saved configuration changed. Refresh the saved review, check its details, then acknowledge again." : dirty.profile || dirty.rules ? "Save your pending changes before approving." : !reviewReady ? "Waiting for a saved configuration snapshot from the local server." : approved ? `Approved ${fullDate(state.approval.approved_at)}. Changes require a new approval.` : "This review stays fixed while Application rules is open.";
    $("#approval-acknowledge").disabled = approved || reviewStale || !reviewReady;
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
    const fields = [["Applicant",profile.name || "Not saved"],["Legal first name",profile.first_name || "Not provided"],["Legal last name",profile.last_name || "Not provided"],["Preferred first name",profile.preferred_first_name || "Not provided"],["Email",profile.email || "Not saved"],["Approved work locations",list(profile.approved_work_locations).length ? list(profile.approved_work_locations).join("; ") : "None confirmed"],["Resume",approvalReview.resume.loaded ? approvalReview.resume.filename || "Uploaded resume" : "Not uploaded"],["Approved companies",companies.length ? `${companies.length} selected` : "None selected"],["Official Lever boards",`${Object.keys(rules.company_boards || {}).length} mapped`],["Locations",list(rules.locations).length ? list(rules.locations).join(", ") : "Any location"],["Minimum fit",`${rules.min_score ?? "—"} / 100`],["Maximum required experience",experienceLimit(rules) === null ? "Any level" : experienceLimit(rules) === 0 ? "No experience" : `${experienceLimit(rules)/12} year${experienceLimit(rules) === 12 ? "" : "s"}`],["Prioritize new grads",rules.prefer_new_grad ? "Yes, ranking preference" : "No"],["Daily limit (UTC day)",`${rules.daily_limit ?? "—"} applications`],["Run interval",`${rules.interval_minutes ?? "—"} minutes`],["Allowed systems",list(rules.allowed_ats).length ? list(rules.allowed_ats).map(item => String(item).toUpperCase()).join(", ") : "None"],["Known eligibility",rules.require_known_eligibility === false ? "Not required" : "Required"]];
    const boards = Object.entries(rules.company_boards || {}).map(([company,board]) => `${company} → ${board}`);
    const stale = Boolean(approvalReview.fingerprint && approvalReview.fingerprint !== fingerprint);
    $("#approval-summary").innerHTML = fields.map(([key,value]) => `<div class="approval-row"><span>${key}</span><strong>${escape(value)}</strong></div>`).join("") + (companies.length ? `<div class="approval-companies">Companies: ${escape(companies.join(" · "))}</div>` : "") + (boards.length ? `<div class="approval-companies">Lever boards: ${escape(boards.join(" · "))}</div>` : "") + `<div class="approval-companies">${stale ? "Saved configuration has changed. The details above have been preserved for your review." : "Review the saved details above before acknowledging."}</div><button type="button" class="text-button" data-action="review-refresh">Refresh saved review ${icon("refresh")}</button>`;
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
      ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company","experience_months","graduation_date"].forEach(name => { profileForm.elements[name].value = profile[name] ?? ""; });
      ["work_authorized","needs_sponsorship"].forEach(name => { profileForm.elements[name].value = profile[name] === true ? "true" : profile[name] === false ? "false" : ""; });
      profileForm.elements.skills.value = list(profile.skills).join("\n");
      profileForm.elements.approved_work_locations.value = list(profile.approved_work_locations).join("\n");
      profileForm.elements.answers.value = profile.answers && Object.keys(profile.answers).length ? JSON.stringify(profile.answers,null,2) : "";
      initialized.profile = true;
    }
    if (!dirty.rules && (!rulesForm.contains(document.activeElement) || !initialized.rules)) {
      const rules = state.rules || {};
      rulesForm.elements.companies.value = list(rules.companies).join("\n");
      rulesForm.elements.company_boards.value = rules.company_boards && Object.keys(rules.company_boards).length ? JSON.stringify(rules.company_boards,null,2) : "";
      rulesForm.elements.locations.value = list(rules.locations).join("\n");
      ["min_score","daily_limit","interval_minutes"].forEach(name => { if (rules[name] !== undefined && rules[name] !== null) rulesForm.elements[name].value = rules[name]; });
      rulesForm.elements.max_required_experience_months.value = rules.max_required_experience_months ?? "";
      rulesForm.elements.prefer_new_grad.checked = rules.prefer_new_grad === true;
      rulesForm.elements.require_known_eligibility.checked = rules.require_known_eligibility !== false;
      rulesForm.elements.allowed_ats.checked = list(rules.allowed_ats).includes("lever");
      $("#score-output").value = rulesForm.elements.min_score.value;
      initialized.rules = true;
    }
  }
  function renderState() {
    const jobs = list(state.jobs);
    const stats = state.stats || {};
    $("#stat-total").textContent = formatNumber(stats.total ?? jobs.length);
    $("#stat-queued").textContent = formatNumber(stats.queued ?? jobs.filter(job => ["new","queued"].includes(job.status)).length);
    $("#stat-submitted").textContent = formatNumber(stats.submitted ?? jobs.filter(job => job.status === "submitted").length);
    $("#stat-attention").textContent = formatNumber(stats.needs_attention ?? jobs.filter(jobAttention).length);
    $("#nav-queue-count").textContent = $("#stat-queued").textContent;
    $("#resume-status").textContent = state.resume?.loaded ? state.resume.filename || "Resume uploaded" : "No resume uploaded";
    $("#resume-status").classList.toggle("loaded",Boolean(state.resume?.loaded));
    $("#source-message").textContent = sourceMessage;
    $("#source-message").hidden = !sourceMessage;
    hydrateForms();
    renderSummary();
    renderControls();
    renderReadiness();
    renderJobs();
    renderActivity();
  }
  function refresh(manual = false) {
    if (refreshing) return refreshInFlight;
    refreshing = true;
    clearTimeout(pollTimer);
    refreshInFlight = (async () => {
      try {
        const next = await api("/api/state");
        const wasApproved = Boolean(state.approval?.approved);
        state = {...state,...next};
        loaded = true;
        if (wasApproved && !state.approval?.approved) $("#approval-acknowledge").checked = false;
        renderState();
        return true;
      } catch (error) {
        if (!loaded || manual) showNotice(error.message,true);
        return false;
      } finally {
        refreshing = false;
        pollTimer = setTimeout(() => refresh(),state.automation?.running ? 3000 : 8000);
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
  document.addEventListener("click", event => {
    const viewButton = event.target.closest("[data-view]");
    if (viewButton) { setView(viewButton.dataset.view); return; }
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const name = button.dataset.action;
    const id = button.dataset.id;
    if (name === "details") {
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      const detail = document.getElementById(button.getAttribute("aria-controls"));
      const open = expanded.has(id);
      button.setAttribute("aria-expanded",String(open));
      button.innerHTML = `${open ? "Less detail" : "View details"}${icon("chevron")}`;
      if (detail) detail.hidden = !open;
    } else if (name === "queue" || name === "dismiss") {
      action(`job-${id}`,button,() => post(`/api/jobs/${encodeURIComponent(id)}/status`,{status:name === "queue" ? "queued" : "dismissed"}),name === "queue" ? "Role added to your queue. Submission still follows your approved rules." : "Role dismissed.");
    } else if (name === "applied-elsewhere") {
      const job = list(state.jobs).find(item => String(item.id) === id);
      if (!job || isDemo(job) || job.attempted_at || ["submitted","applied_elsewhere","active","filling","submitting","applying","uncertain"].includes(String(job.status).toLowerCase())) return;
      const canonical = Boolean(leverPostingURL(job.apply_url));
      action(`job-${id}`,button,() => post("/api/applied-elsewhere",{job_id:id}),canonical ? "Previous application recorded. Jobot will skip this exact Lever posting." : "Previous application recorded. Listings with the same company and title will be held for review.");
    } else if (name === "demo") {
      action("demo",button,async () => { await post("/api/demo"); setView("queue"); },"Local demo created. It uses a mock employer and sends no application to a real company.");
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
  $("#history-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    const input = $("#history-url");
    const url = leverPostingURL(input.value.trim());
    if (!url) { showNotice("Enter an exact HTTPS Lever posting or application URL, including its posting ID.",true); input.focus(); return; }
    action("history",$("button[type=submit]",form),async () => { await post("/api/applied-elsewhere",{url}); input.value = ""; },"Previous application recorded locally. This exact Lever posting will be skipped.");
  });
  $("#profile-form").addEventListener("input",() => markDirty("profile"));
  $("#rules-form").addEventListener("input",() => { markDirty("rules"); $("#score-output").value = $("#min-score").value; });
  $("#approval-acknowledge").addEventListener("change",renderControls);
  $("#profile-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    let answers = {};
    try {
      answers = form.elements.answers.value.trim() ? JSON.parse(form.elements.answers.value) : {};
      if (!answers || Array.isArray(answers) || typeof answers !== "object" || Object.values(answers).some(value => typeof value !== "string")) throw new Error();
    } catch { showNotice('Approved answers must be a JSON object of exact questions and text answers, for example: {"Question?": "Answer"}.',true); form.elements.answers.focus(); return; }
    const body = {};
    ["name","first_name","last_name","preferred_first_name","email","phone","location","linkedin","github","portfolio","current_company"].forEach(name => { body[name] = form.elements[name].value.trim(); });
    body.skills = splitLines(form.elements.skills.value);
    body.approved_work_locations = splitLines(form.elements.approved_work_locations.value);
    body.experience_months = form.elements.experience_months.value === "" ? null : Number(form.elements.experience_months.value);
    body.graduation_date = form.elements.graduation_date.value || null;
    body.work_authorized = booleanValue(form.elements.work_authorized.value);
    body.needs_sponsorship = booleanValue(form.elements.needs_sponsorship.value);
    body.answers = answers;
    action("profile",$("button[type=submit]",form),async () => { await post("/api/profile",body); markSaved("profile"); },"Profile saved. Review and approve your rules before running applications.");
  });
  $("#rules-form").addEventListener("submit",event => {
    event.preventDefault();
    const form = event.currentTarget;
    let companyBoards = {};
    try {
      companyBoards = form.elements.company_boards.value.trim() ? JSON.parse(form.elements.company_boards.value) : {};
      if (!companyBoards || Array.isArray(companyBoards) || typeof companyBoards !== "object" || Object.values(companyBoards).some(value => typeof value !== "string" || !value.trim())) throw new Error();
    } catch { showNotice('Company boards must be a JSON object mapping exact company names to official Lever board names, for example: {"Shield AI": "shieldai"}.',true); form.elements.company_boards.focus(); return; }
    const body = {companies:splitLines(form.elements.companies.value),company_boards:companyBoards,locations:splitLines(form.elements.locations.value),allowed_ats:form.elements.allowed_ats.checked ? ["lever"] : [],min_score:Number(form.elements.min_score.value),daily_limit:Number(form.elements.daily_limit.value),interval_minutes:Number(form.elements.interval_minutes.value),require_known_eligibility:form.elements.require_known_eligibility.checked};
    body.max_required_experience_months = form.elements.max_required_experience_months.value === "" ? null : Number(form.elements.max_required_experience_months.value);
    body.prefer_new_grad = form.elements.prefer_new_grad.checked;
    action("rules",$("button[type=submit]",form),async () => { await post("/api/rules",body); markSaved("rules"); },"Rules saved. Review the saved summary and give explicit approval to authorize applications.");
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
    const expectedFingerprint = approvalReview?.fingerprint;
    if (!$("#approval-acknowledge").checked || dirty.profile || dirty.rules || !expectedFingerprint || expectedFingerprint !== state.approval?.candidate_fingerprint) return;
    action("approve",$("#approve-button"),async () => {
      try { return await post("/api/approve",{acknowledge:true,expected_fingerprint:expectedFingerprint}); }
      catch (error) { await refresh(true); throw error; }
    },"Your saved rules are approved. Run the queue when you are ready, or enable scheduled applications.");
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
  $("#run-button").addEventListener("click",event => { if (state.approval?.approved) action("run",event.currentTarget,() => post("/api/run"),"Approved queue run requested. Follow the results in Activity."); });
  $("#refresh-activity").addEventListener("click",event => action("activity",event.currentTarget,() => refresh(true),"Activity refreshed."));
  window.addEventListener("hashchange",() => setView(location.hash.slice(1),false));
  document.addEventListener("visibilitychange",() => { if (!document.hidden) refresh(); });
  $$("svg").forEach(svg => { svg.setAttribute("aria-hidden","true"); svg.setAttribute("focusable","false"); });
  setView(location.hash.slice(1) || "queue",false);
  renderReadiness();
  refresh();
})();
