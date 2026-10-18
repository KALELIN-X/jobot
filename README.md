# Jobot

A private, local job-search workspace: discover US software engineering new-grad roles with direct employer links, compare official job descriptions against your resume, and fill and submit supported applications under rules you explicitly approve.

This is a newly built project. The application worker supports bounded Lever and Greenhouse hosted forms, with synthetic local-browser fixtures for supported controls and submission checks. A separately authorized, guided Greenhouse application was visibly confirmed by the employer during onboarding and recorded in local history; that manual session is separate from automatic adapter testing. Public description retrieval and local form tests do not guarantee unattended success or delivery on every live employer form.

## First-time setup (Windows PowerShell)

The [GitHub repository](https://github.com/KALELIN-X/jobot) stores the source code. GitHub does not run this app or submit applications for you; Jobot runs locally on your Windows computer.

Install **Git for Windows** and **Python 3.12+** first. Python 3.12 is the tested version. Open PowerShell in the parent folder where you want to keep the project, then run these commands one at a time:

```powershell
git clone https://github.com/KALELIN-X/jobot.git
cd jobot
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

If the repository is private, sign in to GitHub with an account that has access when Git asks. Do not put a password or access token in the clone URL. The existing local checkout is `D:\Projects\jobot`; open that folder and run only the setup command when updating its dependencies.

Setup creates the local Python environment, installs the pinned dependencies, and downloads the Playwright browser used for supported forms. Internet access is needed for setup, fetching listings, and real applications. No paid service, AI subscription, API key, or browser extension is required. There is no frontend build step.

## Daily launch

For the existing installation, open PowerShell and run:

```powershell
cd D:\Projects\jobot
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

For a fresh clone elsewhere, replace the first command with its actual folder. Jobot runs independently of Codex once launched.

After the server starts, open [Jobot on this computer](http://127.0.0.1:8765/) in your browser. The launcher prints this address but does not open a tab for you. This loopback address works only on the computer running the app. If Jobot is already running, the launcher reports that and returns rather than starting a second server; keep the original server window open.

**Keep that PowerShell window open.** Closing it or pressing Ctrl+C stops the server and scheduling. Keep the computer awake and online while a run or schedule is active; a browser tab alone does not keep the app running. This is not a Windows background service or cloud scheduler.

On first launch, Jobot starts the local dashboard only. It does not fetch jobs, run applications, or enable automation just because you installed or opened it. Use **Fetch jobs**, complete your profile and rules, and select roles with **Add to queue** or **Load jobs to queue**. Fetching alone leaves roles discovered and unselected. Use **Review & start queue**, review the saved details and acknowledgement, then choose **Approve & run once**, or **Save approval only** and enable scheduling separately. On later launches, your profile and history load from the same local `data/` folder. A schedule you previously enabled resumes if its saved approval is still valid; turn automation off before closing the app if you do not want it to resume next time.

## Start with the local demo

Click **Try local demo**. The same Playwright worker used for applications opens a local mock form, fills the synthetic Alex Example profile, attaches a synthetic text resume, submits once, and checks for confirmation. No real employer is contacted and no personal profile is needed. Demo records are labeled **LOCAL DEMO** and excluded from real application statistics and quotas.

## Configure your own applications

1. **Fetch jobs:** imports up to **100 US software-engineering rows per refresh** from the public SimplifyJobs new-grad list, prioritizing supported direct Lever/Greenhouse links. Official employer GET endpoints supply descriptions and verify the exact posting. Other published application links remain available for manual review. Jobright is a bounded fallback if the primary source is unavailable or has no usable US rows. Before you add a resume, fit scores remain unknown. Uploading a resume reranks saved listings. Cards show location, required experience, stated pay, education, and graduation windows. Open **Checks & details** for blockers and score evidence, or **Open role** for the complete employer posting.
2. **Profile & resume:** enter your legal full name and separate legal first/last names, plus a preferred first name if you use one. The app uses only the name fields you supply; it does not split a full name or substitute a preferred name for a legal name. Add verified contact details, links, skills, experience, and an actual resume PDF or UTF-8 text file. Graduation month accepts `YYYY-MM`; it does not invent a day when you know only a month. Use **Answer memory** below the profile to save reusable answers without editing JSON. Confirm approved work locations separately from your current address, including any relocation you actually authorize. Optional eligibility answers start unknown. The app does not infer citizenship, work authorization, sponsorship, or other personal facts from your resume. Save reusable answers only when they are accurate for the exact question.
3. **Application rules:** choose **Any company from fetched jobs** (the default), a location preset, maximum required experience, new-grad priority, minimum resume match, and daily limit. Company names and board codes are optional; **Only companies I choose** adds an employer restriction. **More settings** holds scheduling, eligibility preferences, and supported application systems. The experience cap compares the role's stated minimum with your chosen cap: a 0–2-year range has a minimum of zero; a 2+-year requirement exceeds a one-year limit. Missing or ambiguous numeric requirements stay unknown and are held when an experience cap is active. A new-grad feed or title never establishes zero years of experience. **Enforce employer graduation windows** starts enabled. Turning it off allows applications despite a detected graduation-window mismatch; your actual saved graduation month remains unchanged and unfamiliar form questions still need accurate answers. New-grad priority affects ordering without changing fit scores. **Only show my experience range** is an optional display filter, off by default. It does not change saved application rules, and selected queued roles always stay visible.
4. **Select your queue:** **Add to queue** lets you individually select any unattempted available role, including low-fit or held roles and application systems that need manual review. **Remove from queue** returns it to discovery without dismissing it. **Load jobs to queue** selects all available roles with a known fit score at or above your saved minimum, across the fetched list rather than just the current search/view. It includes previously dismissed or held roles when their score qualifies; it skips unknown/below-cutoff scores, already selected postings, demos, active applications, attempted submissions, and recorded duplicates. This only selects jobs: it does not approve, run, bypass eligibility, answer questions, or submit them. The selected/ready/held counts distinguish queue membership from submission readiness. Protected records show a disabled Add button with a reason.
5. **Review & approve:** review the saved profile, resume filename, company scope, preferences, and saved application answers. Any-company approval covers current and future fetched jobs that meet those saved preferences. Approval is bound to that exact configuration. Saving a profile, resume, rule, or reusable answer revokes approval and disables scheduling. A stale browser tab cannot approve unseen settings. **Save approval only** does not start a queue run. **Approve & run once** approves that reviewed configuration and requests one run of the selected queue; it does not enable scheduling.
6. **Run or schedule:** **Run approved queue** processes only the roles already selected when that run starts. Newly added roles wait for a later run. Empty or ineligible queues return an actionable message without launching a browser. Each role's details explain its blockers. Enabling scheduling authorizes periodic fetching and automatic selection of newly discovered matching roles, followed by guarded queue processing, while the server remains running. That automatic selection requires a still-valid saved approval and enabled schedule. **Stop before next submission** stops scheduling and requests the worker to stop before another final application request; an application already sent cannot be undone. Check **Activity** for actual progress, results, and questions. Closing and reopening the app does not resume an unfinished employer form.

### Watch a run

The worker panel identifies the active employer and role, the latest observed stage, processed/selected counts, and confirmed, held, uncertain, or failed outcomes. Stages come from actual work: checking the current posting, opening and inspecting the form, filling fields, uploading the resume, checking answers, sending the single final application request, and waiting for confirmation. Field/question counts appear when available; they are not an estimated completion percentage.

The last-stage timestamp and worker heartbeat have different meanings. The timestamp changes when a worker stage is reported; the heartbeat indicates that the worker is still alive while a browser action waits. A fresh heartbeat alone does not mean another application was submitted. **Activity** keeps the stage events and final run summary. A saved run that was active when the app stopped is marked interrupted on restart, rather than silently resumed.

When a worker records a browser capture, the application details provide its latest local PNG through the app's restricted capture route. This is a recorded screenshot, not a live browser stream. It can show entered personal information and stays in private local storage. Captures are served only from that application's artifact directory; arbitrary filesystem paths are not exposed.

### Remember answers from applications

Supported forms record unfamiliar ordinary questions in the **Activity** question inbox and **Profile & resume → Answer memory**, along with the employer, role, input type, and available choices. Required unanswered questions pause that application before submission; optional questions can be left blank. Jobot does not treat prefilled website values or guessed responses as your answers.

In the inbox, answer the actual recorded question: choose one of the form's recorded radio/select options, or enter a written answer for a text question. **Confirm & remember** explicitly saves the answer through the same local answer-memory workflow. You can also use **Answer & remember** in the profile, or add, edit, and forget an exact question/answer directly. Confirm that an answer is accurate and suitable for reuse beyond that employer before saving it.

Saved answers live in `Profile.answers`, survive restarts, and appear under **Application rules → Saved application answers** and in the approval review. They are reused only for the same exact visible question, with whitespace normalized, never by fuzzy or semantic matching. Choice controls must have one exact matching option; a changed choice list needs review. Boolean answers preserve explicit Yes/No values.

Saving or changing an answer revokes application approval and turns scheduling off. Review and approve the updated configuration, then explicitly add the paused role back to the queue if it has no prior attempt. Saving an answer never adds a role to the queue, resumes a form, or retries a submission by itself. Duplicate and uncertain-attempt protections still apply. Keep company-specific answers manual; this library cannot infer whether identical wording has a different meaning at another employer. Legal agreements, credentials, CAPTCHAs, and verification challenges are not reusable answers.

Search preferences and saved answers serve different purposes. Preferences decide which jobs match, including location, experience, company scope, and daily limits. Saved application answers supply facts or wording you confirmed for recurring employer questions. Changing a preference does not invent an answer, and remembering an answer does not relax a job filter. Normal editing uses the visible controls; JSON is optional advanced editing.

This is explicit answer memory, not model training. Jobot cannot observe answers entered in unrelated browser sessions or learn from unsupported application sites.

### Applications you already submitted elsewhere

Use **Already applied** on a saved listing to record a previous application. This persists across restarts and feed refreshes. It also places listings with the same normalized employer and title on hold, including possible reposts with different posting IDs. This is a conservative review hold, not proof that two different postings are the same job.

If the listing is absent, paste its exact supported HTTPS Lever or Greenhouse posting URL into **Already applied elsewhere?** under Application rules. That blocks the canonical posting ID even if a matching row is fetched later, without consuming today's quota. Equivalent posting/application URLs, tracking parameters, and official Greenhouse host aliases share the same history identity. Existing recorded Greenhouse submissions are included, even when an older manually recorded row lacks an automatic-attempt timestamp. Recording history never overwrites evidence of an active, uncertain, or confirmed submission.

Jobot cannot know about applications made outside the app until you record them. A different posting ID or changed employer/title may represent a repost that needs manual review. Do not rely on a one-time dismissal as a complete external-application history.

Approval authorizes sharing your saved profile, reusable answers, and selected resume with employers covered by the displayed company scope when a fetched job passes your rules. With **Any company**, that includes future matching jobs from the configured source; with **Only companies I choose**, only listed employers are covered. Uploads to Jobot itself stay in local storage until an application runs. Do not upload employer-confidential material, credentials, or unrelated personal files.

## Supported discovery and applications

- The public [SimplifyJobs new-grad list](https://github.com/SimplifyJobs/New-Grad-Positions) is the primary source. Only its software-engineering table is used, with positive US location evidence; a bare "Remote" does not establish US eligibility. Source-published employer links produce stable posting IDs. The much larger historical listings JSON is not downloaded. [Jobright's software-engineering list](https://github.com/jobright-ai/2026-Software-Engineer-New-Grad) remains a fallback.
- The dashboard sends compact posting facts instead of repeating full job descriptions in every status poll. Official posting responses are read within the existing byte/time bounds because eligibility and technical resume matching still need their source text; that bounded text stays in local storage. Published salary ranges retain their currency and pay period, without annualizing hourly pay. The existing Greenhouse posting GET includes its documented pay-transparency flag; Lever public salary fields are read when supplied ([Greenhouse Job Board API](https://docs.greenhouse.io/job-board), [Lever Postings API](https://github.com/lever/postings-api/blob/master/README.md)). Missing pay, degree, and graduation constraints are labelled as not stated; short source excerpts remain available under score evidence. No additional employer requests are made to obtain these facts.
- At most 100 returned rows per refresh, three concurrent description requests, a 2 MB source limit, a 1.5 MB individual employer-response limit, a 15-second primary-source deadline, and a 45-second employer-description budget. A failed request leaves that role available with an explanatory hold; it does not discard successful rows or keep background requests running after the response.
- Source-row provenance binds the company/title/location and canonical application destination. New direct-feed applications additionally require matching official Lever or Greenhouse public GET metadata for that exact board/posting ID. Strict HTTPS hosts and posting paths are checked again before filling and submission. No board names are guessed. Missing, unrelated, or conflicting links stay manual. Existing optional Lever board mappings can enrich fallback rows; they are never a required employer restriction.
- Explainable keyword ranking against resume text. Matched terms include evidence snippets; missing terms and eligibility problems remain visible. Narrow PDF-token aliases recover extraction artifacts such as `A WS` and `SpringBoot` without changing the quoted evidence. Explicit one-of language lists count as one requirement; ambiguous alternatives remain separate. Required/preferred section headings affect weights. A missing resume or description yields unknown fit. Scores are ranking heuristics, not calibrated probabilities, hiring predictions, or measured accuracy.
- Lever and Greenhouse hosted forms: supported standard contact/resume fields, saved professional links, and exact approved reusable answers. Greenhouse support includes ordinary labelled file uploads, ARIA-required fields, and bounded single-choice React-select controls selected through their visible choices. Unknown/custom layouts, unsupported choice controls, or a selection that fails native form validation stop for review. Workday, Ashby, login-required flows, account creation, email verification, and resuming a form after verification are not implemented; no account passwords or verification codes are stored.
- Required unfamiliar questions, legal declarations/agreements, CAPTCHAs, logins, destination changes, and invalid forms go to **Needs attention**. This app does not generate personal claims, accept agreements, solve CAPTCHAs, or bypass site restrictions.
- SQLite records submission attempts before the final click. One durable canonical destination ledger covers both supported ATS providers, across feed IDs and recorded historical applications. Limits count attempts per **UTC calendar day**, not confirmed submissions; the maximum configurable daily limit is 20.
- A timeout or crash after a reserved attempt becomes **uncertain**. Such records cannot be automatically requeued. Verify the outcome on the employer site before doing anything manually. Failed attempts remain counted to avoid blind retries.

Feeds are discovery sources, not authoritative inventories of open positions. A disappearing feed row is not proof that a job closed. An official posting's 404/410 response marks that refreshed role closed; other request failures leave availability unknown. Listings without a verified destination remain discoverable but cannot auto-submit. Feed dates describe when a row was listed, not necessarily the employer's original posting date. Foreign locations mentioning US working hours are not treated as US offices. The 100-row refresh limit does not erase saved history, so past application records can exceed 100. This app does not crawl the entire market or every posting on an employer's board.

## Architecture and flow

```text
Local HTML/CSS/JS dashboard
        | same-origin FastAPI endpoints
        v
SimplifyJobs public SWE table -> published Lever/Greenhouse posting link
        |                                  |
        |                    official public GET metadata + provenance
        |                                  |
        +----------> resume ranking <-------+
        |
        +-- unavailable/empty -> bounded Jobright fallback
                        |
       discovered roles -> explicit selection -> SQLite application queue
                              ^
           approved enabled schedule may select matching discoveries
                        |
       exact configuration approval + company scope/location/eligibility rules
                        |
           isolated Playwright browser context
                        |
         discover fields -> check questions -> fill -> recheck approval
                        |
         atomic attempt reservation -> one click -> confirmation/status
```

- `jobradar/sources.py`: direct-link software-engineering discovery, bounded official employer descriptions/pay fields, and source-row binding.
- `jobradar/job_facts.py`: compact, evidence-backed location, experience, education, graduation, and published-pay summaries; missing or ambiguous facts remain explicit.
- `jobradar/ingest.py`: restricted HTTP reads, primary-source entry point, and legacy Jobright parsing/fallback.
- `jobradar/destinations.py`: strict Lever/Greenhouse URL parsing, source verification, and shared canonical posting identities.
- `jobradar/locations.py`: US and custom-location matching without treating "Remote" as worldwide permission.
- `jobradar/refresh.py`: one bounded official posting recheck before form preparation; stale or changed postings are held.
- `jobradar/ranking.py`: resume evidence and separate eligibility checks.
- `jobradar/models.py`: validated configuration and API input models.
- `jobradar/approval.py`: configuration fingerprints, resume integrity, submission rules.
- `jobradar/answer_memory.py`: explicit reusable answers, recorded questions, and answer validation.
- `jobradar/store.py`: SQLite state, atomic claims, deduplication, quotas, recovery.
- `jobradar/autofill.py`: form planning, navigation/request boundaries, browser filling and confirmation.
- `jobradar/form_adapters.py`: bounded DOM snapshots, visible custom-choice discovery, and committed-value validation.
- `jobradar/progress.py`: observed browser-stage callbacks with bounded reporting and no answer contents.
- `jobradar/run_status.py`: active-role status, outcome counts, heartbeat, and persisted run summaries.
- `jobradar/app.py`: local API, scheduler, queue coordination, uploads, synthetic demo.
- `static/`: dependency-free dashboard. No frontend build step.

Before filling a role that passes the saved rules, Jobot rechecks its exact official posting and reranks the current description. Unavailable metadata, changed title/location, or newly failing requirements hold the role before any applicant data is shared. Each run checks at most the number of attempts remaining in the daily budget (never more than 20); each check makes one bounded GET with a 12-second deadline. Failed checks do not consume submission quota.

Each browser run uses an isolated context without importing your browser cookies or passwords. Lever traffic stays on the approved origin. Supported modern Greenhouse forms additionally advertise an exact posting submission endpoint and presigned resume-upload fields. The worker pins the posting, the recognized Greenhouse-owned US upload bucket, the advertised fields, and the selected resume filename/bytes. Uploads can occur during filling, before the final application click. Unknown endpoints or regional upload layouts pause for review. Only one final application POST is permitted, and an upload response cannot count as application confirmation. Public metadata GET endpoints are not application-submission APIs. Approval is rechecked immediately before reserving the attempt and clicking. Changes to resume bytes, listing identity, or the application destination stop the run.

## Tests

Run tests in a separate PowerShell window from the project folder. Set an isolated import directory first: importing `jobradar.app` also constructs its default application, and tests must not open the normal private database.

```powershell
cd D:\Projects\jobot
$jobotTestId = [guid]::NewGuid().ToString("N")
$env:JOBRADAR_DATA_DIR = Join-Path (Get-Location) ("data\test-import-" + $jobotTestId)
try {
    & .\.venv\Scripts\python.exe -m pytest -q --basetemp ("data/test-run-" + $jobotTestId)
    & .\.venv\Scripts\python.exe -m compileall -q jobradar
} finally {
    Remove-Item Env:\JOBRADAR_DATA_DIR -ErrorAction SilentlyContinue
}
```

If Node.js is available, run the dashboard's pure filter-helper checks with `node --test tests/dashboard_helpers.test.cjs`. Node is not required to run the app.

The unique test directories avoid reusing a locked Windows temporary folder. Unit and API tests use synthetic fixtures and mocked HTTP/browser boundaries. `tests/test_greenhouse_forms.py` runs actual Chromium against local synthetic Greenhouse-shaped forms, covering ordinary uploads, visible custom choices, and submission blockers; install the browser with the setup command first. These tests send no candidate data to employers. **Try local demo** provides a separate dashboard-driven browser check. Tests also cover explicit queue selection/removal, safe bulk selection, approved scheduled selection, observable stages and stop requests, capture-path boundaries, public-source bounds, posting identity, historical duplicate protection, US filtering, graduation-month handling, unknown experience, exact answers, legal blockers, approval revocation, quotas, and interrupted-run recovery. No external dataset or accuracy evaluation is claimed.

## Local data and limitations

`data/` contains the SQLite database, resume uploads, synthetic demo files, and application screenshots. It is ignored by Git, as are `.venv/` and `.env`. Screenshots can contain entered profile details. Treat this folder as private; it is local plaintext storage protected by your Windows account, not an encrypted vault. Never publish it. No analytics or remote language-model service is used.

### Keep your application history when moving or reinstalling

A fresh GitHub clone contains code, **not your profile, resume, or application history**. Those records live in the ignored local `data/` folder. A new empty `data/` folder cannot know about past applications, so duplicate protection depends on retaining your existing local history or recording previous applications again.

Before moving the app or replacing a checkout, stop Jobot with Ctrl+C and privately back up the entire `data/` folder. With the app stopped, restore that folder beside `start.ps1` in the new checkout before launching. Keep the backup in a private location; do not commit it, attach it to an issue, or upload it to a public repository. Stopping the server before copying avoids an incomplete SQLite backup. The `.venv/` folder does not need to be copied; rerun setup instead.

This is single-user local software. Do not expose its port publicly or deploy it as a shared website. It uses loopback binding, trusted-host checks, same-origin mutation checks, and a dashboard request header; it has no multi-user authentication. Use only where the relevant site's rules permit automation.

The matching rules and form adapters are conservative and may pause often. Automatic submissions are supported by code and tested locally; employer-specific behavior and delivery still require live validation for each supported form. A new-grad source can contain roles whose experience or graduation requirements do not match the applicant. Turning off graduation-window enforcement does not change education dates, satisfy other requirements, or answer personal questions. Reasonable next improvements are validating a small set of explicitly approved live Lever/Greenhouse forms, adding tested adapters for other ATS providers, and supporting an OS task/service for reliable daily startup. There is currently no cover-letter generation, email integration, account login, resume rewriting, or cloud scheduler.

## Sources and newly built functionality

All application code, the dashboard, ranking, approvals, scheduler, and tests in this directory were newly built for this project. Refinements added explicit queue selection, readiness and approve-and-run controls, observed worker progress, local browser captures, an application-question inbox and exact answer memory, readable cool styling, optional company restrictions, direct-link discovery, official employer metadata verification, Greenhouse form support, shared cross-source application history, and graduation-month/window controls. Listing content comes from SimplifyJobs, Jobright, and public employer postings; it is not an original dataset. Official API references are [Lever Postings API](https://github.com/lever/postings-api) and [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html). Browser automation uses [Playwright](https://playwright.dev/python/), the local server uses [FastAPI](https://fastapi.tiangolo.com/), and PDF extraction uses [pypdf](https://pypdf.readthedocs.io/). No historical project achievements or accuracy metrics are implied.
