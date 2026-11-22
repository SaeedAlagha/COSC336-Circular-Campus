# Submitted design → implementation mapping

This maps the Phase 4–5 design (v3.1) onto the code. Names are kept wherever possible. Where a technical name differs, the correspondence is stated explicitly.

## 1. Architecture (DR-01, DR-02, Figure 7, Figure 44)

The design chose a **modular monolith** (Django + PostgreSQL) with an AI gateway. The implementation is one Django project with **one app per Level-1 DFD process**:

| Level-1 process | Django app | Main classes / modules |
|---|---|---|
| 1.0 Accounts and access | `apps/accounts` | `User`, `Role`, `UserRole`, `Department`; `policy.py` (identity & policy component); `services.py` |
| 2.0 Assets and handover | `apps/catalog` | `Asset`, `Category`, `Attachment`; `services.py` (registration, publication, balances, transitions, batch split) |
| 3.0 Requests and holds | `apps/exchange` | `ResourceRequest`, `Reservation`; `services.py` (draft, submit, hold, expiry, withdraw, cancellation, fulfilment) |
| 4.0 Authorization and transfer | `apps/workflow` | `ProposalVersion`, `Approval`, `Transfer`; `services.py` (`decide`, effect handlers, `release`, `confirm_receipt`, `resolve_exception`) |
| 5.0 Service and disposition | `apps/maintenance` | `MaintenanceRecord`, `Inspection`, `Disposition`, `LifeCycleEvent`, `AssetLineage`; `services.py` |
| 6.0 AI advice | `apps/intelligence` | `ModelVersion`, `AITask`, `AIResult`, `AssetRepresentation`, `MatchRecommendation`, `HumanReview`, `Evaluation*`; `gateway.py` (AIGateway), `gemini.py`, `classification.py`, `matching.py`, `advice.py`, `assistant.py` |
| 7.0 Reports, audit and notices | `apps/reporting` | `SustainabilityImpact`, `Report`, `ReportMetric`, `AvoidedPurchaseReview`, `EmissionFactor`, `Notification`, `AuditEvent`; `metrics.py`, `impacts.py`, `audit.py`, `services.py` |
| (shared) | `apps/core` | `BusinessError` codes, idempotent `execute`, reference counters, `AppView` base, dashboard, template tags |

**Layering.**
- Presentation: `templates/`, `forms.py` and thin `views.py`.
- Application: `services.py` per app.
- Policy: `accounts/policy.py`.
- Domain / persistence: `models.py`.
- AI adapters: `intelligence/gemini.py` and `gateway.py`.
- Deterministic reporting: `reporting/metrics.py`.

Views contain no business rules; every write goes through a service command that re-checks policy on the server.

## 2. DFD stores → tables (the logical stores are groupings, not one table each)

| Logical store | Physical tables |
|---|---|
| D1 Identities and departments | `accounts_user`, `accounts_role`, `accounts_userrole`, `accounts_department` |
| D2 Assets and attachments | `catalog_asset`, `catalog_category`, `catalog_attachment` (+ files in `data/protected_media/`) |
| D3 Requests and reservations | `exchange_resourcerequest`, `exchange_reservation` |
| D4 Proposals, approvals and transfers | `workflow_proposalversion`, `workflow_approval`, `workflow_transfer` |
| D5 Service and life-cycle history | `maintenance_maintenancerecord`, `maintenance_inspection`, `maintenance_disposition`, `maintenance_lifecycleevent`, `maintenance_assetlineage` |
| D6 AI results and model versions | `intelligence_modelversion`, `intelligence_aitask`, `intelligence_airesult`, `intelligence_assetrepresentation`, `intelligence_matchrecommendation`, `intelligence_humanreview`, `intelligence_evaluationrun`, `intelligence_evaluationobservation` |
| D7 Reports and impacts | `reporting_report`, `reporting_reportmetric`, `reporting_sustainabilityimpact`, `reporting_avoidedpurchasereview`, `reporting_emissionfactor` |
| D8 Audit and notices | `reporting_auditevent`, `reporting_notification` |
| (infrastructure, not in DFD) | `core_idempotencyrecord` (repeat-submission protection, design 5.9.2), `core_referencecounter` (R-0001 style references), Django `django_session`, `django_migrations`, `django_content_type` |

## 3. Context flows F01–F22 → where they enter or leave the system

