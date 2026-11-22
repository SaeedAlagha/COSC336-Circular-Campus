# Test and verification report (what was actually run)

**Environment:** Linux container · Python 3.13 · Django 5.2.19 · SQLite 3 · PostgreSQL 16 (local cluster) · Chromium via Playwright. Date: 21 Nov 2026.

## 1. Automated tests

| Command | Result |
|---|---|
| `python manage.py test` (SQLite) | **Ran 122 tests — OK (skipped=1)**. The skipped test is the PostgreSQL-only race test. |
| `POSTGRES_DB=campus … python manage.py test` (PostgreSQL 16) | **Ran 122 tests — OK** (includes the race test) |
| `python manage.py makemigrations --check` | No changes detected |
| `python manage.py check` | No issues |

**Coverage by suite.**

| Suite | Tests | What it covers |
|---|---|---|
| `apps/accounts/tests.py` | 12 | Login/logout, generic failure, inactive account, deactivated session, account validation, hashed password, admin cannot self-grant approval, direct GET/POST denial, cross-department edit denial, hidden financials, department deactivation keeps records |
| `apps/catalog/tests.py` | 15 | Draft, unknown values stay NULL, duplicate ID, negative value, HTTP form errors, custody immutable via edit, corrections need a reason, stale edit, location change blocked while committed, attachment type/content/size, protected download, unpublish preserves holds, poor stock not publishable, filters, unknown-age exclusion, inverted range, state-transition table, Reserved/Transfer Pending sync, append-only events |
| `apps/workflow/tests.py` | 26 | 48 h hold, stage routing, cost-review rule, past date, partial choice, last unit (sequential), hold expiry keeps need open, stale approval after expiry, allocation never expires, idempotent resubmission, payload conflict, withdraw releases once, self-approval, stage order, sysadmin cannot approve, reject with reason, return/resubmit stale, approval without custody change, release/receipt only by assigned custodians, matching receipt moves custody, split + lineage, whole-batch move, idempotent receipt, quantity mismatch → Exception → resolution with follow-up, damage → return to source, **rollback on failure**, cancellation after approval, released cannot cancel, audit actor/values + append-only |
| `apps/maintenance/tests.py` | 7 | Defect restricts + flags holds + blocks release; technician cannot verify; repair needs approval and finance review; unknown cost stays unknown; life extension; unrepairable → Unavailable; disposition needs approval and evidence; quantity must match; self-authorisation refused; partial split + component recovery; rejection → Unavailable |
| `apps/reporting/tests.py` | 12 | Unavailable not zero; reuse/diversion/kg formulas; repeat transfer counted once; confirmation required for savings; negative and provisional savings; carbon only with a cited factor (synthetic labelled); notification recipients and stage timing; foreign notice link refused; CSV permission, same filters, formula injection; department scope; report creation permission; AI draft not published; invented numbers block review; edit resets review; published immutable; template fallback labelled; minimal snapshot sent |
| `apps/intelligence/tests.py` | 19 | Gemini REST shape (key in header, JSON schema config); error mapping (429, 400 key, 404, 503, timeout, network); malformed/blocked output; embedding validation; not configured without key; classification fallback label, Gemini success, invalid category fallback, accept/edit/reject; design worked example 85.3 and weights = 1.00; hard filters before ranking; top 5; no reservation from viewing; missing factors excluded; synonym similarity; Gemini embeddings + cache; advice disposal last + unsupported figures flagged; assistant privileged refusal without an AI call, permission-filtered context, unauthorised citation discarded, injection treated as data, "I don't know" |
| `apps/core/tests.py` | 5 | Figure 19 inheritance (ITAsset/FurnitureAsset), aggregation (AssetCollection), composition (RequestSpecification);  Class-diagram operations used directly: `ResourceRequest.submit`, `Approval.decide`, `Transfer.record_release` / `confirm_receipt`, `Asset.free_quantity` / `restrict`, `User.may`, `Reservation.allocate` refusal, `MaintenanceRecord.record_inspection` / `verify_safe_return`, `AIGateway.validate`, and no `AIGateway.approve` |
| `apps/core/test_validation.py` | 12 | Input validation (NFR-10): ports allow-list (`hello there` rejected), serial/dimension/material formats, whole numbers and money (NaN, 1.5e400, 3 decimals, negatives, over 10 million), markup/control characters, keywords, places, names, UUID parsing; asset and request services/forms reject inconsistent values and out-of-range dates with field errors; stored-text guard; assistant question limits; garbage POSTs to 10 endpoints never return 5xx |
| `apps/core/test_bonus.py` | 13 | Bonus: leaderboard formula/badges and hidden AED for requesters; alert validation, one notification per item, no repeat on republish, own-department and below-condition stock ignored; QR label permissions, lookup, 404 on bad tag, wrong label refused at receipt and right label stored as verified; photo AI rejects non-images, says "not analysed" without Gemini, sends inline image to Gemini; Arabic pages are RTL and translated, language switch, catalogue complete, sign-in works in Arabic |
| `apps/exchange/tests.py` | 1 | **PostgreSQL:** 6 threads race for 1 unit → exactly 1 hold; the others get STOCK_CONFLICT |

**Important.**
- All AI tests use **mocked** HTTP responses. **No live Gemini request was made.**
- SQLite tests are not presented as evidence of PostgreSQL locking. The race test ran on PostgreSQL only.

## 2. Bugs found and fixed during verification

