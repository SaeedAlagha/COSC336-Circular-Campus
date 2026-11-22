# Circular Campus

**Intelligent, AI-Powered Circular Campus Resource Exchange and Asset Life Cycle Management System**
COSC 336 — Phase 6 working prototype · Said Alagha, El Habib Bellahouel, Amr El Ata

A Django web application that lets departments publish surplus assets, request them through staged human approval, hand them over with separate release and receipt confirmations, track maintenance and end-of-life decisions, and report evidence-based sustainability results. Gemini adds classification, semantic matching, action advice, a grounded assistant and report narratives. Every AI feature has a clearly labelled rule-based fallback.

> All demo people, assets and amounts are **synthetic**. No carbon factors are seeded, so carbon results stay "Unavailable" until a sustainability officer records a cited factor.

---

## 1. Quick start

**Requirement:** Python **3.13** (3.12 also supported) from <https://www.python.org/downloads/>.
On Windows, tick **"Add python.exe to PATH"** during installation. No Docker, WSL or database server is needed — the demo uses SQLite.

### Windows (PowerShell or File Explorer)

1. Extract the ZIP (any folder; spaces in the path are fine).
2. Double-click **`setup.bat`** — or in PowerShell, from the extracted folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
3. Double-click **`run.bat`** — or:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run.ps1
   ```
4. Open **<http://127.0.0.1:8000/>**. Stop the server with **Ctrl+C** in its window; start it again with `run.bat`.

### macOS (Terminal)

```bash
cd "/path/to/Circular Campus"
```
```bash
chmod +x setup.command run.command
```
```bash
./setup.command
```
```bash
./run.command
```
Open **<http://127.0.0.1:8000/>**. Stop with **Ctrl+C**; restart with `./run.command`. (You can also double-click the `.command` files in Finder; if macOS blocks them, right-click → Open.)

**What setup does.** It creates a project-local `.venv`, installs the pinned dependencies, and creates `.env` with a random secret key — **only if `.env` does not exist**. It then applies migrations and loads the synthetic demo data. The demo load is **skipped automatically if the database already has data**: setup never deletes records or overwrites `.env`. Every step stops with a clear message on failure.

Use another port: `run.bat --port 8010` · `./run.command --port 8010`.

---

## 1b. Real installation (no demo data)

To start with an **empty** system and your own real accounts instead of the sample data, use a fresh folder (or delete `data/circular_campus.sqlite3` and `.env`) and run:
- Windows: `.\setup.bat --real`
- macOS: `./setup.command --real`

Setup asks for the **first System Administrator** (campus ID, name, department and password). It also creates the standard roles and category list, and sets `CAMPUS_DEMO_MODE=0`, so there is no demo banner and no demo account list. Then:
1. Sign in as that administrator.
2. Go to **Departments** and add your departments.
3. Go to **Accounts** to create staff and grant their roles (requesters, custodians, approvers…).

Run `python manage.py init_system` later to do the same step on its own. It refuses to run once any account exists.

## 2. Demo accounts (local demonstration only)

Password for all demo accounts: **`CircularCampus2026!`** — or the value of `CAMPUS_DEMO_PASSWORD` in `.env` if you set it *before* the first setup. These are local demo credentials, not production accounts.

| Campus ID | Name | Role (scope) |
|---|---|---|
| 100061001 | Layla Haddad | Requester (CS) |
| 100061002 | Omar Farouk | Department Representative + Requester (CS) |
| 100061003 | Reem Al Suwaidi | Requester (ENG) |
| 100062001 | Sara Al Mansoori | Asset Custodian (ENG) |
| 100062002 | Yousef Nasser | Asset Custodian (CS) |
| 100062003 | Mariam Saeed | Asset Custodian (SCI) |
| 100062004 | Hessa Al Marri | Asset Custodian + Requester (LIB) |
| 100063001 | Dr. Hamad Al Ketbi | Assigned Approver (campus-wide) |
| 100063002 | Dr. Aisha Rahman | Assigned Approver (CS) |
| 100064001 | Khalid Ibrahim | Maintenance Staff |
| 100065001 | Noura Al Hammadi | Procurement Officer |
| 100065002 | Rashid Al Zaabi | Finance Officer |
| 100065003 | Fatima Khoury | Sustainability Officer |
| 100069001 | Ahmed Saleh (IT) | System Administrator (no approval authority) |
| 100069002 | Huda Al Nuaimi | Management / Auditor |

The sign-in page also lists these accounts while `CAMPUS_DEMO_MODE=1`. Set `CAMPUS_DEMO_MODE=0` to hide the list and the demo banner.

---

## 3. Gemini (optional, private)

The app works fully without a key, using labelled fallbacks. To enable live Gemini:

1. Create a key in Google AI Studio.
2. Open **`.env`** in a text editor and paste it after `GEMINI_API_KEY=`. Never paste it into chat, code, screenshots or the ZIP; `.env` is git-ignored.
3. Restart the server.
4. Run the optional **live smoke test**. It prints pass/fail and model names, never the key:
   - Windows: `.venv\Scripts\python manage.py gemini_smoke`
   - macOS: `.venv/bin/python manage.py gemini_smoke`

`GEMINI_MODEL` defaults to `gemini-3.8-flash` and `GEMINI_EMBEDDING_MODEL` to `gemini-embedding-001`. Both are configurable. They were taken from the official `google-genai` SDK v3.6.1 because ai.google.dev could not be reached from the build environment. If the smoke test reports a model as MISSING, it lists the models your key can see; copy one into `.env`.

**Verification status:** the Gemini adapter is tested with mocked HTTP responses only. **No live Gemini call was made during development.**

---

## 4. PostgreSQL (optional, matches design DR-02)

1. Install PostgreSQL 15+ and create a database and user, for example `campus` / `campus`.
2. Install the driver:
   - Windows: `setup.bat --postgres`
   - macOS: `./setup.command --postgres`
3. In `.env`, uncomment and fill `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` and `POSTGRES_PORT`.
4. Run setup again: it migrates and seeds PostgreSQL. SQLite is used whenever `POSTGRES_DB` is empty.

---

## 5. Useful commands

Run these from the project folder. On Windows use `.venv\Scripts\python`; on macOS use `.venv/bin/python`.

| Command | Purpose |
|---|---|
| `python manage.py test` | Full automated test suite (97 tests; the PostgreSQL race test is skipped on SQLite) |
| `python manage.py trace_request R-0004` | Print the DB rows behind a request (for the oral demo) |
| `python manage.py gemini_smoke` | Optional live Gemini check |
| `python manage.py evaluate_ai` | Classification on a fixed 12-item synthetic sample, stored as an EvaluationRun |
| `python manage.py expire_holds` | Release unapproved holds older than 48 h (also runs automatically) |
| `python manage.py seed_demo` | Load demo data into an **empty** database (refuses otherwise) |

**Fresh demo database:** stop the server, delete `data/circular_campus.sqlite3`, then run setup again. This deletes all local data.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `python` not found / wrong version | Install Python 3.13 from python.org (Windows: tick "Add to PATH"), then re-run setup. |
| PowerShell says scripts are disabled | Use `setup.bat` / `run.bat`, or the `powershell -ExecutionPolicy Bypass -File …` commands above. |
| macOS "cannot be opened" | Right-click the `.command` file → Open, or run it from Terminal after `chmod +x`. |
| "Port 8000 is already in use" | Another server is running. Close it, or start with `--port 8010` and open that port. |
| "Setup has not been run yet" | Run setup before run. |
| `DJANGO_SECRET_KEY is not set` | `.env` is missing; run setup (it creates `.env`). |
| pip install fails | Check your internet connection or proxy, then re-run setup. It is safe to repeat. |
| AI shows "Rule-based fallback" | Expected without a key. With a key, run `gemini_smoke` to see the exact error code (AUTH_FAILED, MODEL_NOT_FOUND, RATE_LIMITED…). |
| Forgot which data exists | Sign in as the auditor (100069002) → Audit trail, or use `trace_request`. |

---

## 7. Documentation

| Document | Contents |
|---|---|
| [docs/BONUS_FEATURES.md](docs/BONUS_FEATURES.md) | Bonus features: QR labels and scan-to-confirm, availability alerts, leaderboard and badges, photo classification, Arabic/English |
| [docs/REQUIREMENTS_TRACEABILITY.md](docs/REQUIREMENTS_TRACEABILITY.md) | C1–C9 and AI 1–5 checklist with sources, modules, screens, tables, tests and status; FR/BR coverage |
| [docs/DESIGN_MAPPING.md](docs/DESIGN_MAPPING.md) | DFD processes, flows and stores → code, tables and UI; classes; ERD; state machines; deviations |
| [docs/DATABASE_GUIDE.md](docs/DATABASE_GUIDE.md) | Opening the DB safely on Windows/macOS, tables and keys, before/after demo |
| [docs/DEMO_WALKTHROUGH.md](docs/DEMO_WALKTHROUGH.md) | Step-by-step demo script and rehearsal Q&A |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture, classic vs AI comparison, limitations |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | Commands actually run, results, browser checks, what is unverified |
| [docs/ZIP_AUDIT.md](docs/ZIP_AUDIT.md) | Audit of the earlier code ZIP and why it was not reused |
| [docs/TEAM_GITHUB_GUIDE.md](docs/TEAM_GITHUB_GUIDE.md) | Three-member file ownership, branches, commits, PR order |
| [docs/AI_USE_DECLARATION.md](docs/AI_USE_DECLARATION.md) | How AI assistance was used to build this deliverable |
| [docs/PHASE6_IMPLEMENTATION_REPORT.md](docs/PHASE6_IMPLEMENTATION_REPORT.md) | Implementation environment and selected features (slide 5) |
| `docs/screenshots/` | Browser screenshots captured from the running app (synthetic data) |

**Local demo vs production.** `.env` from setup uses `DJANGO_DEBUG=0` on `127.0.0.1` over plain HTTP. For any real deployment: serve behind HTTPS with `CAMPUS_HTTPS=1`, use PostgreSQL, set `CAMPUS_DEMO_MODE=0`, never load demo data, and set strong admin passwords.