| Flow | Implemented as |
|---|---|
| F01 / F02 Access/request data & outcomes | Sign-in form (`accounts.SignInView`), catalog search, request form, assistant, request detail, notifications |
| F03 / F04 Departmental needs & demand outcomes | Request form and list (Department Representative sees department requests) |
| F05 / F06 Asset/handover data & outcomes | Register/edit asset, publish, attachments, release/receipt forms on Transfer page; AI classification panel |
| F07 / F08 Approval decisions & pending proposals | Approval queue (`workflow.QueueView`) and decision page (`workflow.ApprovalView`) |
| F09 / F10 Service evidence & assignments | Maintenance record page (start inspection, findings, work, verification) |
| F11 / F12 Procurement review & reuse alternatives | Avoided-purchase review form on a completed Transfer; Operational reports |
| F13 / F14 Financial review & evidence | Cost-review approval stage; avoided-purchase review; financial fields shown only to permitted roles |
| F15 / F16 Impact review & reports | Sustainability dashboard, emission-factor form, AI report review/publish |
| F17 / F18 Account/config data & admin outcomes | Accounts, Departments, role grants; audit trail (technical events) |
| F19 / F20 Authorized AI data & task results | `GeminiClient.generate_json` / `embed` called only from `AIGateway.run` with minimal context |
| F21 / F22 Oversight query & evidence | Audit trail filters (Auditor), published reports, operational reports |

## 4. Level-2 process 6.0 (Figure 6) → code

