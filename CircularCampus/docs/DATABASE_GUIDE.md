# Database guide (inspection and oral demonstration)

The local demo database is **SQLite**: one file, `data/circular_campus.sqlite3`. PostgreSQL uses the same schema (same migrations).

## 1. Open it safely

**Read-only is safest.** Back up first: stop the server, then copy the whole `data/` folder. To restore, put the copy back.

**Windows or macOS — DB Browser for SQLite (graphical, free).** Install from <https://sqlitebrowser.org>.
1. *Open Database Read Only…* → choose `data/circular_campus.sqlite3`.
2. *Browse Data* shows table contents; *Database Structure* shows the keys.

**Any OS — Python's built-in sqlite3, read-only (no install).**
- Windows (PowerShell):
  ```powershell
  .venv\Scripts\python -c "import sqlite3;c=sqlite3.connect('file:data/circular_campus.sqlite3?mode=ro',uri=True);[print(r) for r in c.execute('select asset_tag,status,quantity from catalog_asset')]"
  ```
- macOS (Terminal):
  ```bash
  .venv/bin/python -c "import sqlite3;c=sqlite3.connect('file:data/circular_campus.sqlite3?mode=ro',uri=True);[print(r) for r in c.execute('select asset_tag,status,quantity from catalog_asset')]"
  ```

**Django's shell**, for joins with model names:
```bash
.venv/bin/python manage.py dbshell
```
This opens the `sqlite3` CLI if it is installed. On Windows, `sqlite3.exe` usually is not.

**PostgreSQL:** connect with `psql -h 127.0.0.1 -U campus campus`, then `\dt` lists the tables.

Never edit rows by hand during the demo. Workflow, audit and stock rules live in the application services; manual edits bypass them, which is exactly what the design forbids.

## 2. Tables, primary keys and relationships

All business tables use a **UUID primary key `id`**. Human-readable references (R-0001, T-0001, A-101 …) are separate **unique** columns. `→` means a foreign key.

| Table | Key columns (FK →) | Purpose |
|---|---|---|
| `accounts_department` | code (unique), default_approver_id → accounts_user | Departments; deactivated, never deleted |
| `accounts_user` | campus_id (unique), department_id → accounts_department, password (salted hash) | Accounts |
| `accounts_role` | code (unique) | The 10 roles |
| `accounts_userrole` | user_id → user, role_id → role, scope_department_id → department (NULL = explicit campus-wide) | Role grants (many-to-many with scope) |
| `catalog_category` | parent_id → catalog_category | Category tree (subcategories) |
| `catalog_asset` | asset_tag (unique), category_id, owner_department_id, custodian_id → user, parent_batch_id → catalog_asset | Unit or homogeneous batch; status; NULL = unknown value |
| `catalog_attachment` | asset_id → asset, unique (asset_id, attachment_no) | Photo/document metadata; file is in protected storage |
| `exchange_resourcerequest` | reference (unique), requester_id, department_id, category_id, receiving_custodian_id, assigned_approver_id | Departmental need |
| `exchange_reservation` | reference, request_id → request, asset_id → asset | Hold (Pending Approval, expires) / allocation (never expires) |
| `workflow_proposalversion` | exactly one of request_id / maintenance_id / disposition_id (CHECK) | Immutable snapshot + hash of what is being approved |
| `workflow_approval` | version_id → proposalversion, reviewer_id, scope_department_id; unique (version, stage) | One stage decision |
| `workflow_transfer` | reservation_id, approval_id, source_asset_id, received_asset_id → asset, releasing_user_id, receiving_user_id | Release / receipt / exception |
| `maintenance_maintenancerecord` | asset_id, reporter_id, assigned_staff_id, verified_by_id | Service record |
| `maintenance_inspection` | maintenance_id, inspector_id | Findings |
| `maintenance_disposition` | asset_id, proposer_id, completed_by_id, ai_result_id | Donation / recycling / retirement / disposal |
| `maintenance_lifecycleevent` | asset_id, actor_id, transfer_id, maintenance_id, disposition_id, correction_of_id | Append-only asset history with before/after JSON |
| `maintenance_assetlineage` | parent_asset_id, child_asset_id, event_id; unique pair; CHECK parent ≠ child | Batch split / recovered component provenance |
| `intelligence_modelversion` · `_aitask` · `_airesult` | task_id (1:1), model_version_id | AI provenance: model, config, latency, fallback |
| `intelligence_matchrecommendation` | ai_result_id, request_id, asset_id | Ranked candidate with score components |
| `intelligence_humanreview` | ai_result_id, actor_id, selected_match_id | Accept / edit / reject / select / override |
| `intelligence_assetrepresentation` | asset_id, model_version_id | Cached embedding vectors |
| `reporting_sustainabilityimpact` | asset_id, event_id (unique with indicator), department_id, source_department_id | Evidence-linked metric contribution (`counted` flag) |
| `reporting_avoidedpurchasereview` | transfer_id (unique) | Procurement/Finance confirmation of an avoided purchase |
| `reporting_emissionfactor` | category_id, entered_by_id | Cited carbon factors (none seeded) |
| `reporting_report` · `reporting_reportmetric` | report → impacts | Frozen snapshot + narrative (draft / reviewed / published) |
| `reporting_notification` | recipient_id | In-app notices, read_at |
| `reporting_auditevent` | actor_id (or service_actor) | Append-only audit |
| `core_idempotencyrecord` | unique (actor_id, key) | Repeat-submission protection |

## 3. Before/after demonstration (request → approval → release → receipt)

The seeded request **R-0004** (two laptops for Computer Science, from Engineering) is waiting at the release check. Run this before each step and after it:

```bash
.venv/bin/python manage.py trace_request R-0004
```
On Windows use `.venv\Scripts\python manage.py trace_request R-0004`.

| Step (UI, user) | What changes in the database |
|---|---|
| Start | `exchange_reservation` H-…: `PENDING_APPROVAL`, `expires` = +48 h. `workflow_approval` RELEASE_CHECK decision NULL. `catalog_asset` IT-220 dept ENG, custodian 100062001. |
| Release check approve (100062001) | RELEASE_CHECK → APPROVED. COST_REVIEW is already NOT_APPLICABLE (zero transfer cost). Request → UNDER_REVIEW. |
| Authorise (100063002, CS approver) | AUTHORIZE → APPROVED. ProposalVersion → APPROVED. Reservation → `ALLOCATED`, expires NULL. New `workflow_transfer` T-… APPROVED. **Asset still ENG / 100062001.** |
| Release (100062001 on the transfer page) | Transfer → RELEASED, released_at set. **Asset still ENG.** |
| Confirm receipt (100062002) | Transfer → COMPLETED. New `catalog_asset` IT-220-1 (2 units, dept CS, custodian 100062002, room B3/210, IN_USE); source IT-220 quantity 5 → 3. `maintenance_assetlineage` row SPLIT. Reservation CONSUMED. Request FULFILLED. New `reporting_sustainabilityimpact` REUSE + DIVERSION rows. `reporting_auditevent` "Transfer completed". |

Show the same evidence in the UI:
- Asset IT-220 → *Life-cycle history* and *Stock lineage*.
- Auditor 100069002 → *Audit trail*, filtered by record `R-0004` or `T-`.
