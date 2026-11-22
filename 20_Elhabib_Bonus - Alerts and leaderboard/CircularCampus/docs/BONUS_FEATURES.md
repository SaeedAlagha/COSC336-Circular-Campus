# Bonus features (beyond C1–C9 and AI 1–5)

These five features were added after the core system was complete. Each one reuses the existing rules (permissions, validation, audit, counted-once sustainability records), so none of them can bypass the approved workflow.

Demo password for every account: `CircularCampus2026!`

| # | Feature | Where | Who | Main code | Tests |
|---|---|---|---|---|---|
| B1 | QR labels and scan-to-confirm | Asset page → **QR label**; My assets → **Print QR labels**; top bar scan icon; Transfer → *Confirm receipt* → **Verify the item** | Custodians print; anyone scans; the receiving custodian verifies | `apps/catalog/qr.py`, `apps/catalog/views.py` (`QRLookupView`, `LabelSheetView`, `ScanView`), `apps/workflow/services.py` (`match_scanned_code`), `static/js/scan.js`, `static/vendor/jsqr/` | `apps/core/test_bonus.py::QRTests` |
| B2 | Availability alerts ("notify me") | Catalog → **Notify me**; sidebar **My alerts** | Requesters / department reps | `apps/exchange/models.py` (`SavedSearch`, `WishlistHit`), `apps/exchange/services.py` (`save_search`, `notify_wishlists`) | `AvailabilityAlertTests` |
| B3 | Leaderboard and badges | Sidebar **Leaderboard** | Every signed-in user (AED column only for reporting roles) | `apps/reporting/leaderboard.py` | `LeaderboardTests` |
| B4 | Photo classification (AI) | Register asset → AI card → **Or suggest from a photo** | Custodians | `apps/intelligence/classification.py` (`suggest_from_photo`), `apps/intelligence/gemini.py` (inline image) | `PhotoClassificationTests` |
| B5 | Arabic / English interface | Top bar language button (ع / EN) | Everyone, including the sign-in page | `config/settings.py` (`LANGUAGES`, `LocaleMiddleware`), `templates/**` (`{% translate %}`), `locale/`, `scripts/i18n_*.py`, RTL rules at the end of `static/css/app.css` | `ArabicInterfaceTests` |

## B1 — QR labels and scan-to-confirm

- **Labels.** Each label is a QR code generated on the server with the `segno` library (BSD licence); no online service is used. The code holds a link such as `http://127.0.0.1:8000/catalog/q/A-101/`, and the asset ID is printed underneath.
- **Scanning.**
  - Any phone camera app opens that link. The person signs in, and the normal permission check applies.
  - The in-app scanner uses the browser's own `BarcodeDetector`, or the bundled **jsQR** library (Apache-2.0) where that isn't available, for example Chrome on Windows.
  - Frames are decoded on the device; nothing is uploaded.
- **Scan-to-confirm.** At handover, the receiving custodian can scan or type the label. The server checks that it names the transferred batch:
  - **Wrong label:** refused, and nothing changes.
  - **Right label:** the receipt is stored as *QR verified* and recorded in the audit trail (`qr_verified`).
- **Camera limits.** Browsers only allow the camera on `https://` or `http://localhost`. Over plain HTTP on another device, use a phone camera app or type the code.

## B2 — Availability alerts

- **Alert criteria.** A requester saves keywords and/or a category, a minimum condition and a minimum number of free units.
- **Matching.** When a custodian **publishes** stock, every active alert is checked with plain rules (no AI). Matching users get one in-app notification per item:
  - Republishing the same item never notifies the same alert twice.
  - A department's own stock never triggers its own alerts.
- **Limits.** Up to 10 alerts per person. The validation rules from `apps/core/validators.py` apply to every field.
- **Never reserves stock.** An alert doesn't reserve anything; the requester still submits a normal request.

## B3 — Leaderboard and badges

**Points (shown on the page):**

`10 × units given + 5 × units received and reused + 8 × units diverted (donated, recycled, components) + 15 × repairs + 1 per AED 100 of confirmed avoided purchases`

- **Source of the numbers.** Every figure comes from the same counted `SustainabilityImpact` records as the C7 dashboard.
  - Diversion records that belong to a reuse or repair event are excluded, so no unit earns points twice.
- **Badges.** First share, Generous giver, Circular champion, Waste diverter, Fix-it team, Budget saver. The rule for each is listed on the page.

## B4 — Photo classification

- **How it works.** A JPEG/PNG of up to 5 MB is checked by content, then sent to Gemini as inline image data for this one call. It is **not stored**.
- **Output.** Gemini must choose a category from the allowed list, and its output is validated like AI feature 1. The custodian accepts, edits or rejects the suggestion as before.
- **Without Gemini.** The result says clearly: *"Photo NOT analysed — image understanding needs Gemini"*. If a name or description was typed, the labelled text rules are used instead; nothing pretends to have read the image.

## B5 — Arabic / English

- **Language selection.** Django's standard i18n is used. The language button posts to `/i18n/setlang/` and the choice is kept in a cookie. A browser set to Arabic gets Arabic automatically.
- **Right-to-left layout.**
  - When Arabic is active, the whole layout mirrors (`dir="rtl"`).
  - Asset IDs, codes, numbers and e-mail addresses stay left-to-right inside Arabic text.
  - Arabic keeps Western digits and `.` decimals (`config/formats/ar/formats.py`), so numbers typed into forms stay valid.
- **What is translated.** 1,620 interface strings:
  - every fixed text in the templates;
  - menus, field labels, help texts and dropdown options;
  - status and condition names, badges and error messages;
  - Django's own built-in messages.
- **What stays in the original language:**
  - data people type (asset names, request titles, notes);
  - AI outputs;
  - formulas;
  - messages that contain live values, for example "R-0007 sent for review (version v1)".
- **Editing translations.**
  1. Run `python scripts/i18n_collect.py` to list the strings.
  2. Edit `locale/ar/part*.json` (same order as `locale/msgids_v1.json`), or add `{english: arabic}` pairs to `locale/ar/extra.json`.
  3. Run `python scripts/i18n_build.py` to write `django.po` / `django.mo`. GNU gettext is not needed.
- **Accuracy.** The Arabic text was machine-drafted by the AI assistant. A native speaker on the team should review it before it is presented as final.

## Third-party components added

| Component | Licence | Use |
|---|---|---|
| segno 1.6.6 (Python) | BSD-3-Clause | QR code generation (`requirements.txt`) |
| jsQR 1.4.0 (JavaScript) | Apache-2.0 (`static/vendor/jsqr/LICENSE`) | QR decoding in browsers without `BarcodeDetector` |
