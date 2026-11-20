# Circular Campus — Phase 6 prototype

COSC 336, Khalifa University, Group 3. Based on the submitted Phase 3 SRS and Phase 4/5 design. This is a draft implementation with major features, not a production university system. All demonstration records are synthetic.

## Review bundle integration

The `amr/phase6-foundation` branch contains only the foundation portion: configuration, data model, role policy, workflow services, administration and four foundation checks. Its home page stays in foundation mode until the interface and AI/reporting bundles are integrated. The complete submission ZIP contains all three portions and runs the full application. Teammates should review, test and adapt their respective bundles before committing under their own accounts; the split does not claim completed equal contributions.

## Run locally

Python 3.11+ is required (verified on Python 3.13). From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
export CAMPUS_DEMO_PASSWORD='Choose-your-local-demo-password'
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8765
```

Open http://127.0.0.1:8765 . The seed command only accepts an empty database and never overwrites existing records. Keep your chosen password local. Do not publish databases, attachments, credentials or virtual environments.

The optional `.env.example` is documentation; the application reads exported environment variables, not an automatic dotenv file. SQLite is the zero-setup local demonstration mode. Set `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` and `POSTGRES_PORT` before migrate to use the designed PostgreSQL backend. Use a dedicated test database; the test runner needs permission to create one. PostgreSQL must be installed and running separately. SQLite cannot verify PostgreSQL row-lock/concurrency behavior.

## Demonstration accounts

All seeded accounts use the value you supplied in `CAMPUS_DEMO_PASSWORD`.

| Username | Role | Main actions |
|---|---|---|
| requester | Requester | Browse, request, compare, reserve, withdraw pending hold |
| representative | Department Representative | Department needs and tracking |
| custodian | Source Asset Custodian | Register, publish, safety review, release, verify service |
| receiver | Receiving Asset Custodian | Confirm actual receipt and manage received stock |
| approver | Assigned Approver | Independent version-bound decisions and reconciliation |
| maintenance | Maintenance Staff | Inspection and no-cost service evidence |
| finance | Finance Officer | Transfer-cost review and purchase-substitution evidence |
| procurement | Procurement Officer | Purchase-substitution evidence and reports |
| sustainability | Sustainability Officer | Impact counts, carbon evidence visibility, policy advice |
| auditor | Management / Auditor | Read-only reports and audit filters |
| sysadmin | System Administrator | Django admin: create users, profiles and departments |

A prototype account has one role and one department. To create an account, the administrator creates an active User, then its Profile with an explicit role and department. Business approval is never implied by technical administrator status. Use separate accounts during the demonstration to make responsibility clear.

## Working capabilities

* Registration with complete core metadata and validated private PNG/JPEG/PDF attachments; safe publication; search by category, condition, location, department, quantity, value, purchase date and status.
* Requests with required specifications, condition, date, urgency, destination and explicit partial-supply preference.
* Atomic holds, 48-hour pending expiry, idempotent reservation, independent staged approval, finance review when cost is present, release, exact receipt, exceptions, stock split lineage, cancellation and notifications.
* Defect reporting immediately restricts stock. No-cost inspection/service requires a separate custodian safety verification before reuse. Existing allocations with a new safety restriction must be cancelled by their approver and resubmitted for fresh review after verification.
* Donation/recycling/disposal proposal, independent authorization and separate completion evidence. Whole stock segment is handled by each disposition.
* Local Naive Bayes classification and TF-IDF/latent semantic analysis matching. Hard eligibility checks run first. The final score follows the design weights and excludes unsupported cost/environment factors, displaying evidence coverage. Confidence is explicitly unavailable.
* Optional OpenAI-compatible LLM adapter for assistant replies and report drafts, with timeout/schema fallback and read-only context. The deterministic sustainability-action fallback is labelled as policy advice, not an AI prediction.
* Operational summaries, period/cohort calculations, financial evidence, unknown-value handling, unit-range deduplication, immutable data snapshots and human narrative review.
* Role checks, CSRF, hashed passwords, private file reads, append-only application audit, protected history, and in-app notifications.

## AI configuration and limits

Classification and matching run offline. The training corpus contains 18 hand-authored descriptions and is included in `campus/intelligence.py`. This demonstrates statistical NLP, not a pretrained large model or validated production semantic service. There is no supported accuracy percentage. Mandatory technical specifications are literal required terms; they are not guessed from similarity.

To enable the assistant and narrative generator, set `CAMPUS_LLM_URL` to your approved OpenAI-compatible chat-completions endpoint, `CAMPUS_LLM_MODEL` to its model name and `CAMPUS_LLM_KEY` if needed. Example: a locally installed server may expose `http://127.0.0.1:11434/v1/chat/completions`. The model/server is not bundled. No external provider is called until the operator explicitly configures one. Only authorized listing summaries or report-snapshot data are sent. Treat replies as untrusted drafts. A live provider was not available for verification; the unavailable/manual path was verified.

The LLM gateway has an 8-second request timeout, a 100 KB response bound, schema validation and a read-only prompt. This synchronous draft does not yet include the proposed production job queue, embedding cache, provider telemetry, calibrated confidence, image classification or evaluated fairness/relevance dataset.

## Verification

```sh
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test campus --verbosity 2
```

The included automated checks cover stock/idempotency, workflow authorization, receipt exceptions, source/receiver custody, expiry, score eligibility, unknown metrics, rollback, unit deduplication, role routes and CSRF. They are Phase 6 implementation checks, not a completed Phase 7 quality-assurance report. Run separate PostgreSQL concurrent-user tests, live-provider evaluation, upload/security tests, usability/UAT, backup/recovery and performance tests before production or Phase 7 claims.

## Deliberate prototype limits

No university SSO, enterprise integration, email delivery, background worker, automatic transport arrangements, paid repair work-order approval, partial receipt acceptance, editable returned proposal version history, or complete Phase 3 requirement coverage is claimed. Returned/rejected requests use a new hold rather than editing old decision evidence. Split received records keep ancestry and unit positions; attachments remain with the original source record. Purchase evidence is immutable once reviewed; correction/adjustment workflows are future work. Lifespan extension is calculated on reviewed substitution evidence; overlapping extensions across repeated purchases need further control. Reporting counts are basic operational summaries, not every detailed management report layout from the design.

For deployment, provide a strong secret, DEBUG=0, trusted hosts, HTTPS, separate least-privilege database credentials and protected storage. Do not use Django's development server in production.

## AI assistance and authorship

This implementation and accompanying materials were prepared with AI assistance from the submitted design. Each team member must review, understand, test and adapt their allocated portion before committing it. Commit dates and authorship should describe actual work; do not backdate or impersonate contributions. The three review bundles are a proposed division, not evidence of completed equal contributions.
