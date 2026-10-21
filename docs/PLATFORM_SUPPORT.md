# Platform support and validation scope

This document describes the local Jobot release as checked on **October 21, 2026**. Recognizing a platform, reading a public posting, inspecting visible fields, filling an application and confirming delivery are different capabilities. A successful public check does not grant permission to apply or establish that later steps work.

## Current support matrix

| Platform | Automatic applications | Public check in Jobot | Main boundary |
| --- | --- | --- | --- |
| Lever | Bounded hosted-form adapter | Yes, for verified exact supported posting URLs | Familiar native controls, supplied resume and one guarded submission; unknown layouts, agreements, login and verification pause |
| Greenhouse | Bounded hosted-form adapter | Yes, for verified exact supported posting URLs | Familiar uploads, ARIA-required controls and bounded single-choice React-select; recognized US presigned resume upload transport only; unknown regional layouts pause |
| Ashby | No | Inspection only | Exact hosted posting/application identity and visible controls; candidate-state mutations, uploads and submission are not implemented |
| SmartRecruiters | No | Inspection only | Exact hosted posting or OneClick form and visible controls; multi-step completion, repeated records, uploads and final submission are not implemented |
| Workday | No | No | Complete candidate-account and tenant-specific application steps manually |
| iCIMS | No | No | Complete candidate-portal and employer-specific steps manually |
| Taleo | No | No | Complete account and employer-specific steps manually |
| Rippling | No | No | Complete multi-step candidate form and agreement flow manually |
| SAP SuccessFactors | No | No | Complete candidate-account and employer-specific steps manually |

These entries describe the implemented adapters, not the entire market. Unrecognized hosts remain manual. The nine providers are a capability catalogue, not a representative sample or a measured majority-platform coverage result. Manual providers can remain in discovery, queue and history; queue membership does not make them automatically runnable.

## Using Check platform

1. Fetch roles and open a role's **Checks & details**.
2. Review its platform mode and reason. **Check platform** appears only when this exact destination is inspectable and the saved role is available and unattempted. Duplicate-history, closed, dismissed, active, attempted and uncertain records cannot use the check.
3. Run the public check. Lever/Greenhouse require verified employer metadata; inspection-only providers require a source-published link from the configured SimplifyJobs feed. A missing or changed link is held rather than guessed.
4. Read the result's timestamp, observed stage, gates, field labels/types, required markers, visible options and next action. A posting-only result, zero fields or a blocked request is useful diagnostic information, not a completed application.

The check does not receive the saved candidate profile or resume. It uses a fresh browser context without importing personal browser sessions. It never enters candidate details, uploads, accepts agreements, solves challenges, logs in or submits. A required email-verification or account step remains manual. The report contains bounded public control metadata, not answers, credentials or resume bytes, and is stored with the role for later review. Running a check does not change application approval, reserve an application attempt or claim delivery.

The Ashby inspector pins the exact board/posting and verifies the public document's identity before reporting form fields. SmartRecruiters may follow one unique, visible application link actually advertised by the saved posting, with the same employer and origin; it pins that publication before navigation. It does not invent a OneClick URL from the advertisement's different numeric ID, press **Next**, or inspect every later step.

The transport permits public HTTPS GET/HEAD resources within its resource restrictions, pins document navigation, blocks data-changing requests and closes WebSockets. A site requiring another transport can return a partial/unavailable report. Public availability and layouts can change after a check; automated Lever/Greenhouse runs still perform their own current posting, form, approval and duplicate checks.

## Common controls refined in this release

- **Confirm your email** and supported equivalent confirmation labels reuse the explicitly saved email address. Repeating an email field does not verify address ownership or answer a one-time-code challenge.
- **City** uses a separate optional `application_profile.facts.current_city` value from the identity onboarding step. It is never inferred by splitting a full location such as `Boston, MA, United States`.
- Unknown values remain unknown. These aliases do not add a default city, fabricate personal facts, or make unrelated fields answerable.

## What was tested

### Automated local and mocked checks

All automated fixtures use synthetic profiles, local loopback/in-memory pages or intercepted HTTP boundaries. They send no candidate data to employers. Real Chromium fixtures exercise browser controls; a real browser running synthetic HTML is still a local fixture rather than a live employer completion test.

- Lever-shaped native contact inputs, authorization radio buttons, sponsorship/custom selects, exact supplied resume bytes and one local receipt; unknown questions, agreements, login, CAPTCHA, final approval refusal and HTTP-error outcomes.
- Greenhouse-shaped file controls, required proxies, single-choice dropdowns, searchable/portaled/delayed choices, committed-value validation, advertised posting/upload endpoints and rejected premature/duplicate/unrelated requests.
- Ashby and SmartRecruiters destination parsers, visible field/gate snapshots and provider-specific controls without automatic submission capability.
- Public-inspection route/resource restrictions, candidate-free behavior, unchanged approval and history, unavailable/gated forms, and protected-role rejection.
- Dashboard support modes, bounded/escaped reports, exact field choices, guarded read-only actions, and expanded compatibility details that survive status polling.

The tests do not establish universal live delivery, platform market share, classifier accuracy or a production-readiness certification. See the README for the isolated full-suite and Node commands; no fixed final test count is asserted here.

### Separate live public observations

The following manual public-browser observations were made on October 21, 2026. **No candidate details were entered, no file was uploaded, no account was used or created, and no application was submitted during these checks.** They demonstrate only the observed pages and gates at that time.

| Page inspected | Observed result | What remains unverified |
| --- | --- | --- |
| SmartRecruiters / Western Digital, advertisement `744000142878952` | Its published application link led to OneClick publication `24b39e7f-dfeb-48ef-aa46-c6a6a6b647ce`; the first step displayed controls including **Confirm your email**, **City**, **Resume** and **Next** | Later steps, actual upload, agreement handling, submission and receipt |
| [Ashby / OpenAI application](https://jobs.ashbyhq.com/openai/55150071-fce8-48f5-aea4-14ed78b83511/application) | The public application loaded | Candidate-state changes, uploads, final submission and receipt |
| NVIDIA / Workday, requisition `JR2028162` | Opening **Apply** reached **Create Account / Sign In**; inspection stopped there | Account creation/login, verification and all candidate application steps |

An account gate on this NVIDIA posting does not mean every Workday tenant has the same gate. One accessible SmartRecruiters or Ashby page does not establish support for every employer on those systems. These observations are separate from the automated fixture results and from any earlier authorized applications recorded in the private ledger.

## Public discovery APIs do not grant submission access

Ashby's documented [public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) exposes published job data through a GET endpoint, optionally including compensation. That is a separate surface from [applicationForm.submit](https://developers.ashbyhq.com/reference/applicationformsubmit), which is a POST endpoint requiring `candidatesWrite` permission. Its developer API uses an API key through [HTTP Basic authentication](https://developers.ashbyhq.com/docs/authentication). Jobot does not request employer credentials or use that authenticated submission endpoint.

SmartRecruiters' documented [Application API](https://developers.smartrecruiters.com/docs/application-api) covers screening/configuration, application creation and status. Its endpoints are protected, including the required application-management scope when OAuth is used. A public hosted posting or visible OneClick form is not authorization to call those employer/partner API endpoints. Jobot's new inspector reads public pages; it does not add such an integration.

Future automatic adapters need their own tested navigation, repeated-record/control handling, candidate-state/upload transport, factual answer mapping, agreement and verification boundaries, one-attempt reservation and reliable receipt detection. Merely adding a host to an allowlist or reusing a generic submit click would not supply these capabilities.
