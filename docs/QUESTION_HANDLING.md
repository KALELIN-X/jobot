# Candidate setup and question handling

This release keeps Jobot as a single-user local application. The changes address repeated factual questions, profile consistency, and the steps between answering a question and continuing a queue. They do not promise successful unattended applications on every employer site.

## Setup

The first-launch walkthrough has six independently saved steps:

1. Legal/preferred identity, contact details, and professional links.
2. US authorization, citizenship, and separate present/future sponsorship answers.
3. Education records, enrollment status, earned GPA/scale, and graduation month.
4. Employment/internship records, completed experience, and relevant history facts.
5. Location, relocation, workplace, salary, travel, deadlines, and bounded availability.
6. A review of the saved facts, records, unknowns, and resume.

The catalog was derived from recurring questions encountered while developing Jobot. It contains no real candidate's answers. New profiles start with unknown personal facts. It is a practical inventory, not an exhaustive dataset or a measured coverage claim.

Skipping an optional field preserves unknown. Explicit No and zero are meaningful answers. A completed walkthrough means the user reviewed setup; it does not establish that every future form is answerable. A usable resume and legal contact details are required to finish setup. Normal application approval remains a separate review.

Education, employment, and internship availability are separate records rather than free-form question answers. Incoming education is not current enrollment. GPA belongs only to a program where it was earned. Month-only dates remain month-only; the app does not invent a day for a form. Users may choose a primary education or employment record for generic fields. A primary preference cannot override an explicit current-school or internship qualifier.

Saves use revision checks. If another tab changes a profile, a stale editor receives a conflict and retains its draft. Old clients that omit the structured profile cannot erase it with an ordinary contact edit. Full structured profile writes require a current revision. Merely opening an old profile does not migrate it or change application history.

## Prediction-free answer resolution

`question_intents.py` is a controlled, deterministic resolver shared by the form planner, queue readiness, and question inbox. It does not call a remote model, rank personal answers by similarity, or manufacture facts.

For each ordinary question it:

1. Normalizes ordinary punctuation, spacing, and supported wording.
2. Identifies a supported single-fact intent and its qualifiers.
3. Resolves a confirmed typed profile fact or an unambiguous applicable record.
4. Checks applicable employer/application-scoped bindings.
5. Considers a compatible legacy exact-question answer when appropriate.
6. Matches the result against the actual enabled control options, or records a review reason.

The result exposes an intent, status, value, source, scope, and explanation. The UI groups related unresolved meanings, while retaining each employer's actual question and choices. A profile change can resolve several paraphrases without saving each spelling separately. Current typed facts take precedence over stale legacy exact answers.

Supported meanings include identity/contact/link fields, common US authorization and sponsorship wording, relocation/workplace preferences, bounded travel and availability questions, compensation policy, internship counts, and qualified education/employment fields. The templates are deliberately bounded. Negative or combined assertions, country ambiguity, multiple applicable records, contradictory facts, or unfamiliar wording can require a specific answer.

Country matters: a saved US authorization answer does not establish authorization in Canada. A question about the country where the user is applying needs explicit US evidence in the job context. Generic Remote text does not supply that evidence. Time matters: one confirmed summer window does not authorize all future summers. Highest degree completed is not the same question as preferred degree for generic fields.

## Learning and scopes

Recognized questions show the proposed meaning and scope before the user confirms reusable learning. `confirm_intent` and the current `intent_id` are required to write a common fact. Merely finding similar text does not promote an answer into the profile.

Ordinary custom answers may be limited to an application or employer. Employer history, company motivation, and personal stories cannot be promoted as global employer-independent answers. A specific dropdown format can be confirmed for that exact question and context. When it bridges a canonical fact to a different choice label, it records a hash of the source fact; changing that fact invalidates the old bridge. It cannot reverse a known boolean answer.

`Profile.answers` remains the backward-compatible exact-question store, with its original bounds. New structured facts and scoped bindings live under `Profile.application_profile`. There is no silent promotion of old strings into new factual claims. Confirmations and walkthrough progress have timestamps; approval fingerprints ignore these metadata-only changes but include material facts, records, and answer scopes.