| Sub-process | Code |
|---|---|
| 6.1 Authorize and minimize inputs | Callers build minimal context (`assistant.retrieve` under the user's permissions; `advice.evidence`; report `minimal` snapshot); `policy.require` before AI calls |
| 6.2 Check eligibility and prepare context | `matching.hard_filter` before scoring; cached vectors in `AssetRepresentation` |
| 6.3 Invoke bounded AI adapter | `AIGateway.run` → `GeminiClient` (timeout, error codes) |
| 6.4 Validate result and explain limits | Per-feature validators (category allow-list, action enum, citation allow-list, narrative number check); `AIResult.limitations` |
| 6.5 Return advice and record human review | `AIResult` + `HumanReview` (classification accept/edit/reject, match select/reject, advice accept/override/defer) |

## 5. Class design (Figures 16–19) → implementation

Every operation in the class diagrams exists as a **method on the matching class** (models.py), for example `transfer.confirm_receipt(...)`, `approval.decide(...)` and `request.submit(...)`. Each method delegates to the tested application service, which holds the transaction, locking and audit logic. `apps/core/tests.py` drives a complete exchange through these methods.

| Design operation | Class method → service |
|---|---|
| `ResourceRequest.submit()` / `cancel()` | `ResourceRequest.submit()` / `.cancel()` → `exchange.services` |
| `Asset.freeQuantity()` / `restrict(reason)` | `Asset.free_quantity()` / `.restrict()` |
| `Reservation.allocate()` / `releaseOnce()` | `Reservation.allocate()` (refuses; allocation only via approval) / `.release_once()` |
| `ProposalVersion.matchesCurrent()` | `ProposalVersion.matches_current()` |
| `Approval.decide(actor)` | `Approval.decide()` |
| `Transfer.recordRelease()` / `confirmReceipt()` | `Transfer.record_release()` / `.confirm_receipt()` |
| `MaintenanceRecord.recordInspection()` / `verifySafeReturn()` | `.record_inspection()` / `.verify_safe_return()` |
| `Disposition.propose()` / `recordEvidence()` | `Disposition.propose()` (classmethod) / `.record_evidence()` |
| `AIGateway.invoke(task)` / `validate(output)` | `AIGateway.invoke()` / `.validate()` — no approve/release/dispose method |
| `AIResult.markUnavailable()` | `AIResult.mark_unavailable()` |
| `HumanReview.record()` | `HumanReview.record()` (classmethod, checks same AI result) |
| `MatchRecommendation.explain()` | `MatchRecommendation.explain()` |
| `User.may(action, record)` | `User.may()` → `accounts.policy` |

Detailed service mapping:

| Design class / operation | Implementation |
|---|---|
| `ResourceRequest.submit()` / `cancel()` | `exchange.services.submit`, `withdraw`, `request_cancellation` |
| `Asset.freeQuantity()`, `restrict(reason)` | `catalog.services.balance` / `free_listed_quantity`; `maintenance.services.report_defect` |
| `Reservation.allocate()`, `releaseOnce()` | `RequestEffects.approved`; `exchange.services.release_hold` (refuses a second release) |
| `ProposalVersion.matchesCurrent()` | `ProposalVersion.matches()` and `workflow.services._check_authorisation_current` |
| `Approval.decide(actor)` | `workflow.services.decide` |
| `Transfer.recordRelease()`, `confirmReceipt()` | `workflow.services.release`, `confirm_receipt` |
| `AIGateway.invoke(task)`, `validate(output)` | `AIGateway.run(ai_call=…, fallback=…)`; validators inside each feature. No approve/release/dispose method exists. |
| `AIResult.markUnavailable()` | `AIResult.fallback=True` + `fallback_reason` |
| `HumanReview.record()` | `HumanReview` rows (`classification.record_review`, match/advice views) |
| `MaintenanceRecord.recordInspection()`, `verifySafeReturn()` | `maintenance.services.record_inspection`, `verify` |
| `Disposition.propose()`, `recordEvidence()` | `propose_disposition`, `complete_disposition` |
| `MatchRecommendation.explain()` | `matching.one_line` + stored `reasons` / `limitations` |
| `User.may(action, record)` | `accounts.policy.*` functions |
| `ITAsset` / `FurnitureAsset` specialisations | `Category.kind` selects validated `Asset.type_metadata` (serial number, ports / material, dimensions). The design's own note says these use "validated type-specific metadata on Asset". |
| `AssetCollection` (transient) | Catalog query results (`visible_assets`) |
| `RequestSpecification` (composition) | Specification fields stored on the request and frozen in `ProposalVersion.snapshot` |

## 6. ERD (Figures 20–30) → tables — deliberate differences

- **UUID primary keys** are used for every business entity. Human-readable references (R-0001, H-0001, T-0001, M-0001, D-0001, REP-0001, A-101) are separate unique columns.
- **Attachment** has a unique `(asset_id, attachment_no)` constraint plus a surrogate UUID PK, because Django has no composite primary keys. The partial key from the design is preserved.
- **AssetLineage, ReportMetric and UserRole** each have a surrogate id plus unique constraints on the design's composite keys.
- **Exactly one ProposalVersion target** is enforced by a database `CHECK` constraint, with per-target unique `(target, version_no)`.
- **SustainabilityImpact** adds `counted`, `department` and `source_department` for de-duplication and department-scoped rates (SUS-09).
- **Asset** adds `is_published`, `reuse_counted`, `cohort_entered_at`, `version` (optimistic concurrency), `replacement_value_aed`, `estimated_transfer_cost_aed`, `unit_weight_kg`, `available_from`, `expected_retirement_date` and `revised_usable_until`. These are the inputs that design 5.10/5.11 formulas require.

## 7. State machines (Figures 37–43) → code

| Diagram | Code | Notes |
|---|---|---|
| Asset (Fig 37 + 43) | `AssetStatus.TRANSITIONS`, `catalog.services.transition` | Available/Reserved/Transfer Pending are derived from quantity holds by `sync_listing_status` (the design says "holds affect quantity, not the entire batch"). |
| Request (Fig 38) | `RequestStatus.TRANSITIONS`, `exchange.services.set_request_status` | |
| Approval (Fig 39) | `Approval.decision` + `ProposalVersion.status` (Pending / Approved / Rejected / Returned / **Superseded**) | Superseded is shown via `effective_status`. |
| Transfer (Fig 40) | `TransferStatus.TRANSITIONS` | "Proposed / Pending Approval" lives on the ProposalVersion; a Transfer row is created at approval. |
| Maintenance (Fig 41) | `MaintenanceStatus.TRANSITIONS` | |
| Reservation (Fig 42) | `ReservationStatus.TRANSITIONS` | Partial receipt keeps it Allocated with a lower `remaining_quantity`. |

## 8. Approval stages (design 4.5.1) → `workflow_approval` rows

| Stage | Who | When applicable |
|---|---|---|
| 1 Submit & validate | requester (`submit`) | always |
| 2 RELEASE_CHECK | any custodian of the source department (not the proposer) | requests that hold stock; otherwise recorded **NOT_APPLICABLE** |
| 3 COST_REVIEW | any finance officer in scope | transfer cost > 0, repair cost > 0 or unknown, disposition cost > 0; otherwise NOT_APPLICABLE |
| 4 AUTHORIZE | the assigned approver (department default approver, never the proposer) | always |
| 5 Release / work | releasing custodian / maintenance staff | `release`, `record_work` |
| 6 Receipt / outcome | receiving custodian / verifying custodian / completion evidence | `confirm_receipt`, `verify`, `complete_disposition` |
| 7 Commit & notify | system, inside the same transaction | |

## 9. Justified deviations and simplifications (not hidden in the reports)

1. **No background worker.** Design pattern C ("queue and notify") for action advice and report narratives is implemented as **synchronous calls with a bounded timeout** (default 10 s) and an immediate labelled fallback. The `AITask` record still captures state, latency and error. Reason: avoids extra infrastructure for a local demo; NFR-04's 15-second ceiling still holds.
2. **Asset state additions not drawn in Figure 37:**
   - Reserved/Transfer Pending → Under Inspection, so a defect on held stock restricts it (FR-24).
   - Available/Reserved → In Use, for unpublishing (FR-07).
   - In Use → Disposition Pending, so a working item can be proposed for donation without first being declared unavailable.
   - Draft → Under Inspection.
   - Awaiting Approval → Under Inspection when a repair is declined (Figure 41 shows no exit).

   All are listed in `AssetStatus.TRANSITIONS`.
3. **One stock batch per submission.** A request version holds one asset batch; more stock for the same need uses an additional, separately approved version (BR-15 "separate approved transfers").
4. **Defect restricts the whole batch** (a batch has one condition by definition, BR-03), not only some units.
5. **Classification from text, and optionally from one photo.** Text classification is AI feature 1. Photo classification is a bonus added later: the photo is sent to Gemini for that one call only and is not stored by the app (data minimisation). Stored attachments are never sent to Gemini.
6. **Confidence.** Gemini returns no calibrated probability, so every AI result shows "confidence unavailable" (AI-03). The 0.80 confidence threshold is therefore never triggered. The 70/100 weak-match and 70% coverage thresholds are implemented.
7. **Exception resolution** offers two authorised outcomes:
   - Accept the confirmed evidence; any shortfall goes on a follow-up transfer under the same approval.
   - Return the stock to the source.
8. **Life-extension months** count whole calendar months (month-end aware).
9. **Database (DR-02).** PostgreSQL is supported and was tested, including the race test for the last unit. The Windows/macOS launchers default to SQLite so the project runs with no database server; `setup --postgres` switches to PostgreSQL.
10. **Notifications are written in the same transaction** as the business change, in a savepoint. A failed notice never reverses the change (BR-16). There is no separate outbox table, because there is no background worker (see 1).
11. **Small shortcuts not drawn in the state figures:**
    - A Draft request can be withdrawn (Draft → Cancelled).
    - A decision taken while a request is still Submitted moves it directly to Returned, Approved or Rejected without displaying Under Review first.
    - Handover scheduling is optional: Approved → Released is allowed when the custodian records the release without first arranging a date.
    - "Return stock to source" ends an Exception as Cancelled (Figure 40 shows only the accept outcome).
12. **Matching cost and sustainability fit (design 5.10).**
    - K uses the estimated transfer cost against the replacement value. Repair and refurbishment costs for a candidate are not known at matching time, so they are excluded rather than guessed.
    - E treats a published surplus unit's diversion fraction as 1 (it diverts that unit if reused), averaged with the carbon fit. E is unavailable without an approved emission factor.

## 10. Where each UML relationship is in the code

Every relationship from the class diagrams (Figures 16–19) and the ER diagrams (Figures 20–30) has a comment directly above it in the code, starting with **`# [UML:`**. In VS Code, press **Ctrl+Shift+F** and search for `[UML:` to list them all.

| Relationship (report) | File (search for the bracketed tag) |
|---|---|
| **Inheritance** — `ITAsset` and `FurnitureAsset` specialise `Asset` (Fig 19) | `apps/catalog/models.py` → `class ITAsset(Asset)`, `class FurnitureAsset(Asset)` `[UML: Inheritance]` |
| **Aggregation** — `AssetCollection ◇—— Asset` (Fig 19) | `apps/catalog/models.py` → `class AssetCollection`; used in `apps/catalog/views.py` → `visible_assets()` `[UML: Aggregation]` |
| **Composition** — `ResourceRequest ◆—— RequestSpecification` (Fig 19) | `apps/exchange/models.py` → `class RequestSpecification`, `ResourceRequest.specification` `[UML: Composition]` |
| Aggregation — Department ◇ User / Asset, Category ◇ Asset (Figs 20, 21) | `apps/accounts/models.py`, `apps/catalog/models.py` |
| Composition — Asset ◆ Attachment, Asset ◆ LifeCycleEvent, MaintenanceRecord ◆ Inspection, Report ◆ ReportMetric, EvaluationRun ◆ Observation, Asset ◆ AssetRepresentation | the matching `models.py` files |
| Associations with multiplicities, e.g. Reservation "belongs-to" ResourceRequest, Approval "decides-on" ProposalVersion, Transfer "authorized-by" Approval (Fig 16) | `apps/exchange/models.py`, `apps/workflow/models.py` |
| Dependency — AIGateway "creates" AIResult; Disposition "uses" advice (Fig 17) | `apps/intelligence/gateway.py`, `apps/maintenance/models.py` |
| Inheritance + polymorphism — `Effects` → `RequestEffects` / `CancellationEffects` / `RepairEffects` / `DispositionEffects` | `apps/workflow/services.py` |
| Inheritance — `AppView` (all screens), `User`, `AccessDenied`, `BusinessError` | `apps/core/views.py`, `apps/accounts/models.py`, `apps/accounts/policy.py`, `apps/core/errors.py` |

**Performance:** `ITAsset` and `FurnitureAsset` are proxy classes (no extra table, no extra query). `AssetCollection` and `RequestSpecification` are plain in-memory objects. The bracketed tags are comments and do not affect how the app runs.
