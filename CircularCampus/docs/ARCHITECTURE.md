# Architecture, classic vs AI, and limitations

## 1. Architecture

```
Browser (HTML + local CSS/JS, no CDN)
   │  HTTPS in deployment · CSRF · session cookie
Presentation  — templates/, apps/*/views.py (thin), apps/*/forms.py
   │  calls one command per action
Application   — apps/*/services.py   (transactions, idempotency, state machines)
   │                     │
Policy        — apps/accounts/policy.py  (deny-by-default, role + department scope)
   │
Domain/Data   — apps/*/models.py  →  SQLite (demo) or PostgreSQL (DR-02)
   │
AI gateway    — apps/intelligence/gateway.py → gemini.py (REST, server-side key, timeout)
                labelled deterministic fallbacks; AITask/AIResult provenance
Reporting     — apps/reporting/metrics.py (deterministic), audit.py (append-only + notices)
```

**Key engineering decisions.**
- **Modular monolith, one app per DFD process.** This keeps one transaction boundary for stock, workflow, audit and notices (NFR-03).
- **Every command is a service function** that re-checks policy, locks rows in a stable order (asset, then request, then reservation), validates the state transition, and writes the business change, life-cycle event, audit event and notifications atomically.
- **Optimistic version numbers on forms** (stale edits are rejected), plus **idempotency keys** (a repeated submission returns the original result).
- **Concurrency.** PostgreSQL row locks (`select_for_update(of=self)`); on SQLite, `transaction_mode=IMMEDIATE` serialises writers.
- **AI calls go through `AIGateway`.** It has no approve, release or dispose operation, so AI cannot change authoritative state.

## 2. Security controls

- Deny-by-default policy, checked on every view and again in every service.
- Denied attempts are written to the audit trail.
- CSRF protection; `SameSite` cookies; a Content-Security-Policy allowing only same-origin scripts and styles; `X-Frame-Options: DENY`; `nosniff`.
- Passwords stored as salted hashes, minimum 12 characters.
- Uploads are validated by extension, magic bytes and size, stored outside the static tree, and served only through a permission check.
- The Gemini key stays server-side (`.env`) and is never logged or stored. AI prompts carry only minimal, permitted context; record text is wrapped as untrusted data.
- Every exported CSV cell is neutralised against formula injection.

## 3. Classic vs AI-enhanced — comparison

| Function | Classic (deterministic) | AI-enhanced | Where AI adds value / where rules must win |
|---|---|---|---|
| Search | Keyword + structured filters (category, condition, department, location, quantity, age, value, status) | Semantic matching using Gemini embeddings (fallback: concept/synonym similarity) | AI finds "display screen" for "monitor". Rules still decide visibility, safety and eligibility. |
| Matching | Hard filters (condition, mandatory specs, date, quantity, safety) | Weighted 0–100 score with an explanation per factor | Ranking and explanation help users choose; the score never reserves or approves. |
| Classification | Manual category / condition entry | Suggested category, condition, tags, missing metadata | Saves typing; the custodian accepts, edits or rejects, and no confidence is invented. |
| Life-cycle decision | Custodian judgement + approval workflow | Ranked actions with evidence-based reasons (disposal last) | Structures the reuse-first hierarchy; humans decide and approve. |
| Reporting | Tables, CSV, calculated indicators with formulas | Short narrative from a frozen snapshot | Readability. Every number must come from the snapshot; human review and publication are mandatory. |
| Help | Help page, policies, catalog | Grounded assistant with citations and request drafts | Faster navigation. It refuses actions and says "I don't know". |

**Risks handled:**
- Hallucinated records and figures — citation and number validation.
- Prompt injection — data wrapping plus a server-side refusal guard.
- Over-trust — scores are labelled "suitability, not confidence".
- Privacy — minimal context only.
- Outages — labelled fallbacks.
- Bias — the same documented weights apply to every department, and no department preference rule exists.

## 4. Known limitations (prototype)

- **No live Gemini call was verified.** The adapter is tested only with mocked HTTP; run `gemini_smoke` with your key.
- Model IDs come from the official SDK, not the docs site, which was unreachable here.
- AI calls are synchronous with a 10 s timeout; there is no background job queue (design pattern C simplified).
- Classification uses text only; photos are stored but not analysed.
- No email notifications (Phase 3 marks email as *desirable*).
- No load or performance test at the 1,000-asset baseline. No backup/restore drill (NFR-20); manual copy only.
- Browser checks covered Chromium only. Windows and macOS launchers were not executed on those operating systems here.
- The evaluation sample (`evaluate_ai`) is 12 synthetic items written by the team. It is not evidence of real-world accuracy.
- The synthetic demo history is back-dated by the seed command, so charts have a time axis; every scenario still ran through the real services.
- Institution-specific values remain prototype defaults: approver assignments, the 48 h hold, thresholds and emission factors (Phase 3 Appendix C).
