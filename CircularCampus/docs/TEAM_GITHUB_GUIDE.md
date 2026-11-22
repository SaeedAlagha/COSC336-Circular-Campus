# Team contribution and GitHub history

The application was added to the team repository in **21 cumulative commits**, 7 per member, taking turns
(Said → Elhabib → Amr). Each commit builds on the previous one, and the application starts, migrates and
passes its tests after every commit. The full step-by-step guide (files, commit messages, explanations and
checks) is in `GITHUB_STEPS.html`, delivered with the step folders.

| # | Member | Commit |
|---|---|---|
| 1 | Said | Project setup and data model (Phase 4-5 ERD) |
| 2 | Elhabib | Shared core: validation, audit trail, launchers and design system |
| 3 | Amr | C1: Sign-in, roles, departments and server-side permissions |
| 4 | Said | C2: Asset registration with category, condition, department and status |
| 5 | Elhabib | C3: Publish surplus items, search and filters |
| 6 | Amr | C4: Resource requests, status tracking and 48-hour holds |
| 7 | Said | C5: Approval stages with no self-approval |
| 8 | Elhabib | C5: Transfers - release, receipt and custody change |
| 9 | Amr | C6: Asset states, life-cycle history and audit trail |
| 10 | Said | AI 2: Matching and ranking with the Gemini AI gateway |
| 11 | Elhabib | Bonus: C7 Sustainability dashboard with formulas and sources |
| 12 | Amr | AI 5: Generated report - database totals, AI draft, human review and publish |
| 13 | Said | Bonus: C8 In-app notifications and role dashboard |
| 14 | Elhabib | Bonus: C9 Operational report and CSV export |
| 15 | Amr | Bonus: Maintenance, repair and disposal workflow |
| 16 | Said | Bonus: AI 1 Classification and tagging |
| 17 | Elhabib | Bonus: AI 3 Sustainable-action advice |
| 18 | Amr | Bonus: AI 4 Campus assistant and help page |
| 19 | Said | Bonus: QR labels and scan-to-confirm |
| 20 | Elhabib | Bonus: Availability alerts and department leaderboard |
| 21 | Amr | Bonus: Photo classification and Arabic/English interface |

**Honesty.**
- The code base was produced with AI assistance; see AI_USE_DECLARATION.md.
- Each member reviewed, ran and can explain the commits they pushed.
- Commits are made from each member's own account, with truthful messages and no back-dating.
- The earlier phase reports in the repository are unchanged.

**Why some features have no screen until a later step.** Step 1 contains the whole data model because the Phase 4-5 tables reference each other. The menu and links show a page only once its step is installed (`installed` template filter, `apps/core/links.py`), so the application runs correctly at every intermediate commit.
