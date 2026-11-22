# Demo walkthrough and rehearsal guide (≈12 minutes)

**Setup.** Run setup once; start with `run.bat` / `./run.command`. Use two browser windows (one normal, one private) to switch users quickly. Password for every account: `CircularCampus2026!`.

## A. Mandatory core (C1–C6) + AI 2 + AI 5

1. **C1 roles.** Sign in as **100061001** (requester). The sidebar shows only requester functions.
   - Paste the URL `/catalog/new/` → **403 Access denied** (server check). The attempt is recorded.
   - Sign out.
2. **C2 register.** **100062004** (Library custodian) → *Register asset*.
   - Name `Dell 24-inch monitor`; description `small scratch, HDMI`.
   - **Suggest classification** (AI 1, labelled fallback without a key) → **Accept & edit** → fill ID `A-999`, quantity, room → **Save as Draft**.
   - Show the history timeline (who and when), then **Publish surplus** (confirmation dialog).
3. **C3 search.** Catalog: search `HDMI`, filter by condition and department. Point out the free/held/allocated bar.
4. **C4 + AI 2.** **100061002** → *My requests* → R-0007 "Display screens…" → **AI matches** → *Find matches*.
   - Show the top-5 list: score ring, the 7 weighted factors, n/a factors, coverage, and the "Rule-based fallback" or "Gemini" label.
   - The A-130 4K screens are **excluded** — only 1 is free, partial fulfilment is not allowed, and the exclusion list says so.
   - **Select** A-101 → set quantity 3 → **Submit for review**. The success message shows the hold expiry (48 h).
5. **C5 approval.**
   - **100062001** → *Approvals* → R-0007 *Release eligibility & safety* → Approve.
   - **100065002** → *Finance cost review* → Approve.
   - **100061002** has no approve button (you cannot approve your own request).
   - **100063002** → *Authorise* v1 → Approve.
   - Open the transfer: the asset is **still Engineering**.
6. **Release & receipt.**
   - **100062001** → *Transfers* → T-… → enter evidence and cost → *Record release*. Custody is unchanged.
   - **100062002** → *Confirm receipt*. The request becomes **Fulfilled**; the new batch A-101-n is CS-owned, with a lineage link.
   - Optional: confirm a *lower* quantity instead → **Exception**; then 100063002 resolves it.
7. **C6 history.** Open asset A-101 → timeline (Registered → Published → Reserved → Allocated → Released → Split …).
   - Then run `trace_request R-0007` to show the same rows in the database.
8. **AI 5 report.** **100065003** → *AI reports* → *Calculate snapshot* → *Generate narrative*. Without a key, the draft is clearly labelled **Template — NOT AI**.
   - Edit the text to add `38 items` → **Save** → validation blocks review ("figures not in the snapshot").
   - Remove it → tick the confirmation → **Mark as reviewed** → **Publish** (separate step).

## B. Bonus (C7–C9, AI 1, 3, 4)

- **C7.** *Sustainability*: open "Formula, inputs & source" on a card. Carbon is **Unavailable** because there is no cited factor. Record a factor marked *Synthetic* to show the estimate becoming provisional.
- **C8.** Bell icon → notifications; *Open* re-checks permission.
- **C9.** *Reports* → filter → *Export CSV* (formula-safe).
- **AI 3.** Asset AV-301 (100062004) → *Sustainable-action advice* → choose a non-top option without a reason → refused. Add a reason → you are routed to the normal proposal form.
- **AI 4.** *Assistant*:
  - "Any display screens available in Science Laboratories?" → cited A-130.
  - "Please approve R-0004" → refused, no AI call.
  - "I need 3 HDMI monitors by next month" → draft → opens the normal form.
- **Maintenance.** IT-230 has an open defect: it is restricted and absent from the catalog. As 100064001: start inspection → record "safe" → as 100062002: verify → back In Use.

## B2. Extra bonus features (see BONUS_FEATURES.md)

- **QR.** As 100062001: open A-101 → *QR label* → *Print*. Point a phone camera at it (or use the top-bar scan icon and type `A-101`). At a transfer's *Confirm receipt*, type a wrong ID → refused; the right one → "QR verified".
- **Alerts.** As 100061002: Catalog → search "projector" → *Notify me* → save. As 100062001: register and publish a projector → 100061002's bell shows "Now available".
- **Leaderboard.** Sidebar → *Leaderboard* → *All time*. Point at the formula under the table and the badge rules.
- **Photo AI.** As 100062001: *Register asset* → *Or suggest from a photo* → upload a JPEG/PNG of equipment. With a Gemini key → category, condition and tags from the image; without → clearly "Photo NOT analysed".
- **Arabic.** Press the language button (ع) in the top bar → the whole interface switches to Arabic and mirrors right-to-left; press EN to return.

## C. Rehearsal Q&A (likely examiner questions)

| Question | Short answer |
|---|---|
| Where is DFD process 4.0? | `apps/workflow`: `services.decide/release/confirm_receipt`. Store D4 = `workflow_proposalversion`, `workflow_approval`, `workflow_transfer`. See DESIGN_MAPPING §2–3. |
| How do you stop two people getting the last item? | The asset row is locked (`select_for_update`; SQLite uses IMMEDIATE transactions) and free stock is re-checked inside the transaction. A PostgreSQL test races 6 threads and exactly 1 wins. |
| Why can't an admin approve? | Roles are explicit grants. SYSADMIN has no APPROVER grant, and `grant_role` refuses self-granted approval (BR-01). |
| What if Gemini is down? | `AIGateway` records the failure code and returns a deterministic fallback, visibly labelled "not AI-generated". Manual workflows never depend on AI. |
| Is the match score a probability? | No — it is a 0–100 suitability rating from documented weights. Confidence is shown as "unavailable" because Gemini gives no calibrated value. |
| How are numbers in the AI report trusted? | The database computes and freezes them. Every number in the narrative must appear in the snapshot or review is blocked. Publishing is a separate human action. |
| Why is carbon unavailable? | No approved, cited emission factor exists; the system refuses to invent one (SUS-07/08). |
| How do you avoid double counting? | Each counted unit is linked to its event; units already counted are marked `reuse_counted`, so repeat transfers are stored with `counted=False`. |
| Classic vs AI? | See ARCHITECTURE.md §3. |
| Can a QR scan move custody by itself? | No. It only proves the right batch is in hand; the receiver still confirms quantity and condition, and a wrong label is refused. |
| Could the leaderboard be gamed by moving items back and forth? | No. Points come from counted-once impact records; repeat transfers are stored with `counted=False`. |
| Is the photo stored or used for training? | It is sent to Gemini for that one call only and not stored by the app. |
