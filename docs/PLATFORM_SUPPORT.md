# Platform support and validation scope

This document describes the local Jobot release as checked on **October 21, 2026**. Recognizing a platform, reading a public posting, inspecting visible fields, filling an application and confirming delivery are different capabilities. A successful public check does not grant permission to apply or establish that later steps work.

## Current support matrix

| Platform | Automatic applications | Public check in Jobot | Main boundary |
| --- | --- | --- | --- |
| Lever | Bounded hosted-form adapter | Yes, for verified exact supported posting URLs | Familiar native controls, supplied resume and one guarded submission; unknown layouts, agreements, login and verification pause |
| Greenhouse | Bounded hosted-form adapter | Yes, for verified exact supported posting URLs | Familiar uploads, ARIA-required controls and bounded single-choice React-select; recognized US presigned resume upload transport only; unknown regional layouts pause |
| Ashby | Bounded, resumable hosted-form adapter | Yes, independently read-only | Posting/render-bound answer operations, exact supplied file and one final attempt; unknown renderer operations and user-only steps pause |
| SmartRecruiters | Bounded, resumable OneClick adapter | Yes, independently read-only | Source advertisement bound to its published application UUID; recognized steps, exact resume and one final request; unknown layouts/transports pause |
| Workday | Bounded, resumable tenant application adapter | No independent public check | Exact tenant/site/requisition, response-created draft, recognized repeated records and selected resume; the user handles account access/verification; unknown tenant variants pause |
| iCIMS | No | No | Complete candidate-portal and employer-specific steps manually |
| Taleo | No | No | Complete account and employer-specific steps manually |
| Rippling | No | No | Complete multi-step candidate form and agreement flow manually |
| SAP SuccessFactors | No | No | Complete candidate-account and employer-specific steps manually |

These entries describe the implemented adapters, not the entire market. Unrecognized hosts remain manual. The nine providers are a capability catalogue, not a representative sample or a measured majority-platform coverage result. Manual providers can remain in discovery, queue and history; queue membership does not make them automatically runnable.

Fresh rules enable the five bounded automatic providers. Existing saved provider selections are preserved; adding providers requires saving and approving the changed rules. Application policy version 6 requires a fresh reviewed approval after upgrade. Local browser tests establish the listed implementation boundaries, not successful live delivery on every employer.

## Using Check platform

1. Fetch roles and open a role's **Checks & details**.
2. Review its platform mode and reason. **Check platform** appears only when this exact destination is inspectable and the saved role is available and unattempted. Duplicate-history, closed, dismissed, active, attempted and uncertain records cannot use the check.
3. Run the public check for an inspectable provider. Its exact destination must pass current source and employer-metadata requirements; a missing or changed link is held rather than guessed. Workday has an approved application-browser workflow rather than a separate **Check platform** inspector.
4. Read the result's timestamp, observed stage, gates, field labels/types, required markers, visible options and next action. A posting-only result, zero fields or a blocked request is useful diagnostic information, not a completed application.

The check does not receive the saved candidate profile or resume. It uses a fresh browser context without importing personal browser sessions. It never enters candidate details, uploads, accepts agreements, solves challenges, logs in or submits. A required email-verification or account step remains manual. The report contains bounded public control metadata, not answers, credentials or resume bytes, and is stored with the role for later review. Running a check does not change application approval, reserve an application attempt or claim delivery.

The Ashby inspector pins the exact board/posting and verifies the public document's identity before reporting form fields. SmartRecruiters may follow one unique, visible application link actually advertised by the saved posting, with the same employer and origin; it pins that publication before navigation. It does not invent a OneClick URL from the advertisement's different numeric ID, press **Next**, or inspect every later step.

The public-check transport permits public HTTPS GET/HEAD resources within its resource restrictions, pins document navigation, blocks data-changing requests and closes WebSockets. A site requiring another transport can return a partial/unavailable report. Public availability and layouts can change after a check; application adapters independently recheck the current posting, form, approved configuration and duplicate history before writing.

## Approved application browsers and human handoff

Ashby, SmartRecruiters and Workday application runs use separate ephemeral browsers. Those approved runs are different from the data-free inspector above: they may write confirmed answers, create employer-side drafts and upload the selected resume before the final submission attempt is reserved. A draft or upload is not counted as a confirmed application or final submission receipt.

When a recognized flow needs account access, verification, an agreement or an unfamiliar answer, it pauses. Jobot can continue other eligible queue entries while that browser waits. Use **Show browser** in Activity or the role's details, complete the displayed step yourself, acknowledge it and choose **Resume**. **Close browser** cancels that live session. Passwords, account creation, one-time codes and CAPTCHA completion belong to the user in that browser; they are not autofilled or promoted into reusable profile answers.