| Found by | Bug | Fix |
|---|---|---|
| PostgreSQL test run | `SELECT … FOR UPDATE` on nullable joins is rejected by PostgreSQL (14 errors; invisible on SQLite) | `select_for_update(of=("self",))` |
| Fresh-folder setup | Empty `CAMPUS_DEMO_PASSWORD=` in `.env` broke seeding | Fall back to the documented default when empty |
| Browser QA | Date inputs rendered in en-GB format were blank in `<input type=date>` | ISO `format="%Y-%m-%d"` on every DateInput |
| Browser QA | Main column missing on desktop (overlay element occupied a grid cell); button label colour clash; hamburger visible on desktop; search icon overlapping text; list padding | CSS fixes |
| Seed inspection | Defect left stock "published", blocking republish; month-end life-extension off by one | Unpublish on restriction; month-end aware arithmetic |
| Fuzzing | Unparsed UUIDs, versions, quantities and costs from hidden/tampered fields caused 500s (17 endpoints) | Central `apps/core/validators.py` used by forms, services and views; `BadInputMiddleware` safety net; stored-text guard |
| Review | Substring status tests in templates ("AVAILABLE" ⊂ "UNAVAILABLE") | Explicit flags computed in views |

## 2b. Input-validation fuzzing

- Script posted **11,655** garbage submissions (empty, text in number fields, `-5`, `1.5e400`, `NaN`, `<script>`, 6,000-char strings, SQL text, bad dates, random UUIDs, control characters, emoji) to every form action as every one of the 15 demo users, plus garbage query strings on list pages.
- **Before the validation pass:** 17 endpoints returned server errors (500).
- **After:** **0** server errors. Bad input now gets a red field message, a flash message, or a friendly 400/404 page, and nothing is changed.

## 2c. Bonus features — extra checks

- **Fuzzing including new pages:** 12,630 garbage submissions (alerts, scanner, language switch added). **0 server errors.**
- **Crawl:** every URL (including leaderboard, alerts, labels, scanner) × all 15 users, in **English and Arabic** — no errors; every Arabic page has `dir="rtl"`.
- **QR:** the printed labels were decoded by the bundled jsQR library back to the correct links (3/3). With Chromium's simulated camera showing a label, the scanner opened the right asset page.
- **Browser flow:** requester saved a "projector" alert → custodian registered and published a projector → requester got "Now available: Epson projector".
- **Mobile (iPhone 13), English and Arabic:** dashboard, catalog, alerts, leaderboard, scanner, request form — **0 px** horizontal overflow (three layout bugs found and fixed: crowded top bar, a list grid, an off-screen screen-reader label).
- **Not verified:**
  - Live Gemini photo classification (only a mocked response was tested).
  - A real phone camera.
  - The Arabic wording, which needs review by a native speaker.

## 3. Browser checks (Playwright, Chromium, real server)

- **Every URL × every one of the 15 demo users** (1,395 page loads after a fresh reseed with the validation rules): no 5xx; `200: 735`, `403: 660` (role denials).
- **Validation in the UI:** entering `hello there` as Ports and Serial number, and a current value above the new price, shows red messages under each field and saves nothing. Anonymous users are redirected to sign-in.
- **End-to-end workflow through the UI**, with confirmation dialogs, as 5 users:
  1. 100061002 submits R-0007 with A-101 × 3 → hold message.
  2. 100062001 release check.
  3. 100065002 cost review.
  4. 100063002 authorises.
  5. 100062001 records release.
  6. 100062002 confirms receipt → "Fulfilled".

  No console errors.
- **AI flows:** run ranking (labelled fallback), assistant search + refused approval, classification suggestion, report snapshot → narrative (template fallback).
- **Inspected screenshots** at 1440 px: sign-in, dashboards (requester, approver), catalog, asset detail and history, request detail, matches, approval, transfer, sustainability, report, classification, assistant.
- **Mobile (iPhone 13 emulation):** dashboard, catalog, navigation drawer, request, matches, assistant — **0 px horizontal overflow** measured.
- **`DEBUG=0` mode:** friendly 404 page; CSP, X-Frame-Options and Referrer-Policy headers present; static files served locally.

## 4. Setup / launcher checks

- `setup.command` (bash) on Linux, from a fresh copy in `/tmp/space test/Circular Campus` (a path with spaces):
  - created `.venv`, installed dependencies, created `.env`, migrated, seeded, checked;
  - re-running left `.env` unchanged and refused to re-seed ("Nothing was changed");
  - `run.command --port 8001` served the app;
  - a second run on the same port reported "Port 8001 is already in use … run with --port 8002".
- Migrations, seed and the full test suite on PostgreSQL 16.
- Commands: `trace_request R-0006`, `gemini_smoke` (no key → clear message, nothing sent), `expire_holds`, `evaluate_ai` (12/12 on the team-written synthetic sample, rule fallback — **not an accuracy claim**).

## 5. Not verified here — please run locally

| Item | How to verify |
|---|---|
| Windows `setup.bat` / `run.bat` / `.ps1` | Run them on Windows 10/11 with Python 3.13. |
| macOS double-click `.command` files | Run on macOS (the same bash script passed on Linux). |
| Live Gemini | Put the key in `.env`, then run `python manage.py gemini_smoke`, then try the AI screens. |
| Edge / Firefox / Safari | Open the main screens. |
| Performance at 1,000 assets; backup/restore drill (NFR-20) | Not executed. |
