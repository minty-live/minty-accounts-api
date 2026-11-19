# AI allowances (Minty support)

Owner, 2026-11-18. Every company reads receipts (Petty Cash) and invoices (Payment Request) with
AI against its own **grants** - minty-db v0.13's `ai_grant` - and every **superuser** (`system_role`
`superadmin`) can see each company's allowance and give it a grant here. Anyone else is refused
(403 "Only Minty support can manage AI allowances."). minty-web draws it as Select Company's
**AI allowances** mode, beside Archive companies (owner, 2026-11-19: no page of its own; its
`docs/features/entities.md`).

| Route | What |
|---|---|
| `GET /api/me/ai/companies[?q=&page=\|?ids=]` | Companies not archived, A to Z, 25 a page (`q` filters by name; `ids` = exactly those companies, at most 500, no page - the list's AI mode): `percent_left`, `blocked`, `tokens_used`/`tokens_given` over the grants that still have room, `has_grant` (false = the default grant it gets on its first read), answered `reads`, `last_read_at`; `total` |
| `GET /api/me/ai/companies/<id>` | One company: `allowance` (grants and the extra caps), `default_grant`, every grant newest first (`source`, totals, used, `expires_at`, `open`, `granted_by`, `note`), its last 20 reads |
| `POST /api/me/ai/companies/<id>/grants` | `{tokens?, reads?, days?, note?}` - at least tokens or reads; tokens 1-10,000,000, reads 1-10,000, days 1-3,650 (empty = no end), note up to 255; commas allowed. `granted_by` = the superuser's email. 201 with the company again and `message`; 422 `{error, fields}` with one sentence per field |

Code: `hub/ai_grants.py` on `minty_shared.django.ai_allowance` (minty-shared-py v0.7) - the same
code the two readers reserve and charge with, so the page and the read button never disagree. A
missing or unusable `ai_policy` row answers 503 (reading is off). The models, the default grant and
the extra caps are not here (owner: grants only); they stay on minty-petty-cash-api's
`manage.py ai_policy`.

Tests: `hub/tests/test_ai_grants.py`.