Resume is allowed only while the exact profile, resume, rules and posting binding remain approved and unchanged. A browser acknowledgment applies to its displayed step; it does not become consent for unseen agreements or unknown questions on later steps. Manually entered answers do not silently train a global answer. Changing configuration requires closing the old context and starting a newly reviewed flow.

There are at most **six live application sessions**, including paused browsers. A session becomes unavailable to resume after **one hour without activity**. Keep both the server and that separate browser open. The app does not import personal Chrome cookies or save authenticated state for another launch. Restarted pauses become interrupted review holds; it cannot restore the employer's unfinished form automatically. Submitted, reserved and uncertain attempts cannot be resumed or automatically retried.

### Provider transport boundaries

- **Ashby:** the application adapter uses the public hosted renderer's exact posting/render definition, recognized field operations and advertised upload fields. Supported city and school searches use returned provider IDs, never invented IDs. It does not grant a general GraphQL or same-origin POST capability. Unknown render mutations or presigned-upload variants remain held.
- **SmartRecruiters:** the adapter binds the source advertisement to one explicitly published OneClick publication rather than guessing its different UUID. Its recognized multi-step operations, selected-resume transport and final request remain distinct from its authenticated employer/partner Application API. Human challenge transport must be advertised by the current known-provider frame and form; unknown challenge APIs stay held. Agreement scopes must match the public form definition and require separate user review. Unknown repeated controls, social/import/assessment flows and new transport variants need review.
- **Workday:** strict parsing binds the hosted tenant, external site and requisition; a current public CXS detail response must match its advertised external URL, site, requisition and posting slug. The public renderer can create one draft for that exact verified posting; its returned application ID pins subsequent recognized section writes. The provider handles only known section/method/phase combinations, ordinary visible inputs/dropdowns, and repeated education/employment groups it can identify. Missing dates and record qualifiers are not invented. Unsupported widgets or repeated-section structures pause.
- **Workday uploads/finalization:** only the exact selected resume filename and bytes may use the recognized application-resume or tenant common-attachments transport. A common upload's returned file ID binds the subsequent attachment link; unrelated files/fields are refused. Finalization is allowed once for the bound application after the shared approval check and atomic reservation. Missing, failed, duplicate or mismatched final receipts remain uncertain.
- **Workday account handoff:** the inspected public bundles post email/password account actions to `/{locale}/{site}/login`, `/register`, `/resendactivationinfo` and `/initiatereset`. Jobot permits those exact same-origin, form-encoded routes only while the user handles a login/verification pause. Any redirect must return to the same bound requisition; a locale root must have been observed on that posting. The user enters and submits account details; Jobot does not read or retain the account response or credentials. Unobserved verification-link, OTP and external SSO transports remain held. Complete such verification through the employer's ordinary site and sign in again in the retained application browser before resuming.

