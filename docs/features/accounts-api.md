# The accounts API

## What it does (Phases 1-7, 2026-11-05 to 11-06)

minty-accounts-api stands where Flask stood, at `accounts-api.dailyminty.com` (development:
`dev-accounts-api.`). Web apps, the other APIs and Xero's callback URL keep calling the same
address. Until Phase 7 it fell through to Flask for every path it did not serve; that
fall-through is gone (2026-11-06), so a path not below is a 404, and Flask is no longer used.

| Path | Answered by |
|---|---|
| `/health` | Django: `{"status": "ok", "service": "minty-accounts-api"}`. It never calls Flask |
| `POST /api/internal/xero/token` | Django (Phase 2): the Xero token service, below |
| `POST /api/internal/xero/sync` | Django (Phase 3): the one Xero account/contact sync, below |
| `POST /api/internal/session/touch` | Django (Phase 9, 2026-11-19, `hub/session_store.py`): the other APIs keep a sign-in alive and learn it has ended (410) - sign-out after 60 minutes without activity, authentication.md section 3.3 |
| `POST /api/handoff/redeem` | Django (gap 4, 2026-11-09, `hub/handoffs.py`): an app's landing swaps its one-time code for its token |
| `GET /auth/notices`, `GET /legal/current`, `POST /legal/invite-terms-status` | Django (Phase 5A, `hub/public.py`) |
| `GET /api/me/entities`, `GET/PATCH /api/me/profile`, `GET /api/me/terms`, `POST /api/me/terms/accept`, `POST /api/me/contact` | Django (Phase 5B, `hub/`). Each company row carries `xero_org_name` (2026-11-16, minty-web's Xero badge): `xero_tenant_name` for a `connected` company, else null |
| `GET /api/me/company/users`, `PATCH/DELETE /api/me/company/users/<id>`, `POST /api/me/company/invitations`, `POST .../invitations/<id>/cancel` and `/resend`, `GET/PATCH /api/me/company/integration` | Django (Phase 5C, `hub/company.py`) |
| `POST /api/me/company/xero/disconnect`, `POST /api/me/company/xero/release` | Django (Phase 5D, `hub/company.py`, `xero/services/disconnect.py`) |
| `POST /api/me/entities/<id>/archive`, `POST /api/me/entities/<id>/restore` | Django (2026-11-10, `hub/entities.py`): archived companies, below |
| `POST /api/onboarding/modules`, `GET/POST /api/onboarding/invite`, `POST /api/onboarding/invite/resend` and `/cancel`, `POST /api/onboarding/xero/disconnect` and `/release` | Django (Phase 5D, `hub/onboarding.py`) |
| `GET /static/img/new_logo.png` | Django (Phase 5C): the invitation email's logo, the same file Flask serves |
| `/`, `/index`, `/register`, `POST /auth/email/request-code`, `/verify-code`, `GET /auth/email/handoff`, `/invitation/accept/<token>` | Django (Phase 6B, `hub/email_signin.py`, `hub/front_door.py`) |
| `/entity`, `/entity/create`, `/entity/<co>/petty-cash`, `/modules`, `/enter`, `/payment-request`, `/handoff/minty-web`, `/handoff/petty-cash-web`, `/profile`, `POST /logout`, `POST /leave-entity` | Django (Phase 6C, `hub/handoffs.py`) |
| `/xero_auth`, `/xero_connect`, `/xero_reconnect`, `/callback` | Django (Phase 6D, `hub/xero_oauth.py`, `xero/services/connect.py`) |
| `/entity/<co>/settings/*`, `/petty-cash/reports`, `/xero-not-connected`, `/Minty_Report/<path>/` and the old addresses (308) | Django (Phase 6E, `hub/redirects.py`) |
| anything else | 404 (no fall-through to Flask since Phase 7) |

## The fall-through (removed in Phase 7, 2026-11-06)

While the phases moved Flask's routes over (2026-11-05 to 11-06), `core/fallthrough.py` sat in
front of Django at the WSGI level and forwarded every path Django did not route to Flask
(`FLASK_FALLBACK_URL`), unchanged both ways. Once every path anything calls was here, it was
removed with its tests and `FLASK_FALLBACK_URL`: on development the Flask service behind it had
been suspended first and nothing failed, and the routes no caller uses (`/check/token`,
`/refresh_token`, `/minty/api/users/*`, payment-methods, bank-transactions, `xero-sync-status`,
`xero-data`, `billing-relogin`) were never ported. Flask's code stays in the Minty repo, unused.

## The Xero token service (Phase 2, 2026-11-05)

`POST /api/internal/xero/token` (`xero/views.py`, `xero/services/tokens.py`) hands a
currently-valid Xero access token to minty-petty-cash-api, minty-onboarding-api and
minty-payment-request-api. They never refresh: Xero rotates the refresh token on every use, so
two refreshers would leave a dead token and break the connection until someone reconnects.

**Who may ask:** a Minty backend with a 60-second HS256 assertion
(`minty_shared.tokens.service_assertion`, the shared `SECRET_KEY`), `scope` =
`xero-access-token`, decoded with every backend's 60 s skew. The company is the assertion's
`entity_id`, never the body. Any failure is a 401.

**The answers** are Flask's `jsonify` bytes (sorted keys, compact, a trailing newline), because
the callers' `main` reads them:

| Status | Body |
|---|---|
| 200 | `{"access_token":"…","xero_org_id":"…"}` |
| 401 | `{"message":"unauthorized","status":"error"}` |
| 409 | `{"message":"No valid Xero token for this entity","status":"reconnect_required"}` |

A 409 means a person has to reconnect, or another refresh is still running; it is never a server
error. A company with no Xero org, no `connected_by_user_id`, a missing connector, or a connector
with no `user_token` bundle all answer 409, as does an `entity_id` that is not a uuid.

**The token** belongs to the person who connected the company (`entities.connected_by_user_id`)
and lives in `user_token`. It counts as fresh while it has more than 300 s left
(`access_token_expires_in` after `access_token_obtained_at`).

**The refresh lock:**
- Postgres `pg_try_advisory_lock(1481594447, hashtext('xero_refresh:<user id>'))`, polled every
  0.1 s for up to 5 s and held across the call to Xero. That call is what spends the refresh token.
- It is Flask's exact key, so while Flask still refreshes through its own service path, the two
  wait for each other.
- Under the lock the row is read again. A fresh token is handed out, even if the wait ran out.
- If the wait ran out and the token is stale: 409, and no refresh.
- Otherwise it refreshes.

**The refresh:**
- `POST https://identity.xero.com/connect/token` with Basic `XERO_CLIENT_ID:XERO_CLIENT_SECRET`
  and `grant_type=refresh_token`; timeout 5 s connect, 10 s read.
- A refusal, an error, or a 200 with no access token: 409, and nothing is written, so the same
  refresh token is presented next time.
- Success writes each part Xero sent and stamps `access_token_obtained_at` and
  `refresh_token_last_used_at` with now.
- A failed write after Xero rotated the token is not swallowed: it is a 500, logged. Flask
  returned a 409 there.

**Not covered by the lock:** Flask's other refreshers take none (`ensure_valid_token` from
`/refresh_token`, `billing_sync.py`, `suppliers.py`, the onboarding and settings services).
They move in Phase 4.

