# Phase 6 — implementation environment and selected features

Slide 5 asks for a short report on the implementation environment and the features selected for implementation.

## Implementation environment

| Item | Choice | Reason / link to design |
|---|---|---|
| Language / framework | Python 3.13 (3.12 supported), Django 5.2 LTS | DR-01 modular monolith; Django's ORM, transactions, CSRF and auth |
| Database | SQLite for the local demo; PostgreSQL 16 supported and tested | DR-02 chose PostgreSQL for row locking. SQLite keeps the classroom demo install-free. |
| AI provider | Google Gemini API (REST v1beta, server-side key); text model `gemini-3.8-flash`, embeddings `gemini-embedding-001` (configurable) | Team decision; typed adapter behind `AIGateway` (design 5.9) |
| UI | Server-rendered HTML; custom CSS design system; local Inter font and Lucide icons; no CDN | NFR-21/22, CSP, offline-safe demo |
| Static files | WhiteNoise | Works with `DEBUG=0` without a web server |
| Tests | Django test runner (97 tests); Playwright for browser checks | NFR-15, NFR-18 |
| Version control | Git / GitHub (see TEAM_GITHUB_GUIDE.md) | Course requirement |

## Features selected and implemented

| Feature | Requirement level (instructor) | Implemented |
|---|---|---|
| C1 Login and roles | Mandatory | Yes — 10 roles with department scope, server-side policy |
| C2 Departments and assets | Mandatory | Yes |
| C3 Publish and search | Mandatory | Yes |
| C4 Request and track | Mandatory | Yes |
| C5 Approve and transfer | Mandatory | Yes — 3 approval stages + release + receipt |
| C6 States and history | Mandatory | Yes |
| C7 Sustainability dashboard | Bonus | Yes — 9 indicators with formulas and sources |
| C8 Notifications | Bonus | Yes |
| C9 Basic report + CSV | Bonus | Yes |
| AI 1 Classification and tagging | Bonus | Yes |
| AI 2 Matching and ranking | Mandatory | Yes |
| AI 3 Sustainable-action advice | Bonus | Yes |
| AI 4 Assistant | Bonus | Yes |
| AI 5 Generated report | Mandatory | Yes |

The status of each, with evidence, is in REQUIREMENTS_TRACEABILITY.md and TEST_REPORT.md.