The Workday endpoint shapes were inspected in the public [candidate-experience apply-flow asset](https://wd5.myworkdaycdn.com/wday/asset/candidate-experience-apply-flow/candidate-experience-apply-flow.min.js) and an exact public NVIDIA CXS detail response on October 21, 2026. This is observed renderer behavior, **not a published universal Workday application API guarantee**. No employer integration credentials are used. Tenant-specific account/SSO routes or write operations outside the recognized boundary stay held.

## Discovery bounds and posting identity

The feed still returns at most 100 US software-engineering rows per refresh, with three concurrent employer-description requests and a 45-second description budget. The source limit is 2 MB; individual employer responses are capped at 1.5 MB except the 5 MB Ashby board response, shared once per endpoint during that fetch. A board exceeding that limit fails visibly; Jobot does not remove its bounds to retrieve an entire market.

Public GET data binds supported posting identity before an approved application. Strict canonical history keys cover the five providers across source rows. SmartRecruiters advertisement IDs and application publication UUIDs are deliberately distinct. Workday's location-encoding variants cannot authorize another tenant, site or requisition. None of these identities infer that an outside application was submitted; record outside applications explicitly to preserve duplicate protection.

## Common controls refined in this release

- **Confirm your email** and supported equivalent confirmation labels reuse the explicitly saved email address. Repeating an email field does not verify address ownership or answer a one-time-code challenge.
- **City** uses a separate optional `application_profile.facts.current_city` value from the identity onboarding step. It is never inferred by splitting a full location such as `Boston, MA, United States`.
- Unknown values remain unknown. These aliases do not add a default city, fabricate personal facts, or make unrelated fields answerable.

## What was tested

### Automated local and mocked checks

All automated fixtures use synthetic profiles, local loopback/in-memory pages or intercepted HTTP boundaries. They send no candidate data to employers. Real Chromium fixtures exercise browser controls; a real browser running synthetic HTML is still a local fixture rather than a live employer completion test.

- Lever-shaped native contact inputs, authorization radio buttons, sponsorship/custom selects, exact supplied resume bytes and one local receipt; unknown questions, agreements, login, CAPTCHA, final approval refusal and HTTP-error outcomes.
- Greenhouse-shaped file controls, required proxies, single-choice dropdowns, searchable/portaled/delayed choices, committed-value validation, advertised posting/upload endpoints and rejected premature/duplicate/unrelated requests.
- Ashby and SmartRecruiters destination parsers, visible field/gate snapshots, posting/render/publication-bound mutations, multi-step flow and exact-file/final-request boundaries.
- Workday identity and public metadata, both observed exact posting identifiers, typed repeated records, native inputs/choices, direct and common-attachment resume transport, final receipt, and rejected wrong/stale/duplicate or uncertain outcomes.
- Actual provider integration with `ApplicationSessions` and synthetic local HTTP forms: human login, a simulated out-of-band email-verification wait followed by sign-in, retained same-browser state, explicit resume acknowledgment, one final atomic reservation/request and no session reuse after completion. Workday account-route checks use the four observed path shapes and reject guessed OTP/password endpoints, wrong tenants/sites/requisitions, unobserved locales and unsafe redirects. These checks use no real account credentials and do not establish a live account or OTP integration.
- Public-inspection route/resource restrictions, candidate-free behavior, unchanged approval and history, unavailable/gated forms, and protected-role rejection.
- Dashboard support modes, bounded/escaped reports, exact field choices, guarded read-only actions, and expanded compatibility details that survive status polling.

The tests do not establish universal live delivery, platform market share, classifier accuracy or a production-readiness certification. See the README for the isolated full-suite and Node commands; no fixed final test count is asserted here.

### Separate live public observations

The following manual public-browser observations were made on October 21, 2026. **No candidate details were entered, no file was uploaded, no account was used or created, and no application was submitted during these checks.** They demonstrate only the observed pages and gates at that time.

| Page inspected | Observed result | What remains unverified |
| --- | --- | --- |
| SmartRecruiters / Western Digital, advertisement `744000142878952` | Its published application link led to OneClick publication `24b39e7f-dfeb-48ef-aa46-c6a6a6b647ce`; the first step displayed controls including **Confirm your email**, **City**, **Resume** and **Next** | Later steps, actual upload, agreement handling, submission and receipt |
| [Ashby / OpenAI application](https://jobs.ashbyhq.com/openai/55150071-fce8-48f5-aea4-14ed78b83511/application) | The public application loaded | Candidate-state changes, uploads, final submission and receipt |
| NVIDIA / Workday, requisition `JR2028162` | Opening **Apply** reached **Create Account / Sign In**; **Sign in with email** displayed blank email/password fields | Account creation/login, verification and all candidate application steps |

An account gate on this NVIDIA posting does not mean every Workday tenant has the same gate. One accessible SmartRecruiters or Ashby page does not establish support for every employer on those systems. These observations are separate from the automated fixture results and from any earlier authorized applications recorded in the private ledger.

## Public discovery APIs do not grant submission access

Ashby's documented [public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) exposes published job data through a GET endpoint, optionally including compensation. That is a separate surface from [applicationForm.submit](https://developers.ashbyhq.com/reference/applicationformsubmit), which is a POST endpoint requiring `candidatesWrite` permission. Its developer API uses an API key through [HTTP Basic authentication](https://developers.ashbyhq.com/docs/authentication). Jobot does not request employer credentials or use that authenticated submission endpoint.

SmartRecruiters' documented [Application API](https://developers.smartrecruiters.com/docs/application-api) covers screening/configuration, application creation and status. Its endpoints are protected, including the required application-management scope when OAuth is used. A public hosted posting or visible OneClick form is not authorization to call those employer/partner API endpoints. Jobot's new inspector reads public pages; it does not add such an integration.

Expanding these adapters or adding another provider requires tested navigation, repeated-record/control handling, candidate-state/upload transport, factual answer mapping, agreement and verification boundaries, one-attempt reservation and reliable receipt detection. Merely adding a host to an allowlist or reusing a generic submit click would not supply those capabilities. The four manual providers remain unimplemented; the five implemented adapters do not constitute a measured majority of application sites.
