# Jobot

A private, local job-search workspace: fetch software engineering listings from Jobright's public GitHub list, compare job descriptions against your resume, and fill and submit supported applications under rules you explicitly approve.

This is a newly built project. Its automated browser worker has been verified against a local mock employer using a synthetic identity and resume. A separately authorized, guided Greenhouse application was submitted through the browser during onboarding, visibly confirmed by the employer, and recorded in local history. That guided submission does not establish automatic Greenhouse support. Live forms still need adapter-specific validation; this is not a guarantee of unattended success on every application site.

## First-time setup (Windows PowerShell)

The [GitHub repository](https://github.com/KALELIN-X/jobot) stores the source code. GitHub does not run this app or submit applications for you; Jobot runs locally on your Windows computer.

Install **Git for Windows** and **Python 3.12+** first. Python 3.12 is the tested version. Open PowerShell in the parent folder where you want to keep the project, then run these commands one at a time:

```powershell
git clone https://github.com/KALELIN-X/jobot.git
cd jobot
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

If the repository is private, sign in to GitHub with an account that has access when Git asks. Do not put a password or access token in the clone URL. If you already have this checkout, open PowerShell in its current project folder (`job-radar` for the earlier checkout) and run only the setup command.

Setup creates the local Python environment, installs the pinned dependencies, and downloads the Playwright browser used for supported forms. Internet access is needed for setup, fetching listings, and real applications. No paid service, AI subscription, API key, or browser extension is required. There is no frontend build step.

## Daily launch

Open PowerShell in your project folder (`jobot` for a new clone, or the existing `job-radar` checkout) and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

After the server starts, open [Jobot on this computer](http://127.0.0.1:8765/) in your browser. The launcher prints this address but does not open a tab for you. This loopback address works only on the computer running the app. If Jobot is already running, the launcher reports that and returns rather than starting a second server; keep the original server window open.

**Keep that PowerShell window open.** Closing it or pressing Ctrl+C stops the server and scheduling. Keep the computer awake and online while a run or schedule is active; a browser tab alone does not keep the app running. This is not a Windows background service or cloud scheduler.

On first launch, Jobot starts the local dashboard only. It does not fetch jobs, run applications, or enable automation just because you installed or opened it. Use **Fetch jobs**, complete your profile and rules, then use **Review & start queue**. The queue shows exact missing settings and separates pending roles from roles that pass every rule. After reviewing the saved details and acknowledgement, choose **Approve & run once**, or **Save approval only** and enable scheduled applications separately. On later launches, your profile and history load from the same local `data/` folder. A schedule you previously enabled resumes if its saved approval is still valid; turn automation off before closing the app if you do not want it to resume next time.

## Start with the local demo

Click **Try local demo**. The same Playwright worker used for applications opens a local mock form, fills the synthetic Alex Example profile, attaches a synthetic text resume, submits once, and checks for confirmation. No real employer is contacted and no personal profile is needed. Demo records are labeled **LOCAL DEMO** and excluded from real application statistics and quotas.

## Configure your own applications

1. **Fetch jobs:** imports up to 40 current feed rows per refresh and reads their public descriptions. Before you add a resume, fit scores remain unknown. Uploading a resume reranks saved listings. Search the queue, inspect evidence and eligibility in **View details**, and dismiss unwanted listings.
2. **Profile & resume:** enter your legal full name and separate legal first/last names, plus a preferred first name if you use one. The app uses only the name fields you supply; it does not split a full name or substitute a preferred name for a legal name. Add verified contact details, links, skills, experience, and an actual resume PDF or UTF-8 text file. Use **Answer memory** below the profile to save reusable answers without editing JSON. Confirm approved work locations separately from your current address, including any relocation you actually authorize. Optional eligibility answers start unknown. The app does not infer citizenship, work authorization, sponsorship, or other personal facts from your resume. Save reusable answers only when they are accurate for the exact question.
3. **Application rules:** choose **Any company from fetched jobs** (the default), a location preset, maximum required experience, new-grad priority, minimum resume match, and daily limit. You do not need company names or Lever board codes. **Only companies I choose** is an optional restriction. **More settings** holds the scheduling interval, unclear-eligibility preference, and supported-form switch. The experience limit compares the role’s stated minimum with your chosen cap: a 0–2-year range has a minimum of zero; a 2+-year requirement exceeds a one-year limit. Unknown requirements remain unknown. New-grad priority changes ordering without overriding eligibility or changing the fit score. To inspect other roles, turn off **Match my experience filter** in the queue.
4. **Review & approve:** review the saved profile, resume filename, company scope, and limits. Any-company approval covers current and future fetched jobs that meet those saved preferences. Approval is bound to that exact configuration. Saving a profile, resume, or rule revokes approval and disables scheduling. A stale browser tab cannot approve unseen settings. **Save approval only** does not start a queue run. **Approve & run once** approves that reviewed configuration and requests a single run; it does not enable scheduling. When no roles pass all saved rules, the app explains why and lets you save approval while you review those roles.
5. **Run or schedule:** **Run approved queue** runs eligible discovered/queued listings once. **Ready to apply** counts only roles that pass saved job rules; pending and held counts remain visible. Each role’s details list submission blockers. Empty or ineligible queues return an actionable message without launching a browser. Enable scheduled applications only when you want periodic refreshes and runs while the server remains running. Turning scheduling off also requests the active worker to stop before its next submission; an already completed click cannot be undone. Check **Activity** and **Needs attention** for results and questions that require your input. Closing and reopening the app does not resume an unfinished employer form.

### Remember answers from applications

Supported forms record unfamiliar ordinary questions under **Profile & resume → Answer memory**, along with the employer, role, input type, and available choices. Required unanswered questions pause that application before submission; optional questions can be left blank. Jobot does not treat prefilled website values or guessed responses as your answers.

Select **Answer & remember**, provide an accurate answer, and acknowledge that it is suitable for reuse beyond that employer. You can also add, edit, or forget an exact question/answer directly. Answers are stored locally in the existing profile, survive restarts, and are reused only for that same visible question (with whitespace normalized), never by a fuzzy or semantic match. Choice controls must have one exact matching option; a changed choice list needs review. Boolean answers preserve explicit Yes/No values.

Saving or changing an answer revokes application approval and turns scheduling off. Review the updated saved answers during approval, then explicitly add a paused role back to the queue if it has no prior attempt. Saving an answer never retries an application by itself. Duplicate and uncertain-attempt protections still apply. Keep company-specific answers manual; this library cannot infer whether identical wording has a different meaning at another employer. Legal agreements, credentials, CAPTCHAs, and verification challenges are not reusable answers.

This is explicit answer memory, not model training. Jobot cannot observe answers entered in unrelated browser sessions or learn from unsupported application sites.

### Applications you already submitted elsewhere

Use **Already applied** on a saved listing to record a previous application. This persists across restarts and feed refreshes. It also places listings with the same normalized employer and title on hold, including possible reposts with different posting IDs. This is a conservative review hold, not proof that two different postings are the same job.

If the listing is absent, paste its exact HTTPS Lever posting/application URL into **Already applied elsewhere?** under Application rules. That blocks the canonical posting ID even if a matching row is fetched later, without consuming today's quota. Recording history never overwrites evidence of an active, uncertain, or confirmed submission.

Jobot cannot know about applications made outside the app until you record them. A different posting ID or changed employer/title may represent a repost that needs manual review. Do not rely on a one-time dismissal as a complete external-application history.

Approval authorizes sharing your saved profile, reusable answers, and selected resume with employers covered by the displayed company scope when a fetched job passes your rules. With **Any company**, that includes future matching jobs from the configured source; with **Only companies I choose**, only listed employers are covered. Uploads to Jobot itself stay in local storage until an application runs. Do not upload employer-confidential material, credentials, or unrelated personal files.

## What the first version supports

- Jobright's public [2026 Software Engineer New Grad list](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad), bounded HTTP reads, stable listing IDs, and saved-job refreshes.
- Public Jobright descriptions and source-published application links. Supported automatic destinations must have a strict HTTPS Lever posting URL and matching source provenance tied to the primary job record. Missing, unrelated, or conflicting links stay available for manual review and cannot auto-submit. Optional existing board mappings can still enrich missing links, but they are not required in the normal UI. Employer slugs are never guessed from company names.
- Explainable keyword ranking against resume text. Matched terms include evidence snippets; missing terms and eligibility problems remain visible. Narrow PDF-token aliases recover extraction artifacts such as `A WS` and `SpringBoot` without changing the quoted evidence. Explicit one-of language lists count as one requirement; ambiguous alternatives remain separate. Required/preferred section headings affect weights. A missing resume or description yields unknown fit. Scores are ranking heuristics, not calibrated probabilities, hiring predictions, or measured accuracy.
- Lever forms only: standard contact/resume fields, approved links, and exact saved answers where supported. Greenhouse, Workday, Ashby, login-required flows, and unknown custom form layouts are not automatically submitted. A separately guided Greenhouse session is not part of the automatic queue. Workday account creation, email verification, and resuming a form after verification are not implemented; no account passwords or verification codes are stored.
- Required unfamiliar questions, legal declarations/agreements, CAPTCHAs, logins, destination changes, and invalid forms go to **Needs attention**. This app does not generate personal claims, accept agreements, solve CAPTCHAs, or bypass site restrictions.
- SQLite records submission attempts before the final click. A unique destination key prevents duplicate feed records from submitting to the same Lever posting. Limits count attempts per **UTC calendar day**, not confirmed submissions; the maximum configurable daily limit is 20.
- A timeout or crash after a reserved attempt becomes **uncertain**. Such records cannot be automatically requeued. Verify the outcome on the employer site before doing anything manually. Failed attempts remain counted to avoid blind retries.

The feed is a discovery source, not an authoritative list of open positions. A row disappearing from the feed is not treated as proof that the job closed. Listings without a verified destination remain discoverable but cannot auto-submit. Feed dates describe when a row was listed, not necessarily when an employer originally posted the job. This version examines the first 40 feed rows per refresh; it does not crawl the whole market or add every posting on an employer's board.

## Architecture and flow

```text
Local HTML/CSS/JS dashboard
        | same-origin FastAPI endpoints
        v
Public Jobright feed -> primary job details -> source-published employer link
        |                                |
        +--------> resume ranking <------+
                        |
                SQLite application queue
                        |
       exact configuration approval + company scope/location/eligibility rules
                        |
           isolated Playwright browser context
                        |
         discover fields -> check questions -> fill -> recheck approval
                        |
         atomic attempt reservation -> one click -> confirmation/status
```

- `jobradar/ingest.py`: bounded public source parsing, primary-job binding, and application-link provenance.
- `jobradar/ranking.py`: resume evidence and separate eligibility checks.
- `jobradar/models.py`: validated configuration and API input models.
- `jobradar/approval.py`: configuration fingerprints, resume integrity, submission rules.
- `jobradar/answer_memory.py`: explicit reusable answers, recorded questions, and answer validation.
- `jobradar/store.py`: SQLite state, atomic claims, deduplication, quotas, recovery.
- `jobradar/autofill.py`: form planning, navigation/request boundaries, browser filling and confirmation.
- `jobradar/app.py`: local API, scheduler, queue coordination, uploads, synthetic demo.
- `static/`: dependency-free dashboard. No frontend build step.

Each browser run uses an isolated context without importing your browser cookies or passwords. Real application traffic is restricted to the approved origin; a site needing a separate upload origin may stop for review. Approval is rechecked immediately before reserving the attempt and clicking. Changes to resume bytes or the application destination stop the run.

## Tests

```powershell
& .\.venv\Scripts\python.exe -m pytest -q --basetemp=data/test-run
& .\.venv\Scripts\python.exe -m compileall -q jobradar
```

If Node.js is available, run the dashboard's pure filter-helper checks with `node --test tests/dashboard_helpers.test.cjs`. Node is not required to run the app.

Use a fresh `--basetemp` name if Windows temporary-folder permissions prevent pytest from cleaning a prior test directory. Unit and API tests use fixtures and mocked network/browser boundaries; they do not contact employers. **Try local demo** provides the separate real-browser integration check. Tests cover parsing and URL restrictions, resume evidence, eligibility, field planning, missing answers, legal blockers, approval revocation, stale reviews, duplicate protection, attempt quotas, and interrupted-run recovery. No external dataset or accuracy evaluation is claimed.

## Local data and limitations

`data/` contains the SQLite database, resume uploads, synthetic demo files, and application screenshots. It is ignored by Git, as are `.venv/` and `.env`. Screenshots can contain entered profile details. Treat this folder as private; it is local plaintext storage protected by your Windows account, not an encrypted vault. Never publish it. No analytics or remote language-model service is used.

### Keep your application history when moving or reinstalling

A fresh GitHub clone contains code, **not your profile, resume, or application history**. Those records live in the ignored local `data/` folder. A new empty `data/` folder cannot know about past applications, so duplicate protection depends on retaining your existing local history or recording previous applications again.

Before moving the app or replacing a checkout, stop Jobot with Ctrl+C and privately back up the entire `data/` folder. With the app stopped, restore that folder beside `start.ps1` in the new checkout before launching. Keep the backup in a private location; do not commit it, attach it to an issue, or upload it to a public repository. Stopping the server before copying avoids an incomplete SQLite backup. The `.venv/` folder does not need to be copied; rerun setup instead.

This is single-user local software. Do not expose its port publicly or deploy it as a shared website. It uses loopback binding, trusted-host checks, same-origin mutation checks, and a dashboard request header; it has no multi-user authentication. Use only where the relevant site's rules permit automation.

The matching rules and form adapter are deliberately conservative and may pause often. Automatic submissions are supported by code and tested locally, but employer-specific behavior and delivery have not been proven in real applications. Reasonable next improvements are validating a small set of explicitly approved live Lever forms, adding tested adapters for other ATS providers, and supporting an OS task/service for reliable daily startup. There is currently no cover-letter generation, email integration, account login, resume rewriting, or cloud scheduler.

## Sources and newly built functionality

All application code, the dashboard, ranking, approvals, scheduler, and tests in this directory were newly built for this project. Queue readiness diagnostics, combined approve-and-run controls, readable cool dashboard styling, explicit answer memory, and optional company restrictions with default any-company discovery were added during refinement. Listing content comes from Jobright and employer boards; it is not an original dataset. Lever's public API is documented in its [Postings API repository](https://github.com/lever/postings-api). Browser automation uses [Playwright](https://playwright.dev/python/), the local server uses [FastAPI](https://fastapi.tiangolo.com/), and PDF extraction uses [pypdf](https://pypdf.readthedocs.io/). No historical project achievements or accuracy metrics are implied.