## Dropdowns

The planner needs a real compatible option from the current form. It does not click the first item or treat a searchable control as a free-text field. Plain Yes/No may map to equivalent simple values, but Yes does not justify “Yes, I am an F-1 student” or “Yes, but I require sponsorship.” Qualified options need their additional facts or an explicitly confirmed scoped choice.

Searchable controls retain the existing bounded read-only choice lookup. A search opens a fresh public form without candidate profile/resume data, types only the user's search query, and records returned options. A query is provisional until one live compatible choice can actually be committed and verified. The worker rechecks the current options before filling.

Required unresolved questions hold that application before submission. Optional unknown or incompatible questions can remain blank and appear as nonblocking review items. An optional legal declaration that the form requires in practice can still stop at the site's native validation.

## Confirming and continuing

**Confirm & remember** saves the answer. A material change revokes old approval and disables scheduling. It does not start a browser or retry an application.

**Confirm & run approved queue** is available only for an already approved configuration and an unattempted held application. It:

1. Checks that the reviewed baseline approval is still valid.
2. Saves the explicitly confirmed answer transactionally.
3. Rechecks profile, rules, resume, posting identity, eligibility, other unanswered questions, and duplicate history.
4. Binds approval to the resulting configuration and queues the eligible unattempted role.
5. Requests the ordinary guarded one-time runner from the dashboard.

A newly polled fingerprint is not proof of review. Unreviewed changes in another tab, running workers, remaining blockers, destination aliases, and attempted or uncertain outcomes prevent the shortcut. Saving can succeed while continuation remains held with a reason. An explicit Stop or disabling automation cancels a pending continuation, including a delayed run request from the dashboard. The shortcut does not enable the schedule. A normal run also rechecks approval immediately before its one permitted submission attempt.

## Site actions

Agreements, signatures, privacy/read acknowledgments, CAPTCHAs, logins, unsupported forms, and uncertain outcomes are separate from reusable factual questions. The dashboard shows actionable holds and a link to the employer form. It does not assert that a user read an unseen document, accept every future agreement, solve verification checks, or retry an uncertain submission automatically.

No credentials or verification codes are added to profile storage. Applicant identity, legal history, and professional experience are never inferred from a willingness to proceed. The existing durable submission ledger remains the authority for duplicate prevention.

## Local storage and release limits

The SQLite database, resume uploads, and screenshots remain in the ignored local data directory. They are private plaintext files protected by the user's operating-system account, not an encrypted multi-user service. Keep them out of Git. Back up the complete data directory with the app stopped when moving installations; a fresh clone has no knowledge of prior applications.

The [platform support guide](PLATFORM_SUPPORT.md) describes the five bounded application adapters and their limits. Ashby, SmartRecruiters and Workday can retain a live application browser for explicit user verification/review and resume; this does not provide autonomous account creation, hosted user accounts, email integration or an external AI service. Existing application history is retained. The new matching policy requires a fresh reviewed approval after upgrade.

## Extending and testing

Add new factual fields and their validation/catalog entries in `candidate_profile.py`. Add only a precise intent template in `question_intents.py`, including country/time/employer qualifiers and the matching write target. Keep planner/readiness/inbox on the shared resolver rather than adding another independent regex path.

For each new template, test ordinary paraphrases, unknowns, contradictory facts, relevant qualifiers, and a nearby phrase that must not match. Test actual dropdown options as well as text values. Scoped bindings need cross-employer/application isolation tests. Do not add real resumes, answers, employer receipts, or credentials to fixtures.

The automated suite uses synthetic profiles and local or mocked browser/HTTP boundaries. API tests cover revision conflicts, schema validation, material approval changes, one-click continuation, and durable duplicate protection. Browser fixture tests cover actual visible selection and submission boundaries. Node tests cover drafts, grouping, review snapshots, explicit scope confirmation, and continuation controls. See the README for isolated test commands and local launch instructions. These checks are not an accuracy metric or a guarantee of every live employer's behavior.
