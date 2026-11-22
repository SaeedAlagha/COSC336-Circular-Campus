# Requirements → implementation → verification checklist

**Sources.**
- **P6** — Phase 6 slides 2–5.
- **IM** — instructor message: C1–C6 and AI 2 + AI 5 mandatory; C7–C9 and AI 1, 3, 4 bonus.
- **P3** — Phase 3 SRS v1.4 (FR/BR/NFR/AI/SUS IDs).
- **D** — Phase 4–5 design v3.1 (section numbers).

**Status legend.**
- **✅ Verified** — implemented, and an automated test and/or browser check passed in this build environment.
- **🟡 Not verified here** — implemented, but not exercised in this environment.
- **⚙️ Needs local config** — works only after you configure something (key / database).
- **❌ Not implemented.**

Test names refer to `apps/<app>/tests.py`. "Browser" means a Playwright run against the real server (see TEST_REPORT.md).

## 1. Phase 6 features

| ID | Requirement (exact source) | Module / screen / records | Verification | Status |
|---|---|---|---|---|
| **C1** Login & roles *(mandatory)* | P6 s3 "≥3 roles; server checks permissions; hiding a button is not enough"; P3 FR-01–04, FR-46, BR-01; D 4.6.1 | `accounts/policy.py` (deny-by-default), `accounts/services.py`, `ActiveAccountMiddleware`, `DeniedAccessAuditMiddleware`; screens Sign-in, Accounts, Departments; tables `accounts_user`, `accounts_role`, `accounts_userrole` (scoped grants), `accounts_department` | `accounts.AuthTests.*`, `accounts.DirectAccessTests.*` (direct GET/POST denied, sysadmin cannot approve or self-grant approver, generic login error, deactivation ends session, password ≥12 + confirm, salted hash); crawl of every URL × 15 users (no 5xx) | ✅ |
| **C2** Departments & assets *(mandatory)* | P6 s3; P3 FR-05, FR-06, FR-09, FR-47, BR-06 | `catalog/services.py`, `catalog/forms.py`; screens Register/Edit asset, Asset detail, Departments; tables `catalog_asset`, `catalog_category` (tree), `catalog_attachment`, `maintenance_lifecycleevent` | `catalog.RegistrationTests.*` (Draft, unknowns stay NULL, duplicate ID, negative values, form errors, custody immutable via edit, corrections need reason, stale edit), `catalog.AttachmentTests.*` (content-sniffed JPEG/PNG/PDF, size, protected download) | ✅ |
| **C3** Publish & search *(mandatory)* | P6 s3; P3 FR-07, FR-08, FR-14 | `catalog.services.publish/unpublish`, `catalog.views.CatalogView`; screen Catalog (keyword, category, condition, department + location, min free, status, age, value, sort) | `catalog.PublicationAndSearchTests.*` (unpublish preserves holds, poor stock not publishable, all main filters, unknown age excluded, inverted range error); browser catalog | ✅ |
| **C4** Request & track *(mandatory)* | P6 s3; P3 FR-10–13, FR-48–50, BR-08–10, BR-15 | `exchange/services.py`; screens New request, Request detail (progress, holds, versions, decisions), My requests | `workflow.RequestAndHoldTests.*` (48 h hold, past date, partial choice, expiry keeps need open, allocated never expires, idempotent resubmit, withdraw), `workflow.TransferTests.test_cancellation_*`; browser E2E | ✅ |
| **C5** Approve & transfer *(mandatory)* | P6 s3 "≥1 approval, no self-approval; receiver confirms; owner and department change; last item cannot go to two people"; P3 FR-15–18, FR-49, FR-51, FR-52, BR-02, BR-07, BR-11, BR-13; D 4.5.1 | `workflow/services.py` (`decide`, `release`, `confirm_receipt`, `resolve_exception`); screens Approval queue/decision, Transfer; tables `workflow_proposalversion`, `workflow_approval`, `workflow_transfer`, `exchange_reservation` | `workflow.ApprovalTests.*` (self-approval, stage order, stale version, reject/return), `workflow.TransferTests.*` (release/receipt only by assigned custodians, custody/department/location move on matching receipt, split + lineage, idempotent receipt, mismatch/damage → Exception, rollback); `workflow.RequestAndHoldTests.test_last_item_cannot_be_held_twice`; **PostgreSQL** `exchange.PostgresLastUnitRaceTests` (6 threads, exactly 1 hold); browser E2E (5 users) | ✅ |
| **C6** States & history *(mandatory)* | P6 s3 "only the moves in the cycle; who did what and when"; P3 FR-09, FR-22, FR-23, FR-45, FR-55; D Fig 37–43 | `AssetStatus.TRANSITIONS`, `RequestStatus`, `ReservationStatus`, `TransferStatus`, `MaintenanceStatus` tables; append-only `LifeCycleEvent` / `AuditEvent`; screens Asset history timeline, Audit trail | `catalog.StateMachineTests.*`, `workflow.TransferTests.test_audit_trail_*`, append-only save/delete refusal | ✅ |
| **C7** Sustainability dashboard *(bonus)* | P6 s3 "≥3 measures, formula and source"; P3 FR-42, FR-43, SUS-01–09, BR-05, BR-06; D 5.11 | `reporting/metrics.py` (deterministic), `reporting/impacts.py`; screen Sustainability: 9 indicators, each with formula, inputs, source, period, scope and status | `reporting.MetricTests.*` (zero denominator → unavailable, reuse/diversion maths, repeat transfer counted once, confirmation needed for savings, negative and provisional savings, carbon unavailable without a cited factor) | ✅ |
| **C8** Notifications *(bonus)* | P6 s3 "made, approved, rejected, received"; P3 FR-19, BR-16 | `reporting/audit.notify` called in each workflow transaction; screen Notifications (read/unread, mark all, open link) | `reporting.NotificationTests.*` (right recipients, approver notified only when their stage opens, cannot open another user's notice), `ApprovalTests.test_reject_*`, `TransferTests.*_lineage` | ✅ |
| **C9** Basic report *(bonus)* | P6 s3 "table of requests and transfers, CSV export"; P3 FR-41 | `reporting.views.OperationalReportView`, `services.to_csv/csv_safe`; screen Operational reports | `reporting.CsvAndReportPermissionTests.*` (permission, same filters, formula injection neutralised, department scope) | ✅ |
| **AI 1** Classification & tagging *(bonus)* | P6 s4; P3 FR-25, FR-26, AI-01–06 | `intelligence/classification.py`; Register-asset AI panel (Accept & edit / Reject); `HumanReview` | `intelligence.ClassificationTests.*` (labelled fallback, no invented confidence, Gemini success, invalid category → fallback, accept/edit/reject recorded); browser | ✅ (mocked Gemini) · ⚙️ live |
| **AI 2** Matching & ranking *(mandatory)* | P6 s4 "top 5, score + one-line reason, accept/reject"; P3 FR-27–31, AI-03, AI-08, BR-12; D 5.10 weights .30/.20/.15/.10/.10/.10/.05 | `intelligence/matching.py`, `similarity.py`; screen AI matches (score ring, components, reasons, coverage, Select/Reject/Reject all, excluded list); `MatchRecommendation`, `HumanReview`, `AssetRepresentation` | `intelligence.MatchingTests.*` (design worked example = 85.3, weights sum 1.00, hard filters first, top-5, missing factors excluded, synonyms, Gemini embeddings + cache, no reservation from viewing); browser | ✅ (mocked Gemini) · ⚙️ live |
| **AI 3** Sustainable-action advice *(bonus)* | P6 s4; P3 FR-32–35 | `intelligence/advice.py`; screen Advice (choose / override with reason / defer → normal forms) | `intelligence.AdviceTests.*` (disposal always last, figures not in evidence flagged) | ✅ (mocked) · ⚙️ live |
| **AI 4** Assistant *(bonus)* | P6 s4 "only app data; 'I don't know'; never acts without confirm"; P3 FR-36–40, AI-01, AI-05 | `intelligence/assistant.py`; screen Assistant (citations, draft → prefilled request form) | `intelligence.AssistantTests.*` (privileged request refused before any AI call, context excludes hidden records, unauthorised citation discarded, injection treated as data, "I don't know") ; browser | ✅ (mocked) · ⚙️ live |
| **AI 5** Generated report *(mandatory)* | P6 s4 "DB calculates numbers; AI writes summary; admin edits before publishing"; P3 FR-44; D 4.5 "Reporting/Sustainability Officer reviews" | `reporting/services.py` (snapshot → generate → edit → review → publish); screens AI reports, Report detail | `reporting.NarrativeTests.*` (draft not published, invented numbers block review, edit resets review, published immutable, template fallback labelled, minimal snapshot sent); browser (template path) | ✅ (mocked) · ⚙️ live |

## 2. Important design rules (P3 / D)

| Rule | Where enforced | Test | Status |
|---|---|---|---|
| 48 h pre-approval hold (BR-08) | `exchange.services._create_hold`, `expire_due_holds` | `test_pending_hold_expires_but_need_stays_open` | ✅ |
| No expiry after allocation (BR-08) | approval sets `expires_at=None`; expiry queries PENDING only | `test_allocated_stock_never_auto_expires` | ✅ |
| Version-bound approval (FR-51, BR-11) | `workflow.decide` (current version), `_check_authorisation_current` at release | `test_return_then_resubmit_…`, `test_pending_hold_expires…` (stale) | ✅ |
| No self-approval (BR-02) | `decide` (proposer check), disposition/repair too | `test_self_approval_rejected…`, `test_proposer_cannot_authorise_own_disposition` | ✅ |
| Separate release & matching receipt (BR-13) | `release`, `confirm_receipt` | `TransferTests.*` | ✅ |
| Receipt discrepancies → Exception (FR-52) | `confirm_receipt`, `resolve_exception` | `test_quantity_mismatch…`, `test_damage…` | ✅ |
| Atomic stock + workflow + audit (NFR-03) | `transaction.atomic` in every command; notices in savepoints | `test_failure_during_completion_rolls_everything_back` | ✅ |
| Last unit cannot go twice (FR-49) | row lock `select_for_update` + SQLite IMMEDIATE + version bump | SQLite sequential test ✅; PostgreSQL 6-thread race ✅ | ✅ |
| Protected attachments (FR-06) | storage outside static; `AttachmentDownloadView` checks asset permission | `test_download_follows_asset_permission` | ✅ |
| Unsafe stock restricted (FR-24, NFR-05) | `report_defect` restricts, unpublishes, flags holds; release blocked | `test_defect_restricts_stock…` | ✅ |
| Verified return from maintenance | `verify` by custodian ≠ technician | same test, `test_repair_requires_approval…` | ✅ |
| Disposition needs approval + evidence (BR-14, NFR-06) | `complete_disposition` | `DispositionTests.*` | ✅ |
| Conservative sustainability (SUS-08) | `metrics.indicator(... status="unavailable")` | `test_zero_denominator…` | ✅ |
| De-duplication via lineage (SUS-09) | `Asset.reuse_counted`, `SustainabilityImpact.counted`, `AssetLineage` | `test_reuse_and_diversion_formulas_and_repeat_transfer_counted_once` | ✅ |
| AI is advice, not authority (BR-04, FR-35, FR-40) | AIGateway has no write commands; assistant guard; advice routes to forms | `AssistantTests.test_privileged…`, `MatchingTests` (no reservation) | ✅ |

## 3. Phase 3 functional requirements coverage (FR-01 … FR-55)

| FR | Summary | Status | Note |
|---|---|---|---|
| 01–04, 46 | Auth, scoped permissions, admin accounts/departments, denied attempts logged | ✅ | |
| 05–09, 47 | Asset metadata, attachments, publish/unpublish, search filters, before/after history, validation | ✅ | Photos are stored and access-controlled, but AI classification reads **text only** (see limitations). |
| 10–14, 48–50 | Requests, holds, tracking, validation, ineligible stock, edit/withdraw/cancel, atomic allocation, release on reject/expiry | ✅ | |
| 15–19, 51–52 | Staged approval, decisions with reasons, transfer records, custody on receipt, notifications, version binding, exceptions | ✅ | |
| 20–24, 53–54 | Defect, inspection, repair/refurbish, life-cycle events, corrections, restriction, disposition proposal and completion | ✅ | |
| 25–31 | AI classification, semantic matching, factors, 0–100 score, explanations, fallback | ✅ / ⚙️ | Live Gemini not exercised here. |
| 32–35 | Action advice, factors, override, not authorisation | ✅ / ⚙️ | |
| 36–40 | Assistant search/status/policy/navigation, draft + confirm, permissions, uncertainty, no actions | ✅ / ⚙️ | |
| 41 | Operational reports filtered by period and department | ✅ | Requests and transfers tables plus summary counts; maintenance, approvals and custody appear as counts, not separate tables. |
| 42–43 | Indicators with period, scope, units, inputs, missing data | ✅ | |
| 44 | Reviewable AI summaries | ✅ / ⚙️ | Six narrative types share one snapshot schema. |
| 45, 55 | Append-only audit, history and audit search | ✅ | |

## 4. Non-functional requirements — honest status

| NFR | Status | Evidence / gap |
|---|---|---|
| 01–02 performance (95% ≤3 s at 1,000 assets) | 🟡 | Not load-tested. The demo data (~20 batches) responds instantly; no claim at the 1,000-asset baseline. |
| 03 atomic + idempotent | ✅ | Rollback and idempotency tests |
| 04 AI progress ≤1 s, result/fallback ≤15 s | ✅ partly | Button loading states; provider timeout 10 s (configurable); fallback always returned. Calls are synchronous (no background queue). |
| 05–07 safety, disposition evidence, corrections | ✅ | Maintenance, disposition and catalog tests |
| 08–12 security, field-level finance, validation, minimal AI data | ✅ | Direct-access tests, financial hiding test, minimal snapshot test, CSP headers |
| 11 encryption in transit | ⚙️ | Local demo is HTTP on 127.0.0.1; set `CAMPUS_HTTPS=1` behind TLS for deployment. |
| 13 labels and messages | ✅ | Field-level errors across forms (browser-inspected) |
| 14 non-negative stock / consistency | ✅ | DB CHECK constraints plus race test |
| 15 testable without AI | ✅ | All 97 tests run with no key |
| 16–17 AI reasons and labels; accept/edit/reject | ✅ | |
| 18 traceability | ✅ | This document |
| 19 hashed passwords, no secrets in logs | ✅ | Hash test; the key is never logged or stored |
| 20 backup / restore test | ❌ | Not implemented as a feature. For the SQLite demo, back up by copying `data/` while the server is stopped (see DATABASE_GUIDE). No restore test was executed. |
| 21 keyboard and focus, non-colour status | ✅ partly | Visible focus rings, labelled fields, text + icon badges. No formal screen-reader audit. |
| 22 Chrome / Edge / Firefox | 🟡 | Verified in Chromium (Playwright) only. |

## 5. Platform deliverables

| Item | Status |
|---|---|
| macOS launchers `setup.command` / `run.command` | 🟡 Same bash script verified on Linux, from a fresh copy in a path containing spaces; **not run on macOS** here. |
| Windows launchers `setup.bat` / `run.bat` / `.ps1` | 🟡 **Not run on Windows** here (no Windows machine). They delegate to the same verified `scripts/bootstrap.py`. |
| PostgreSQL path | ✅ Migrations, seed and full test suite passed on PostgreSQL 16 in this environment. ⚙️ You must install and configure PostgreSQL yourself. |
| Live Gemini | ⚙️ Requires your key; run `gemini_smoke`. |

## Input validation (every field, server-side)

All rules live in **`apps/core/validators.py`**. They are used by the forms (red message under the field), the services (the real gate, because a request can be forged) and the views (tampered IDs → 404, not 500). Tests: `apps/core/test_validation.py`.

| Input | Rule |
|---|---|
| Any text | Trimmed; no control characters; no HTML/script markup; length limits; descriptive text must contain words |
| Ports (IT) | Comma-separated allow-list: HDMI, DisplayPort, Mini DisplayPort, VGA, DVI, USB-A/B/C, Micro USB, Thunderbolt, Ethernet, Audio jack, SD/microSD, Power, … with optional counts (`2x USB-C`). `hello there` is rejected |
| Serial number (IT) | 3–60 letters/digits/`- / .`, at least one digit, no spaces; stored upper-case |
| Material / Dimensions (furniture) | Words only / `140 x 70 cm` or `120 x 60 x 75 cm` (mm, cm, m, in), each > 0 |
| Tags, mandatory specs | Comma-separated keywords, 1–30 characters each, de-duplicated, at most 20 / 10 |
| Campus, building, room | Letters, digits, spaces and `. , ' ( ) / # & -` |
| Quantities | Whole numbers; asset batch 1–10,000; request 1–1,000 |
| Money (AED) | 0 – 10,000,000, at most 2 decimals; NaN/infinity rejected; current value ≤ new-purchase price |
| Weight | > 0 and ≤ 5,000 kg |
| Dates | Purchase: 1970 – today. Ready for reuse: not before purchase, ≤ 2 years ahead. Expected retirement: after purchase, ≤ 30 years. Required date: today – 2 years ahead. Handover: not past, ≤ 1 year. Disposition completion: not future, not before the proposal. Report period: start ≤ end, ≤ 5 years |
| Choices (condition, urgency, route, result, status …) | Must be one of the listed values |
| Hidden IDs / versions | Must be valid UUIDs / integers, otherwise 404 or "form out of date" |
| Accounts | Campus ID `100061024` format; names: letters, spaces, `' - .`; valid email; password policy (≥ 12 characters); department code 2–12 letters |
| Uploads | JPEG/PNG/PDF by content, ≤ 10 MB, non-empty; file name sanitised |
| Assistant question | 1–500 characters, must contain words |

**Safety nets.**
- `BadInputMiddleware` turns any malformed value that slips through into a friendly 400/404 page.
- A pre-save guard (`apps/core/apps.py`) refuses to store control characters or markup in any user text column, whichever code path wrote it.

## Bonus features beyond the brief

QR labels and scan-to-confirm, availability alerts, a department leaderboard with badges, photo-based classification, and a full Arabic/English interface. Each is documented with its location, code, tests and limits in [BONUS_FEATURES.md](BONUS_FEATURES.md).
