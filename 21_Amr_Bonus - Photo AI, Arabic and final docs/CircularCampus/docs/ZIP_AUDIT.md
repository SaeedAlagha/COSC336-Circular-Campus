# Audit of the supplied `Phase6_Package.zip`

The ZIP was treated as untrusted reference material. It was extracted and run **outside** the new project; the original upload was not modified.

## What it contained

- `Source_Code.zip` — a Django 5.2 app, about 1,240 lines of Python. All 15 templates were minified onto single lines, and the CSS was one line.
- Teammate review bundles, screenshots, PDFs, `Implementation_Checks.txt` (28 passing tests), and a note that only a "foundation" branch was pushed.

## Independent verification

- Installed Django 5.2 and scikit-learn in a scratch venv and ran its suite: **28 tests passed on SQLite.**
- These passing tests say nothing about PostgreSQL behaviour. The package itself states that PostgreSQL concurrency was never verified.

## Findings (why it was not used as the foundation)

| Area | Finding | Design it conflicts with |
|---|---|---|
| Roles | One `Profile.role` per user; no department-scoped grants | ERD 5.3.1 `UserRole` with scope; matrix 4.6.1 ("a person may hold several grants") |
| Data model | No `Category` table (hard-coded choices); no `ProposalVersion` / staged `Approval`; no `ModelVersion`, `AITask`, `HumanReview`, `MatchRecommendation`, `ReportMetric`, `AssetLineage` (only `parent` + integer offsets) | ERD 5.3.2–5.3.10 |
| States | Asset status limited to Available / Restricted / Retired / Donated / Recycled / Disposed | State models Figures 37–43 |
| AI | No Gemini integration (OpenAI-compatible endpoint); scikit-learn TF-IDF only | Team decision to use Gemini |
| Maintainability | Minified templates; business logic mixed into `views.py` (e.g. publish toggling) | Phase 6 slide 2 "structure into classes"; layered design 4.3.2 |
| Reporting | Narrative "review" flag without separate publication or numeric validation | AI-5 workflow |

## Decision

**Rebuild in a new folder** (`CircularCampus/`), implementing the submitted ERD, state machines and stages directly. Ideas reused *in concept* only:
- idempotency keys with payload-mismatch rejection;
- compare-and-swap on the asset version;
- consistent lock ordering.

No ZIP code was copied.