**Locally,** the databases are production copies, so a real refresh would rotate a production
token and break that company's connection. The local check on 2026-11-05 ran with outbound
HTTPS sent to a dead proxy. Through accounts-api and through Flask, the 401 (no bearer, wrong
scope) and the 409 (no Xero org) matched Flask's bytes (the local Flask runs in debug mode and
pretty-prints; production's are compact). A stale token went through the lock, its refresh was
blocked, and it answered 409 with nothing written.

## The Xero account and contact sync (Phase 3, 2026-11-06)

`POST /api/internal/xero/sync` (`xero/views.py`, `xero/services/sync.py`) brings a company's Xero
accounts and contacts into Minty's tables. There is one sync, here, for every service (owner's
choice, 2026-11-06). Petty Cash Settings asks for it now; onboarding and Payment Requests will as
their Xero routes move (Phase 4).

**Who may ask:** a 60-second assertion scoped `xero-sync`. The company comes from its `entity_id`.
The token comes in-process from the token service above, refreshed when due.

**Answers:**

| Status | Body |
|---|---|
| 200 | `{"accounts": counts or null, "contacts": counts or null}`. Null means Xero couldn't be asked for that half, and nothing changed. |
| 409 | `{"error", "reconnect": true}`: no usable token. |
| 401 | A bad assertion. |

It asks Xero for the ACTIVE accounts **once**. Flask asked three times each time the settings opened.

**Accounts:**
- New ones are added.
- Returning ones are re-activated.
- Renamed or recoded ones are refreshed. A type Petty Cash's chart excludes (bank, equity, sales,
  revenue, other income) keeps its old name, as in Flask. The bill lines on a refreshed account, published
  or not, take its new code and name, and their bills the header code (`bill_text`, 2026-11-24).
- Ones Xero no longer has ACTIVE are deleted only when nothing uses them (`usage.used_account_ids`)
  and Minty didn't make them; otherwise they are ARCHIVED and kept, with what uses them still
  pointing at them (2026-11-11: deleting set them NULL).
- **A replaced account takes daily closing's settings with it (2026-11-13).** Xero archiving an
  account and making it again gives it a new AccountID: the old row is archived and a new one
  added. A Petty Cash setting (`entity_pettycash_settings`'s six account slots) on an archived row
  Xero no longer sends moves to the one active row of the same code and type that Xero does send.
  No code, or more than one match, leaves it where it is (logged); Petty Cash Settings then marks it
  archived. State-based, so the next sync mends a row archived before the rule. Expense and bill
  lines stay on the archived row. Before this, every publish into such an account failed ("account
  is no longer active in Xero").

**Account lists:**
- `entity_account_xero` (Petty Cash's codes: direct costs, expense, overheads, prepayment) gains a
  row per eligible account. The person's ticks are never changed here.
- `entity_bill_account_xero` (Payment Requests' codes, adding fixed) likewise, and loses rows whose
  account is gone.

**Contacts** come page by page (1,000 a page). New ones are added and renamed ones updated (their
bills, published or not, take the new name - `bill_text`, 2026-11-24). Nothing
is removed: absence from Xero's answer is ambiguous. A failed page discards the whole fetch.

**The Xero copies** (Xero optional, minty-db v0.5's EXPAND, 2026-11-10; Minty
`docs/modernisation/xero_optional_database.md`):
- Every account and contact Xero sent is upserted into `xero_account` / `xero_contact`, keyed by
  company, organisation and Xero id, with Xero's values verbatim. The code is read as the schema's
  `account_code_from_xero` reads it: none for a code longer than 10 characters, of any account type -
  the account's name in the code field (minty-db v0.17, 2026-11-23; before, bank accounts only, and a
  CURRENT account named in its code made the connect fail). `xero_code` keeps Xero's full value.
- The Minty row with that Xero id is linked to its copy (`xero_account_link_id` /
  `xero_contact_link_id`).
- An account copy Xero no longer sends is marked `DELETED`, never removed: a link or a published
  record may name it.
- A row the company made in Minty (no Xero id) is left alone by the sync.

**Disconnect keeps the organisation (2026-11-10, the design's section 4):** the shared disconnect
(`xero/services/disconnect.py`) sets `status = 'disconnected'` (the trigger makes `xero_status =
'disconnected'`) and clears only the connector and, when it was their last connected company, their
tokens. The organisation, the synced chart and contacts, the bill codes and the Petty Cash mapping
all stay, so the company works on in Minty and a reconnect to the same organisation finds them.
Everything that acts at Xero already goes by `status`. A kept organisation is no claim on it:
`connect.live_org_claimant` skips a `disconnected` claimant without touching it, a ghost claimant
(`reconcile_ghost`) is marked `disconnected` and keeps its organisation, and the hub's "move it
here" counts a `disconnected` company as already free. A connect to another organisation replaces
the copy (`mirror_on_connect`). Onboarding's own Disconnect still clears the organisation (an
`onboarding` company holding one reads as connected). Elsewhere: minty-petty-cash-api's Settings
`xero_connected` and minty-payment-request-api's token lookup refuse a `disconnected` company;
minty-web hides Disconnect once it is disconnected.

**Safety:**
- A failed or empty fetch changes nothing.
- Each half is one transaction under Flask's lock,
  `pg_advisory_xact_lock(hashtextextended('xero-sync:<entity>', 0))`. So it never overlaps Flask's
  own syncs (connect, onboarding, the nightly run) while they exist.

Tests: `xero/tests/test_sync.py` (Postgres only).

## The hub and the public routes (Phase 5, 2026-11-06; built and tested locally, not deployed)

Flask's bearer and public routes, moved with their paths, answers (Flask's `jsonify` bytes,
`core/flask_json.py`) and rules.

**The hub's door** (`hub/transport.py`):
- Any valid Minty token passes (HS256, 60 s skew, no scope check); the person must exist and be
  approved, else 401 `{"error": "unauthorized"}`.
- CORS answers minty-web, the payments app, Petty Cash, their preview sets and
  `EXTRA_FRONTEND_ORIGINS`. Refusals carry the headers too.
- A crash in an `/api/me/` route (`HubErrorMiddleware`) is logged with its traceback and answers 500
  `{"error": "Something went wrong on my end. Mind trying again?"}` with the headers. Django's bare 500
  had none, so the browser couldn't read it and minty-web said "I couldn't reach the server".

**Shared pieces:**
- `hub/policy.py`: the other services' permission rules, copied unchanged.
- `hub/registry.py`: Flask's legal manifest, copied unchanged.
- `hub/notices.py`: reads the `?flash=` and `?xero_conflict=` hand-overs, which Flask's session
  routes sign until Phase 6 (itsdangerous 2.2.0).

**5A, the public routes:**
- The sign-in page's notices.
- The live Terms and Privacy versions.
- Whether an invitee still owes a Terms acceptance. This one fails safe to "required".

**5B, the hub's own routes:**
- **The entity list.** Its trial and payment-failed badges come from minty-subscription-api's
  read-only facts (`core/subscription_client.py`). When that service can't answer within 2/4 s,
  the list loads without them.
- **My Profile.** A changed email moves the username when the username was the email; a taken
  address is 422. `user.can_view_subscriptions` (2026-11-12) tells the shared sidebar whether to
  offer Manage subscriptions. It is true for anyone with an approved membership of accountant or
  higher, who pays for any company (`entity_module_subscription.payer_user_id`), or who is a live
company's subscriber before billing is confirmed (`entities.subscriber_user_id`, minty-db v0.9,
2026-11-13). The list's rows
  and who may change them are minty-subscription-api's (`can_manage`).
- **The Terms gate.** The recorded fingerprint is the manifest's; a mismatch is 409
  `document_mismatch`; recording is idempotent, `source = hub`.
- **Talk to Minty.** It mails `CONTACT_EMAIL` from `CONTACT_FROM`, with Reply-To the customer's
  address. An unset inbox or sender is a 500, a failed send a 502, and the throttle is 5 an hour
  per person.

**5C, a company's Users and Entity & Integration tabs** (`hub/company.py`). The company always
comes from `?entity=`, never the body. Each action checks its own permission, then the rank rule:
strict since 2026-11-12 (owner): you give and manage only roles BELOW your own, except that an
admin also invites, re-roles and removes admins, themselves included (owner, 2026-11-13), never a
super_admin. The rules are `minty_shared.policy` (v0.6), one copy for every backend; `hub/policy.py`
keeps only the membership reads. The payer, pending-handover and only-admin guards still refuse
(409): the only admin can't step down or leave, and nor can the payer.
- **Users.** The members, the pending invitations, the roles this person may give, and the
  "Subscriber" tag: `entities.subscriber_user_id` (minty-db v0.9; the creator or whoever it was handed
  to, on a trial too - subscription-api keeps it on the payer once billing is confirmed). It was the
  payer until 2026-11-13, so a trial company tagged nobody.
- **Invitations** (`hub/invitations.py`, Flask's invite service).
  - The address follows the invitations' email rule and both names are needed; the role is one of
    the four assignable roles.
  - A current member, or an unexpired pending invitation for the same address, is a 409. An
    expired one is marked `expired` and replaced.
  - An invitation never changes a system role (owner, 2026-11-19). Flask's `AUTO_SUPERUSER_EMAILS`,
    which made `viewall@dailyminty.com` a superuser when invited, is gone; a superuser is set only
    in the database (`mintyliveadmin@` by the data load).
  - A resend gets a new token, so the old link dies. It waits 60 s after the last send (429 with
    `retry_after`; the count is per worker, as in Flask), and a send that fails is a 502.
  - The email is Flask's HTML, sent from `INVITE_FROM`. Its colours, and the sign-in code
    mail's, are the Minty tokens (owner, 2026-11-18): button #4FC7C7, the company
    name and the code link teal #2E9B9B, text #16202E/#6B7380/#8B93A0 - never Brand #54D3DA as a
    fill (`hub/tests/test_mail_colours.py`). Its accept link is
    `{ACCOUNTS_API_URL}/invitation/accept/<token>?fn=&ln=`, which stays Flask's page until Phase 6.
    The link is on the button and again as plain text under it ("If the button doesn't open, copy
    this link into your browser:"). Brevo's click tracking rewrites only the button's link, and its
    tracker refused every click from 2026-11-16, so the plain copy is the way in when it is down.
    The logo is `/static/img/new_logo.png`, the same file Flask serves, fingerprinted by content.
  - A failed first send keeps the invitation and answers `email_sent: false`.
- **A role change or a removal** passes Flask's guards, in Flask's order:
  - the payer can't be removed, or demoted out of admin;
  - a company never loses its only admin (deactivated accounts don't count);
  - nobody being offered the subscription handover can be changed.
  
  The payer guards **fail closed**: if minty-subscription-api can't answer, nothing changes (503).
  The handover guard **fails open**, as Flask documents it. A change that stays at or below admin
  never asks.
- **Entity & Integration.**
  - The name, country and currency, with the active countries and currencies to pick from.
  - The Xero state, checked live against Xero's `/connections` with the connector's token from this
    service's own token service. The organisation name is filled in when missing.
  - A save renames the company (admins only; names need not be unique, owner 2026-11-16; 422 sentences), and sets
    the country and currency. A country sent on its own brings its own currency, and the company's
    notes and coins follow the currency (`hub/cash_settings.py`, onboarding-api's copy).
  - **The currency is locked while the company is connected to Xero** (the user, 2026-11-24;
    `currency_locked` on the page: an organisation held and status not `disconnected`, so a wizard
    company that linked Xero is locked and one mid-matching or kept after a disconnect is not). A
    different currency is a 409 (`CURRENCY_LOCKED`: "…so its currency stays the same as Xero's. To
    change it, disconnect Xero first."), and a country no longer brings its own currency.
  - A failed save is a 500 and changes nothing.
  - `?xero_conflict=` names the company holding the organisation; "move it here" is offered only in
    development.

**Mail and settings:**
- Mail goes through `SMTP_URL`, which is required outside development.
- `INVITE_FROM` is required.

**5D, Xero disconnect and the onboarding routes:**
- **The one disconnect** (`xero/services/disconnect.py`, Flask's):
  1. It revokes the company's grant at Xero, as a best effort. The connector's token comes from
     this service's own token service.
  2. It clears the connector's tokens when this was their last connected company.
  3. It drops the company's Xero cache: the account and contact mappings, the organisation's
     contacts, `account_info` and `entity_bill_account_xero`.
  4. It marks the company `disconnected`.

  Steps 2-4 are one transaction. A failure is a 502 that says the company may still show as
  connected.
- **The hub's Disconnect** answers the fresh Entity & Integration page. Its **release** frees the
  company holding the organisation. That release is authorized on the company being freed, refused
  for the company itself, and available in development only.
- **The onboarding routes** (`hub/onboarding.py`). minty-onboarding-api forwards its caller's token
  to them, and minty-subscription-api's "invite an admin" forwards to `/invite`. Both reach them
  through `ACCOUNTS_API_URL`, so they need no change.
  - Flask's onboarding door applies: any valid token, 401 `{"error": "Unauthorized"}`, and CORS for
    the onboarding app with `GET, POST, PUT, OPTIONS`.
  - The answers use Flask's words.
  - **Modules:** the chosen modules on and the rest off, recording who chose. Only an **approved**
    member may choose. Flask took any membership row; this is tightened on purpose.
  - **Invitations:** list, send, resend and cancel.
  - **Disconnect:** onboarding's own. The company stays `onboarding`, and its synced lists are kept
    (owner, 2026-11-06).
  - **Release:** the shared disconnect of another company, in development only.

Nothing in Phase 5 is left to move. The sign-in codes and their mail, the invitation's accept page
and every session route are Phase 6.

## The browser session (Phase 6, 2026-11-06; built locally, not deployed)

Everything that used Flask's session cookie, moved in one release (everyone signs in again once).

- **The session** is Django's (`django.contrib.sessions`, table `django_session`, created by hand
  with `manage.py migrate sessions` on each database - the schema file's note B6; the Postgres test
  database gets the same step in the root `conftest.py`). Cookie `session` (Flask's name, so the old
  cookie is replaced), 60 minutes without activity (Phase 9; it was 24 h), HttpOnly, SameSite=Lax, Secure outside development. There is no
  `django.contrib.auth`: `hub/session.py` keeps the person's id and the sign-in's id (`login_sid`,
  every hand-off token's `sid`, from which the key is derived); `sign_in` gives a new key (no fixation) and stamps presence, as
  Flask-Login's signal did. Flashed messages wait in the session until a redirect signs them into
  `?flash=` (`hub/notices.sign_notices`, the same itsdangerous format Flask writes).
- **Headers** (`core/hardening.py`): Flask's `Referrer-Policy`, `nosniff`, `X-Frame-Options`, HSTS
  outside development; `/invitation/accept/<token>` is `no-store` and logged as `[redacted]`.
- **Preview sets** (`hub/links.py`): `?return_origin=` and the stored set, Flask's rules;
  `hub_login_url` builds minty-web's `/login` / `/signup` addresses.
- **Company addresses** (`hub/addresses.py`): the `<entity:...>` converter (`<shortid>/<name>` or a
  full uuid); a GET that named a company any other way is 308'd to the canonical address.
- **The Terms gate** (`hub/terms_gate.py`): a cookie-signed-in person who owes the live Terms is
  sent to `/entity` (minty-web draws the panel), JSON gets 403 `terms_acceptance_required`; the way
  in and out and the acceptance flow are open; cached per session per version; fails open, loudly.

**Signing in by code** (`hub/email_signin.py`, Flask's rules): 60 s codes, 60 s resend cooldown,
5 tries then a 15-minute lock carried across resends, login mode refuses an unknown address, a
failed send is rolled back. Codes keep werkzeug's `pbkdf2:sha256` format, so a code either side
issued verifies on the other. Sign-up makes the account and its Terms agreement in one transaction.

**Gaps fixed in the move (owner, 2026-11-06):**
1. **The hand-off is single use.** Verify-code parks it as a pending `django_session` row
   (5 minutes); the URL carries its key, signed. `/auth/email/handoff` deletes the row and signs in
   only if the delete removed it, so a replay, a copied link or a second tab signs nobody in.
2. **Xero OAuth `state` carries a per-session nonce** (6D, `/xero_auth`, `/xero_connect`,
   `/xero_reconnect`, `/callback`).
3. **`/logout` and `/leave-entity` are POST only** (GET is 405), and the POST must come from one of
   the front ends (`Origin`; the cookie is SameSite=Lax). The apps sign out with a form post.
4. **No token in an address (owner, 2026-11-09).** Each hand-off into an app carries a one-time
   code (`hub/parked.py`, gap 1's parking, now shared: single use, 2 minutes, bound to the app's
   origin) that the app's landing swaps at `POST /api/handoff/redeem`; `/enter` works on the
   session alone. authentication.md section 6 has the detail.

**Xero OAuth** (6D, `hub/xero_oauth.py`, `xero/services/connect.py`): sign in with Xero, and
connect or reconnect a company, with Flask's scopes, state shapes, callback branches,
one-organisation-one-company checks, grant revokes, financial year end and connect-time mirror. The
post-connect sync reuses `xero/services/sync.py`, on a thread. The callback answers a nonce it did
not issue before exchanging the code. Two more changes from Flask:
- **Connecting needs `XERO_SETTINGS_UPDATE` on the company.** Flask checked only `ENTITY_CREATE`
  (everyone) and wrote the organisation onto whatever company id the state named, overwriting its
  connection and pruning its accounts. Now `/xero_connect` refuses a named company the person may
  not connect, and the callback checks the Xero login's owner and revokes the new grant if they
  fail. Flask's last fallback, "the newest company of anyone", is gone: with nothing named, nothing
  is connected.
- A refused code exchange is a sentence, not a 500, and the token write on a connect is in the same
  transaction as the company's.
- A company name holding `:` keeps its name and the wrong-login check: the callback reads the
  initiator from the right of the state (Flask split from the left and skipped the check).
- A Xero login no Minty account holds is told so, and its grant is handed back at Xero (Flask said
  nothing and left it live). The onboarding wizard sees `xero=cancelled`, as for other failures.
- `/callback` is open past the Terms gate. Its start routes stay gated, so only a flow begun while
  agreed lands, and a Terms change mid-connect no longer throws away what Xero granted.
- **The company's currency must be the organisation's base currency** (the user, 2026-11-24).
  `connect.organisation_facts` reads the year end, `BaseCurrency` and name from the one
  `/Organisation` call; no base currency fails the connect like no year end. `currency_refusal`
  then refuses (grant handed back, nothing written) with what to change - Minty never switches the
  currency, and Xero can't change a base currency: "{org} uses USD in Xero, but {company} uses HKD
  in Minty, so it wasn't connected. Change the company's currency to USD in Settings, under General,
  or connect a Xero organisation that uses HKD." A company with no currency is told to choose one.
  Connect and reconnect flash the sentence as category `dialog` (`hub/notices.py` keeps it), which
  minty-web draws as a one-button modal (`NoticeDialog`) instead of a 4 s toast; the wizard gets `xero=currency&xero_currency=USD&
  expected_currency=HKD&org=…` (no currency there is `xero=cancelled`: the wizard asks for one at
  step 1).

Like Flask, the id_token's signature is not checked against Xero's keys: the token comes straight
from Xero's token endpoint over TLS, in exchange for the code and the client secret.

**Hand-offs** (`hub/handoffs.py`): the module token (30 min, `module: "billing"`) and the onboarding
token (60 min, `scope: "onboarding"`) with Flask's claims exactly; `/enter` turns only a module
token for a member into a session. **Redirects** (`hub/redirects.py`): the settings tabs, report
history, share links and the old addresses, with Flask's guards.

**Not moved** (no caller anywhere, checked 2026-11-06): `/check/token`, `/refresh_token`,
`/minty/api/users/*`, `/api/entities/<id>/payment-methods*`, `/api/xero/bank-transactions/*`,
`/api/entity/<id>/xero-sync-status`, `/xero-data`, `/entity/<co>/billing-relogin`. They fall
through until Phase 7 removes the fall-through.

## The whole stack

`docker/stack/`, `scripts/e2e_seed.py` and the stack e2e job moved here from Minty in Phase 7
(2026-11-06): `stack-e2e.md`.

## Tests

`tests/test_settings.py` covers the refusal to start without each required value;
`tests/test_e2e_seed.py` the stack's seed.

`xero/tests/test_internal_token.py` (Postgres only) has three groups:
- Flask's five characterisation tests, ported;
- the exact bytes, the company read from the claims, the 409 cases, POST only, and the route
  owning its path;
- the lock, held by a second session as Flask would hold it: a stale token is not refreshed, a
  fresh one is still handed out, and the lock is released afterwards.

`hub/tests/` restates Flask's hub tests for the moved routes (public routes and the door, the
entity list, My Profile, Terms, Talk to Minty, and the company tabs with their membership guards and
invitations). minty-subscription-api is faked at `core.subscription_client`; a root fixture blocks
any real call to it. Mail goes to Django's in-memory outbox.

## Archived companies (owner, 2026-11-10)

`entities.archived_at` (with `archived_by_user_id`) archives a company. It is a column, not a
`status` value, because the Xero code rewrites `status` (connect, disconnect, the settings page's
sync) and the Xero-optional CONTRACT rebuilds it as `('onboarding','live')`.

- **The list.** `GET /api/me/entities` leaves archived companies out for everyone but a superuser.
  A superuser gets them all, each with `archived_at`, and can narrow the list with
  `?archived=all|active|archived` (default `all`; anything else is a 422). Every answer says
  `is_superuser`, so minty-web knows when to offer the filter.
- **The doors.** An archived company is "no such company" everywhere, superusers included:
  `entity_context` (every `/api/me/company/*` tab and the Petty Cash hand-off), `handoffs._entity`
  (the module chooser, Payment Requests, minty-web and the address redirects), `redirects._gate`,
  invitations (none made; an open one can't be accepted and stays pending), onboarding's Xero
  routes, the Xero connect's company lookup, and `resolve_entity_access_token` (no Xero token for
  the other APIs).
- **Archive.** `POST /api/me/entities/<id>/archive`, **superusers only** (owner, 2026-11-10: a
  company's admins can't archive it; 403 otherwise; 404 for no such company). Archiving an
  archived company keeps its first stamp. It is refused (409, "This company still has a paid
  subscription. Cancel its subscription first, then archive it.") while minty-subscription-api's `entity_facts` lists any `paid_modules` (active or
  past due). A trial, or a cancellation running out its paid time, doesn't block. No answer, or an
  answer without `paid_modules`, archives nothing (503).
- **Restore.** `POST /api/me/entities/<id>/restore`, superusers only (403 otherwise; 404 for no
  such company). It clears both columns.
- The other APIs refuse archived companies at their own doors; the v2→v3 load carries the old
  app's `status = 'deleted'` as `archived_at`.

## Matching a Minty company's accounts to Xero (Settings Phase 2, 2026-11-11)

The design is Minty's `docs/modernisation/xero_optional_database.md` §4 (decisions 5/5a/6/9/12/13/18);
Figma 353:999's SET-1b and frames 3, 3b, 4, 5 are the look. Before this, a connect's mirror deleted every
account with no Xero id (a Minty company's whole chart, the Petty Cash/Bank/Director rows included) and
every supplier with no organisation, leaving expenses, daily-closing accounts and sales methods
pointing at nothing.

- **Connect** (`hub/xero_oauth.py` `_record`): a company with `setup_mode = 'minty'` and its own accounts
  is not mirrored. `matching.begin` fills only the Xero copies (`xero_account` / `xero_contact`) and the
  automatic picks (same code AND type, `account_match_draft.picked_by` NULL), then Minty's guesses
  (below); the organisation is
  recorded, the company stays `disconnected` and `minty`, and the person lands on the hub's
  `/settings/integration/match` (accounts-api's `settings_integration_match` redirect). No background
  sync yet; `_sync_connection` never marks such a company connected (`matching.pending`).
- **The page** (`hub/xero_matching.py` over `xero/services/matching.py`, `XERO_SETTINGS_VIEW` to read,
  `XERO_SETTINGS_UPDATE` otherwise): `GET /api/me/company/xero/matching`. The answer:
  - the accounts still to match first (group `unmatched`, "Not matched yet", in the groups' order, then
    by code), then the matched ones in their groups;
  - each row with its group, its pick (`guess: true` when Minty filled it in by name), a suggestion for a
    row still empty (a bank by its number, else a fitting account with a similar name, never one another
    account has), a same-code other-type conflict, and whether a record uses it;
  - `guessed`: how many picks are still Minty's guesses (they count as matched and never hold Finish).

  **Minty's guesses (the user, 2026-11-25)** - matching each account by hand took too long:
  `matching.auto_fill` starts every row still empty on its closest Xero account when a matching opens
  (`begin`, `start_change`; never on Refresh, so a row the person emptied stays empty). Pairs are ranked
  over the whole chart - a bank's own account number, then `name_score` (the better of the letters and
  the words: filler words such as "expense"/"and" left out, plurals cut, synonyms as one word -
  wages/salaries/payroll, repairs/maintenance, advertising/marketing, telephone/phone,
  delivery/transport, rent/rental, ...), then the same type over the same class - and taken best first,
  each Xero account once; only fitting accounts (a bank only to a bank, else the same class). An
  account nothing in Xero fits stays empty. A guess is `picked_by` NULL with neither the account's own
  link nor the same code and type; the person's pick (choosing an account, or Keep) replaces it.
  - the Xero accounts to pick from, each with `held_by` (the account that has it);
  - `blocking` (accounts with no pick) and `contacts` (the contacts step, below).

  The other routes:
  - `PATCH` `{account_id, xero_account_id | null}`: one pick, saved at once. A bank only to a bank, else
    the same class. Daily closing's petty cash and bank are never one Xero account (`matching.SAME_BANK`).
    A Xero account another account picked is refused (409 `matching.TAKEN_PICK`).
  - `PATCH .../contacts` `{contact_id | slot, xero_contact_id | null, new}`: one answer of the contacts step.
  - `POST .../refresh`: Xero's accounts and contacts again. `{"throttle": true}` (the page opening) skips
    Xero when the chart was read under `SETTINGS_READ_EVERY`; the GET adds `can_edit`.
  - `POST .../finish` and `POST .../contacts`.
- **Every account needs its own Xero account** (the user, 2026-11-24):
  - Used or not, every account needs a match, so nothing is archived at Finish.
  - Accounts are never merged, so two can't share a Xero account (`account_match_draft`'s
    `uq_account_match_draft_xero`, minty-db v0.18).
  - An account with no counterpart is added in Xero, then Refresh (Minty never creates accounts there, A1).
- **Used** (`xero/services/usage.py`, owner 2026-11-11, extending the doc's Q8): an expense line, a bill
  line (by id, or by code on an older line), any daily-closing account, or an account combined into it.
  (A sales method named an account for a day, minty-db's unreleased v0.6; withdrawn the same day.)
- **Finish = the replace**, one transaction.
  - **Refused (409)** while any account has no pick (`UNMATCHED`), two share a Xero account (`SHARED`),
    a pick names a Xero account or contact that is gone, or one already linked here, or the contacts
    step has an answer missing (`CONTACTS_UNMATCHED`).
  - **Each picked account takes Xero's link, Xero code, name and type IN PLACE**, so every reference stays.
    Its Minty code (`code`) stays beside the Xero code (`xero_code`; the user, 2026-11-24 - Minty Code +
    Xero Code, minty-db v0.18 keeps the trigger from overwriting it).
  - **Its bill codes and bill lines follow.** It gets a Petty Cash module row if it had none (X2/X3's
    tick). Its bill code and every bill line on it, published ones included, follow: a line saved with
    only the old code gets the id, each line takes the account's code and name, and its bill the header
    code (`bill_text`, the user's rule 2026-11-24; nothing goes to Xero).
  - **Logged and closed.** Everything is logged in `account_change_log` (one batch; an unchanged match
    writes nothing). The drafts are deleted, `setup_mode = 'xero'`, and `xero_publish_from` = today (Hong
    Kong; the day Finish ran, no longer a publish cut-off: owner 2026-11-18, any record publishes once
    Xero is connected).
  - **The contacts step's answers go in with it, and the company is connected** at once, with the
    first sync started.
- **The contacts step** (the user, 2026-11-24: a step before Finish, replacing the warning after it;
  `contact_match_draft`, minty-db v0.18). `contacts` in the page holds:
  - each supplier not yet in the organisation (`own_suppliers`): its Xero contact, picked for it when
    one has its name (case, spaces and punctuation ignored - `minty_shared.names`), or `new`, meaning
    Minty puts it in Xero at its first publish (C1);
  - each daily-closing contact still empty (`slots`: `cash_sale`, `director`, `discrepancy` - no
    `cash_sale` when every sale is banked, none without a settings row; Xero's books need them,
    petty-cash-api `xero_settings.PETTYCASH_SLOTS`), which always takes a Xero contact;
  - the Xero contacts to pick from (active copies), each with `held_by`: the Minty contact linked to it,
    or the supplier whose pick it is. **Matching never merges** (the user, 2026-11-25):
    - a held one is never picked for a supplier: `auto_pick_contacts` skips it and `PATCH` refuses it
      (409 `merge.CONTACT_TAKEN`, "... Merge the two in Settings first, or choose another contact.");
    - a daily-closing slot may take it (that is no merge);
  - and `blocking`.

  At Finish:
  - A supplier is linked (`contact_change_log`). A Xero contact another Minty contact already has
    refuses Finish (`CONTACT_TAKEN`; one held by a deleted contact, `merge.TAKEN_BY_ARCHIVED`), nothing
    written - the person merges the two contacts first.
  - A slot is set to its Xero contact's row, made from the copy when there is none.
  - **Connecting creates nothing in Xero** (owner's decisions doc C2, 2026-11-19).
  - A change of the matches asks no contacts (`required: false`).
  - `POST .../contacts` (Try again) is left for a company the old rules (before 2026-11-19) still hold
    disconnected: it links by name and connects it. A name whose Xero contact another Minty contact has
    is left unlinked, never merged.
- **Daily closing's contacts** (production, 2026-11-23): a Minty company's daily closing had none, and
  Publish refused after Finish. They are the contacts step's now. Finish's answer still carries
  `pettycash_contacts_missing` (`[]` after the step).
- **The sync never deletes what is used or Minty-made** (also 2026-11-11): `sync.apply_accounts`
  archives a used (or Minty-made) account Xero dropped and leaves rows with no Xero id alone;
  `_mirror_bills` keeps bill codes with no Xero id; the connect's contact clean-up and
  `mirror_on_connect` only remove unused, Xero-made leftovers.
- **Phase 3 (owner, 2026-11-12; `xero/services/matching.py`):** `entities.xero_matching` (minty-db v0.6)
  marks an open matching and why - `first` (above), `new_accounts`, `new_org`, `change` - and every
  pending check reads it. A connect decides (`needs_matching(entity, tenant)`): another organisation
  than the one recorded → `new_org`, matched from scratch (copies + automatic picks only; nothing
  mirrored, archived or deleted before Finish; this used to mirror the new chart over the old one);
  the same organisation with an account not linked yet (made in Minty meanwhile) → `new_accounts`. The
  scope (`scope_accounts`) is every active account (the user, 2026-11-24: a reconnect shows them all;
  the ones linked to the current organisation prefilled - `_prefill_links`); `first` is the company's
  own accounts. `new_org` also covers every archived account (in Xero or in Minty) that a record uses
  (`usage.used_account_ids`; the user, 2026-11-25: the chart is the current organisation's), so Finish
  moves it to the new organisation, active again as A3's map does, and no report, bill or daily-closing
  account is left on the old organisation's. An archived account nothing uses stays out; if Minty made it, Finish lets go of its old Xero account
  and Xero code (`_let_go_of_old_links`, `account_change_log` `unlinked`; its Minty code stays), so one
  brought back later is Minty's own, to be matched. Unused Xero-made rows are the sync's to delete. **Change matches**: `POST .../matching/change` (connected, `XERO_SETTINGS_UPDATE`)
  prefills a pick per account; `POST .../matching/cancel` leaves it, nothing changed; the company stays
  connected and publishing goes on. Finish relinks only what changed (two accounts may swap: the
  changed ones let go of their Xero id first, as `uq_account_entity_xero` isn't deferrable), clears
  the marker, resets `xero_publish_from` to today for `first`/`new_org` and keeps it for
  `new_accounts`/`change` (a record of when Finish ran, no longer a publish cut-off);
  a change asks no contacts (nothing created in Xero). Suppliers of another organisation are the
  contacts step's after a new organisation's connect, archived ones too while a record uses them
  (`usage.used_contact_ids`; the user, 2026-11-25) - linked to the new organisation, still archived in
  Minty. One answered "New in Xero" lets go of the old organisation's contact at Finish
  (`contact_change_log` `unlinked`), so its first publish finds or makes it in the new one (C1). While a `first`, `new_accounts` or `new_org`
  matching is open the sync refreshes only the copies. **While the matches are being changed** (the
  user, 2026-11-24; production: a change left open from 19 Nov kept 58 Xero accounts out of a company
  for days) the sync takes Xero's new accounts in as on any sync (`matching.sync_during_change`, under
  the company row's lock like Finish): one the change already gives to an account is not added beside
  it (`apply_accounts(skip=...)`; Finish links it), and each new one starts matched to itself
  (`_prefill_links`), so it never blocks Finish. **Every Finish starts the background sync**, a
  change's too, so Xero's accounts arrive at once. Tests: `hub/tests/test_xero_matching.py`,
  `test_xero_oauth.py` (`test_a_minty_companys_connect_keeps_every_account_and_goes_to_matching`),
  `xero/tests/test_sync.py` (the archive-not-delete cases); Postgres only.
- **While matching** (`setup_mode` still `minty`, or a `new_accounts`/`new_org` matching), `sync.sync_company` - the sync every app asks for -
  refreshes only the Xero copies: importing Xero's chart as accounts put rows beside the company's own
  holding the Xero ids Finish links. Finish removes an unused Xero-made row that holds a picked Xero
  account (one a sync made before this) instead of refusing; a used one still refuses.

## Settings' accounts and contacts (owner, 2026-11-12)

`/api/me/company/accounts[/<id>]` (GET, POST / PATCH, DELETE) and `/api/me/company/contacts[/<id>]`
(`hub/company_chart.py`): the hub's door to minty-onboarding-api, which holds the ONE implementation
(the owner's choice that day - its account rules and side effects, the Xero writes, who owns what;
its `/api/internal/chart/*`, `docs/features/wizard-api.md`). Here: the person's token, the company
from `?entity=`, the right (`COA_VIEW` to read - cashier and up; `COA_CREATE` / `COA_UPDATE` /
`COA_DELETE` to change - accountant and up, contacts included), then the call with a short-lived
service assertion (scope `company-chart`) naming the company and the person - never the body's.
onboarding-api's answer and status come back as they are. Unreachable, no JSON, our assertion
refused, or `ONBOARDING_API_URL` unset (hosted: no default) -> 503 with one sentence, logged.
Tests: `hub/tests/test_company_chart.py`.

**No duplicates from the sync (owner, 2026-11-12; `xero/services/sync.py`).** A Xero account Minty
doesn't hold yet takes the place of the company's own unlinked account of the same code and type
(`linked` in the counts); a Xero contact, of the company's own unlinked contact of the same name
(case, spaces and punctuation ignored - `minty_shared.names`, 2026-11-23; a name with no letters or digits matches nothing) - never a second row beside it. **The sync only reads** (owner's
decisions doc C2, 2026-11-19): nothing goes to Xero from any sync, the connect's included - a contact
made in Minty reaches Xero when it is added while connected (C5) or at its first publish (C1).
Tests: `xero/tests/test_sync.py`.

**Opening Settings reads at most once every 2 minutes (C2).** `POST /api/internal/xero/sync` with the
body `{"throttle": true}` (Petty Cash Settings' `refresh_from_xero`) skips the read when the company's
contacts were read under 2 minutes ago (`sync.read_recently`: the newest `xero_contact.synced_at`,
which every sync stamps) - 200 `{"accounts": null, "contacts": null, "skipped": true}`. Other callers
(onboarding's saves, Payment Requests) send no body and always read. The General tab's contact list
does the same before forwarding: `hub/company_chart.py` reads a connected company's contacts first,
throttled the same way; Xero failing leaves the stored list, logged. Tests: `xero/tests/test_sync.py`,
`hub/tests/test_company_chart.py`.

## Merging contacts, relinking and mapping one account (the owner's decisions doc, 2026-11-19)

The doc "Minty Xero Sync & Report Decisions" (claude.ai artifact HV37p5LpnzconZBJEUD31n) sets these.
**Accounts are never merged (the user, 2026-11-24):** each matches its own Xero account; the account
Merge (route, `merge_accounts`, `combine_accounts`) is gone. Accounts merged before then keep
`merged_into_id` and their merging-table rows, and every reader still follows them.

**Matching and merging are separate (the user, 2026-11-25):** matching never merges (above: the contacts
step, Try again, "map it now"); merging is the Settings Merge, or Minty following a merge done in Xero.

- **The one merge (M1-M4), `xero/services/merge.py`.** `combine_contacts` moves every record of the
  merged contact to the kept one (expenses, bills, daily closing's slots; a kept contact's bills take
  its name - `xero/services/bill_text.py`, the user's rule 2026-11-24), archives it with
  `merged_into_id` and writes `minty_merge` plus a `minty_merge_record` per moved row (minty-db v0.14)
  besides `contact_change_log`. No new record, Xero not touched, no undo.
- **Settings Merge (contacts only; the user, 2026-11-25)**: `POST /api/me/company/contacts/merge {ids, keep}`
  (`hub/company_chart.py`, handled here, not forwarded; `COA_DELETE`) -> `merge_contacts`.
  - Several ticked contacts are absorbed into the one kept: always one of them, never a new contact.
  - Refused with 400: fewer than two (`TICK_TWO`), or `keep` not among them (`KEEP_TICKED`).
  - Refused with 404: one that isn't the company's active, unmerged contact.
  - While Xero is connected (`status = 'connected'`), a ticked contact linked to the organisation is
    merged in Xero first (409 `MERGE_IN_XERO`, "In Xero already: ... merge them in Xero first, and Minty
    follows at the next sync."). Contacts only in Minty still merge here.
  - While Xero is off (disconnected, or never connected), contacts linked to Xero merge here too (the
    user's pick, 2026-11-25). Xero keeps them as separate contacts, and minty-web says so. The merged
    one stays archived with its Xero link. A later pick of that Xero contact ("map it now", matching) is
    refused as merged, not deleted (`TAKEN_BY_MERGED` via `merge.taken_by_archived`).
  - Each absorbed contact is one `minty_merge` row (reason `user`), one batch. The answer is 200
    `{message: "A and B merged into C. C stays.", kept, moved}`.
  - The pairwise `POST .../contacts/<id>/merge` and `POST .../accounts/<id>/merge` are 404.
- **Minty follows a merge done in Xero at the next sync** (`sync.follow_xero_merges`):
  - Xero's contact list leaves out archived contacts, and only `GET /Contacts/{id}` names
    `MergedToContactID`. So each linked, active contact missing from the list is asked for by id,
    at most `FOLLOW_MERGES_PER_RUN` (25) a run.
  - One Xero merged is relinked to Xero's kept contact, or merged into the Minty contact that has it
    (`relink_contact`, `merged_in_xero`).
  - This runs before the list's new contacts are added, so the kept contact is never a second row
    first.
  - One archived with no merge is left for publish (C4). The sync's contact counts carry
    `followed_merges`.
- **The sync keeps merged rows**: `apply_accounts` never touches a row with `merged_into_id` (it used to
  delete a merged-away Xero account Xero dropped, and its log with it).
- **Every overwrite is logged** (the user, 2026-11-24):
  - `apply_accounts` writes `account_change_log` `overwritten` (Xero's code, name and type before and
    after; `changed_by` NULL) when a rename or recode reaches an account.
  - It writes `linked` when a Minty account Xero takes over by code and type.
  - The Minty code stays.
  - Names are cut to 80 (a longer Xero name failed the whole sync).
  - Types Petty Cash's chart excludes (BANK, EQUITY, OTHERINCOME, SALES, REVENUE) and an empty Xero
    code are still not refreshed.
- **A contact archived in Xero (C4)**: `POST /api/internal/xero/contacts/<id>/relink {xero_contact_id,
  reason}` (assertion scope `xero-contact-relink`; `merge.relink_contact`) - Petty Cash's and Payment
  Requests' publish follow Xero's merge (`merged_in_xero`, no prompt) and their "map it now" (`user`).
  The Xero contact already a different Minty contact: following Xero's merge, the two are merged (the
  user's rule); a person's "map it now" is refused, 409 `CONTACT_TAKEN` (matching never merges, the user
  2026-11-25). A contact the company deleted holding it: 409.
- **One account mapped again (A3)**: `GET /api/me/company/xero/matching/account?account=<id>` (the row and
  the Xero accounts of its kind) and `POST {account_id, xero_account_id}` (`XERO_SETTINGS_UPDATE`;
  `matching.read_one` / `map_one`, through `_link`: Xero's code, name and type, the Minty code kept; no
  records move, nothing in the merging table). Another active account holding that Xero account: 409
  `{error}` - choose a different one (`matching.TAKEN`). `matching.GONE_FROM_XERO` is the doc's sentence.
- Tests: `hub/tests/test_company_merge.py`, `hub/tests/test_xero_matching.py` (A3), `xero/tests/test_sync.py`.
