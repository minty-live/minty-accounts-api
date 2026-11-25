"""Matching a company's own accounts to Xero when a company that kept its books in Minty connects
Xero (Settings Phase 2, 2026-11-11; Minty ``docs/modernisation/xero_optional_database.md`` §4 -
decisions 5/5a/6/9/12/13/18; Figma 353:999 frames SET-1b, 3, 3b, 4, 5).

Before this, the connect's mirror deleted every account with no Xero id - all of a Minty company's
own accounts - and every supplier with no organisation, leaving its expenses, daily-closing
accounts and sales methods pointing at nothing.

**Begin** (``begin``, in the connect's transaction): the organisation is recorded, the company stays
``disconnected`` and ``setup_mode`` ``minty``; only the Xero copies (``xero_account`` /
``xero_contact``) are filled, and each own account whose code AND type equal a Xero account's is
picked automatically (``account_match_draft``, ``picked_by`` NULL). Pending while setup_mode is
``minty``, an organisation is set and own accounts remain (``accounts_pending``).

**Picks** (``pick``) save at once, so matching can be finished later. **Every account needs its own
Xero account** (the user, 2026-11-24): used or not - nothing is archived any more - and accounts are
never merged, so a Xero account another account has picked is refused (``TAKEN_PICK``; the database's
``uq_account_match_draft_xero``). An account with no counterpart in Xero is added there, then
Refresh. **Finish** (``finish``) is the replace, one transaction: refused while any account has no
pick; each picked account takes Xero's link, Xero code, name and type IN PLACE, so every expense,
daily-closing account and sales method keeps pointing at it - its Minty code (``code``) stays beside
the Xero code (``xero_code``; minty-db v0.18); the bill codes and bill lines follow the Xero code;
every change is logged (``account_change_log``, one batch); ``setup_mode`` becomes ``xero`` and
``xero_publish_from`` today (the day Finish ran; no longer a publish cut-off - owner, 2026-11-18).

**Contacts** (the user, 2026-11-24: a step before Finish, not a warning after it). Each supplier not
yet in the organisation takes a Xero contact (the one of its name is picked for it -
``minty_shared.names``) or stays new: Minty puts it in Xero at its first publish (C1; connecting
creates nothing in Xero, C2). Each daily-closing contact still empty takes a Xero contact (Xero's
books need all three; ``pettycash_contacts_missing``). Picks save at once (``contact_match_draft``),
Finish is refused while one has no answer, and Finish applies them in its transaction: linked
(``contact_change_log``) and the daily-closing slots set. Matching never merges (the user,
2026-11-25): a Xero contact another Minty contact already has is shown held and refused
(``merge.CONTACT_TAKEN``) - the person merges the two contacts first.

**Phase 3 (owner, 2026-11-12).** ``entities.xero_matching`` (minty-db v0.6) marks an open matching
and why: ``first`` (the above), ``new_accounts`` (a reconnect to the same organisation found accounts
made in Minty meanwhile), ``new_org`` (a reconnect to another organisation: every account matched
again from scratch, nothing mirrored, archived or deleted before Finish) or ``change`` (the matches
changed on the same page, prefilled; the company stays connected). The scope is every active account
(for ``first``, Phase 2's own accounts); the ones already linked to the current organisation come
prefilled, and the page lists the ones still to match first (the user, 2026-11-24). Finish relinks
only what changed, may swap two accounts' Xero accounts, clears the marker,
and resets ``xero_publish_from`` to today for ``first``/``new_org`` but keeps it for
``new_accounts``/``change``.
While a matching is open the sync refreshes only the Xero copies (``sync.sync_company``).
"""

from __future__ import annotations

import difflib
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.db.models import Max, Q
from django.utils import timezone
from minty_db.models import (
    AccountChangeLog,
    AccountInfo,
    AccountMatchDraft,
    AccountModule,
    BillLine,
    ContactChangeLog,
    ContactMatchDraft,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    XeroAccount,
    XeroContact,
    XeroContactSync,
)
from minty_shared import names

from xero.services import bill_text, sync, usage
from xero.services.connect import MirrorFailed

logger = logging.getLogger("minty-accounts-api")

HK = ZoneInfo("Asia/Hong_Kong")

#: Xero's account type -> Minty's (the schema's ``account_type_from_xero``, for the same rows).
TYPE_FROM_XERO = {
    "BANK": "bank", "CURRENT": "current", "FIXED": "fixed", "INVENTORY": "inventory",
    "NONCURRENT": "noncurrent", "PREPAYMENT": "prepayment", "CURRLIAB": "current_liability",
    "LIABILITY": "liability", "TERMLIAB": "term_liability", "EQUITY": "equity", "SALES": "sales",
    "REVENUE": "revenue", "OTHERINCOME": "other_income", "DIRECTCOSTS": "direct_costs",
    "EXPENSE": "expense", "OVERHEADS": "overheads", "DEPRECIATN": "depreciation",
    "WAGESEXPENSE": "expense", "SUPERANNUATIONEXPENSE": "expense", "PAYGLIABILITY": "current_liability",
    "SUPERANNUATIONLIABILITY": "current_liability",
}
CLASS_OF = {
    **dict.fromkeys(("bank", "current", "fixed", "inventory", "noncurrent", "prepayment"), "asset"),
    **dict.fromkeys(("current_liability", "liability", "term_liability"), "liability"),
    "equity": "equity",
    **dict.fromkeys(("sales", "revenue", "other_income"), "revenue"),
    **dict.fromkeys(("direct_costs", "expense", "overheads", "depreciation"), "expense"),
}
#: The matching page's groups (frame 3), in order.
GROUPS = (
    ("cash_bank", "Cash & bank"),
    ("sales", "Sales"),
    ("cost_of_sales", "Cost of what you sell"),
    ("running", "Running the business"),
    ("owed", "Owed to the director"),
    ("other", "Other"),
)

UNMATCHED = (
    "Accounts still to match: {n}. Choose a Xero account for each one, or add the missing ones in Xero and "
    "refresh."
)
TAKEN_PICK = "{other} already has that Xero account. Choose a different one."
SHARED = "Two accounts can't share one Xero account. Choose a different one for each."
CONTACTS_UNMATCHED = "Contacts still to choose: {n}. Choose a Xero contact for each one to finish."
CONTACT_NOT_OWN = "That contact isn't one to choose here. Reload the page and try again."
NOT_A_CONTACT = "Choose one of your Xero contacts."
SLOT_NEEDS_XERO = "Daily closing needs a contact that's in Xero. Choose one from the list."
CONTACT_GONE = "Some of the Xero contacts you picked are no longer in Xero. Refresh from Xero and pick again."
PICK_GONE = "Some of the Xero accounts you picked are no longer in Xero. Refresh from Xero and pick again."
NOT_PENDING = "This company has nothing to match with Xero."
NOT_OWN = "That account isn't one to match. Reload the page and try again."
WRONG_KIND = "That Xero account can't be used for this one. Choose one from the list."
NO_TOKEN = "Xero needs to be connected again. Sign in to Xero once more to carry on."
NOT_CONNECTED = "Connect this company to Xero before changing its matches."
ALREADY_OPEN = "Finish the matching that's already open before changing the matches."
NOT_CHANGING = "There's no change of matches to cancel."
#: Why a matching is open (``entities.xero_matching``).
REASONS = ("first", "new_accounts", "new_org", "change")
CONTACTS_UNANSWERED = "Xero didn't send your contacts just now. Mind trying again in a moment?"
#: Daily closing moves cash from the float to the bank: they are two accounts (onboarding's
#: accounting.SAME_BANK), so they never share a Xero account - combining would make them one.
SAME_BANK = (
    "Petty cash and the bank you deposit into can't be the same Xero account. "
    "Choose a different one for each."
)


class Refused(Exception):
    """A refusal with its sentence and status (the hub answers ``{"error": ...}``)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


# ---- reading -----------------------------------------------------------------------------------


def own_accounts(entity_id):
    """The company's own accounts still to match: active, no Xero id, not combined away."""
    return AccountInfo.objects.filter(
        entity_id=entity_id, xero_account_id__isnull=True, merged_into__isnull=True
    ).exclude(record_status="archived")


def own_suppliers(entity_id, org=None):
    """The company's suppliers still to link: active, not combined away, with no Xero id - or, given
    the current organisation, one of another organisation (Phase 3: a new organisation), archived ones
    included while a record uses them (the user, 2026-11-25: no record keeps the old organisation's)."""
    unlinked = Q(xero_contact_id__isnull=True) | Q(xero_contact_id="")
    active = XeroContactSync.objects.filter(entity_id=entity_id, merged_into__isnull=True)
    if not org:
        return active.exclude(record_status="archived").filter(unlinked)
    other_org = Q(xero_org_id__isnull=False) & ~Q(xero_org_id=str(org))
    archived_in_use = (Q(record_status="archived") & other_org & ~Q(xero_contact_id="")
                       & Q(xero_contact_id__isnull=False) & Q(id__in=usage.used_contact_ids(entity_id)))
    return active.filter((~Q(record_status="archived") & (unlinked | other_org)) | archived_in_use)


def _active_accounts(entity_id):
    """Every account a matching may touch: active, not combined away, not archived in Xero."""
    return (AccountInfo.objects.filter(entity_id=entity_id, merged_into__isnull=True)
            .exclude(record_status="archived").exclude(status="ARCHIVED"))


def scope_accounts(entity_id, org, reason):
    """The accounts a matching for ``reason`` covers. ``first``: the company's own accounts (Phase 2 -
    in Minty's books, every one); any other: every active account (the user, 2026-11-24: every account
    needs a Xero account, and a reconnect shows them all - those linked to ``org`` prefilled).
    ``new_org`` also covers each archived account a record uses (the user, 2026-11-25: the chart is the
    current organisation's), so none is left on the old organisation's Xero account."""
    if reason == "first" or not reason:
        return own_accounts(entity_id)
    if reason == "new_org":
        archived_in_use = Q(id__in=usage.used_account_ids(entity_id)) & (
            Q(record_status="archived") | Q(status="ARCHIVED"))
        return AccountInfo.objects.filter(entity_id=entity_id, merged_into__isnull=True).filter(
            Q(id__in=_active_accounts(entity_id).values("id")) | archived_in_use)
    return _active_accounts(entity_id)


def needs_matching(entity: Entity, tenant_id=None) -> str | None:
    """Why a connect to ``tenant_id`` must match first, or None (the mirror, as before): the company
    keeps its books in Minty with its own accounts (``first``); it had another organisation
    (``new_org``); or, the same organisation, accounts were made in Minty meanwhile (``new_accounts``)."""
    if entity.setup_mode == "minty" and own_accounts(entity.id).exists():
        return "first"
    if not tenant_id or not entity.xero_org_id:
        return None
    if str(entity.xero_org_id) != str(tenant_id):
        return "new_org" if scope_accounts(entity.id, tenant_id, "new_org").exists() else None
    if entity.setup_mode == "xero" and own_accounts(entity.id).exists():
        return "new_accounts"
    return None


def accounts_pending(entity: Entity) -> bool:
    """A matching is open (``entities.xero_matching``)."""
    return entity.xero_matching in REASONS


def contacts_pending(entity: Entity) -> bool:
    """The accounts are matched (Finish ran: ``xero_publish_from`` set, no matching open) and some
    suppliers aren't in this organisation yet, so the company is still disconnected - while someone's
    Xero connection is in place (``connected_by_user_id``, set by the connect). A Disconnect clears it
    and keeps the organisation: that company is disconnected, not matching (production, 2026-11-23)."""
    return (entity.setup_mode == "xero" and bool(entity.xero_org_id) and entity.status != "connected"
            and entity.connected_by_user_id is not None
            and not entity.xero_matching and entity.xero_publish_from is not None
            and own_suppliers(entity.id, entity.xero_org_id).exists())


def pending(entity: Entity) -> bool:
    return accounts_pending(entity) or contacts_pending(entity)


def _minty_type(account: AccountInfo) -> str | None:
    return account.account_type or TYPE_FROM_XERO.get((account.type or "").strip().upper())


def _class(minty_type: str | None) -> str | None:
    return CLASS_OF.get(minty_type or "")


def _group(minty_type: str | None) -> str:
    if minty_type == "bank":
        return "cash_bank"
    if minty_type in ("sales", "revenue", "other_income"):
        return "sales"
    if minty_type == "direct_costs":
        return "cost_of_sales"
    if minty_type in ("expense", "overheads", "depreciation"):
        return "running"
    if minty_type in ("current_liability", "liability", "term_liability"):
        return "owed"
    return "other"


def _code(account: AccountInfo) -> str:
    return (account.code or account.xero_code or "").strip()


def _copies(entity_id, org) -> list[XeroAccount]:
    return list(XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org), status="ACTIVE")
                .order_by("code", "name"))


def _copy_type(copy: XeroAccount) -> str | None:
    return TYPE_FROM_XERO.get((copy.xero_type or "").strip().upper())


def _fits(account_type: str | None, copy: XeroAccount) -> bool:
    """A Xero account this one may be matched to: a bank only to a bank (daily closing moves money
    through it), anything else to one of the same class."""
    copy_type = _copy_type(copy)
    if account_type == "bank" or copy_type == "bank":
        return account_type == copy_type
    return _class(account_type) is not None and _class(account_type) == _class(copy_type)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()


def _suggestion(account: AccountInfo, copies: list[XeroAccount]) -> XeroAccount | None:
    """A pair Minty suggests (never picks): a bank by its account number, else by name; anything
    else of the same type with a similar name."""
    account_type = _minty_type(account)
    fitting = [c for c in copies if _fits(account_type, c)]
    if account_type == "bank" and account.bank_account_number:
        by_number = [c for c in fitting if (c.bank_account_number or "").strip()
                     == account.bank_account_number.strip()]
        if by_number:
            return by_number[0]
    same_type = [c for c in fitting if _copy_type(c) == account_type] or fitting
    scored = sorted(((_similar(account.name, c.name), c) for c in same_type), key=lambda s: -s[0])
    return scored[0][1] if scored and scored[0][0] >= 0.6 else None


def _prefill_links(entity_id, org, accounts) -> int:
    """Each account already linked to one of ``org``'s Xero accounts starts on it (a reconnect or a
    change shows it matched; the user, 2026-11-24). Existing picks are kept."""
    copies = {c.xero_guid: c for c in XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org),
                                                                   status="ACTIVE")}
    drafts = AccountMatchDraft.objects.filter(entity_id=entity_id)
    picked = set(drafts.values_list("account_id", flat=True))
    held = set(drafts.values_list("xero_account_id", flat=True))
    made = 0
    for account in accounts:
        copy = copies.get(account.xero_account_id or "")
        if copy is not None and account.id not in picked and copy.id not in held:
            AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id, xero_account_id=copy.id,
                                             picked_by=None)
            held.add(copy.id)
            made += 1
    return made


def auto_pick(entity_id, org, reason="first") -> int:
    """Pick each account in the matching's scope whose code and type equal one Xero account's
    (decision: same code, same type = no question) - by its Xero code, else its Minty code - when no
    other account has that Xero account. Existing picks are kept."""
    drafts = AccountMatchDraft.objects.filter(entity_id=entity_id)
    picked = set(drafts.values_list("account_id", flat=True))
    held = set(drafts.values_list("xero_account_id", flat=True))
    by_key: dict = {}
    for copy in _copies(entity_id, org):
        if copy.code:
            by_key.setdefault((copy.code, _copy_type(copy)), copy)
    made = 0
    for account in scope_accounts(entity_id, org, reason):
        if account.id in picked:
            continue
        for code in dict.fromkeys(c for c in ((account.xero_code or "").strip(), (account.code or "").strip()) if c):
            copy = by_key.get((code, _minty_type(account)))
            if copy is not None and copy.id not in held:
                AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id,
                                                 xero_account_id=copy.id, picked_by=None)
                held.add(copy.id)
                made += 1
                break
    return made


# ---- the contacts step ----------------------------------------------------------------------------------


def contact_scope(entity_id, org, reason) -> tuple[list, list[str]]:
    """What the contacts step asks (the user, 2026-11-24): the suppliers not yet in ``org`` and daily
    closing's contacts still empty. A change of the matches is about the accounts: it asks nothing."""
    if reason == "change" or not org:
        return [], []
    return list(own_suppliers(entity_id, org).order_by("name", "id")), pettycash_contacts_missing(entity_id)


def _contact_copies(entity_id, org) -> list[XeroContact]:
    return list(XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(org), status="ACTIVE")
                .order_by("name", "xero_guid"))


def _contact_drafts(entity_id, suppliers, slots) -> tuple[dict, dict]:
    """The contacts step's picks still in its scope: by supplier, and by daily-closing slot."""
    ids = {s.id for s in suppliers}
    by_contact, by_slot = {}, {}
    for draft in ContactMatchDraft.objects.filter(entity_id=entity_id):
        if draft.contact_id in ids:
            by_contact[draft.contact_id] = draft
        elif draft.slot in slots:
            by_slot[draft.slot] = draft
    return by_contact, by_slot


def _drop_stale_contact_drafts(entity_id, org, reason) -> None:
    suppliers, slots = contact_scope(entity_id, org, reason)
    by_contact, by_slot = _contact_drafts(entity_id, suppliers, slots)
    keep = [d.id for d in (*by_contact.values(), *by_slot.values())]
    ContactMatchDraft.objects.filter(entity_id=entity_id).exclude(id__in=keep).delete()


def _held_contacts(entity_id, suppliers, by_contact) -> tuple[dict, dict]:
    """Who already has each Xero contact (matching and merging are separate - the user, 2026-11-25):
    by Xero id, the Minty contact linked to it (outside the step); by copy id, the supplier whose pick
    it is. Such a contact is never picked for another supplier - the person merges the two first."""
    in_scope = {s.id for s in suppliers}
    linked = {r.xero_contact_id: r for r in XeroContactSync.objects.filter(entity_id=entity_id).exclude(
        xero_contact_id__isnull=True).exclude(xero_contact_id="").exclude(id__in=in_scope)}
    names_of = {s.id: s.name for s in suppliers}
    picked = {d.xero_contact_id: names_of[cid] for cid, d in by_contact.items() if d.xero_contact_id}
    return linked, picked


def auto_pick_contacts(entity_id, org, reason="first") -> int:
    """Each supplier without a pick takes the Xero contact of its name (case, spaces and punctuation
    ignored), when there is one and no other contact has it."""
    suppliers, slots = contact_scope(entity_id, org, reason)
    by_contact, _ = _contact_drafts(entity_id, suppliers, slots)
    linked, picked = _held_contacts(entity_id, suppliers, by_contact)
    by_name: dict = {}
    for copy in _contact_copies(entity_id, org):
        key = names.name_key(copy.name)
        if key:
            by_name.setdefault(key, copy)
    made = 0
    for supplier in suppliers:
        copy = by_name.get(names.name_key(supplier.name) or None)
        if (supplier.id not in by_contact and copy is not None and copy.xero_guid not in linked
                and copy.id not in picked):
            ContactMatchDraft.objects.create(entity_id=entity_id, contact_id=supplier.id, xero_contact_id=copy.id,
                                             picked_by=None)
            picked[copy.id] = supplier.name
            made += 1
    return made


def _contacts_blocking(entity_id, org, reason) -> int:
    suppliers, slots = contact_scope(entity_id, org, reason)
    by_contact, by_slot = _contact_drafts(entity_id, suppliers, slots)
    return (len(suppliers) - len(by_contact)) + (len(slots) - len(by_slot))


def read_contacts(entity: Entity) -> dict:
    """The contacts step: each supplier and daily-closing contact to answer, its pick, the Xero
    contacts to pick from - each with ``held_by``, the contact that already has it (a supplier can't
    pick one another contact has; a daily-closing slot can)."""
    org, reason = entity.xero_org_id, entity.xero_matching
    suppliers, slots = contact_scope(entity.id, org, reason)
    by_contact, by_slot = _contact_drafts(entity.id, suppliers, slots)
    linked, picked = _held_contacts(entity.id, suppliers, by_contact)

    def held_by(copy):
        holder = linked.get(copy.xero_guid)
        return holder.name if holder is not None else picked.get(copy.id)

    def answer(draft):
        if draft is None:
            return None
        return {"xero_contact_id": str(draft.xero_contact_id) if draft.xero_contact_id else None,
                "auto": draft.picked_by is None, "new": bool(draft.add_to_xero)}

    return {
        "required": reason != "change",
        "suppliers": [{"contact_id": str(s.id), "name": s.name, "pick": answer(by_contact.get(s.id))}
                      for s in suppliers],
        "slots": [{"slot": slot, "pick": answer(by_slot.get(slot))} for slot in slots],
        "options": [{"id": str(c.id), "name": c.name, "held_by": held_by(c)}
                    for c in (_contact_copies(entity.id, org) if suppliers or slots else [])],
        "blocking": (len(suppliers) - len(by_contact)) + (len(slots) - len(by_slot)),
    }


def read(entity: Entity) -> dict:
    """The matching page (frame 3): the accounts still to match first, then the matched ones in their
    groups, each with its pick or suggestion, whether a record uses it, conflicts; the Xero accounts to
    pick from and which account has each; the contacts step."""
    org = entity.xero_org_id
    copies = _copies(entity.id, org) if org else []
    by_id = {c.id: c for c in copies}
    accounts = sorted(scope_accounts(entity.id, org, entity.xero_matching),
                      key=lambda a: ([k for k, _t in GROUPS].index(_group(_minty_type(a))), _code(a), a.name or ""))
    in_scope = {a.id for a in accounts}
    drafts = {d.account_id: d for d in AccountMatchDraft.objects.filter(entity_id=entity.id)
              if d.account_id in in_scope}
    holder = {d.xero_account_id: d.account_id for d in drafts.values()}
    names_of = {a.id: a.name for a in accounts}
    used = usage.used_account_ids(entity.id)
    titles = dict(GROUPS)
    unmatched: list = []
    groups = {key: [] for key, _title in GROUPS}
    matched = 0
    for account in accounts:
        account_type = _minty_type(account)
        draft = drafts.get(account.id)
        copy = by_id.get(draft.xero_account_id) if draft else None
        same_code = [c for c in copies if c.code and c.code == _code(account)]
        # Same code, another type: listed, never picked; the person decides (design doc §4 rule 2).
        conflict = next((c for c in same_code if _copy_type(c) != account_type), None) if copy is None else None
        suggestion = _suggestion(account, copies) if copy is None else None
        if suggestion is not None and suggestion.id in holder:
            suggestion = None  # another account has it: no suggesting what can't be picked
        row = {
            "account_id": str(account.id),
            "code": _code(account),
            "name": account.name,
            "type": account_type,
            "kind": "bank" if account_type == "bank" else _class(account_type),
            "group": titles[_group(account_type)],
            "used": account.id in used,
            "system": bool(account.is_system),
            "pick": {"xero_account_id": str(copy.id), "auto": draft.picked_by is None} if copy else None,
            "suggestion": str(suggestion.id) if suggestion else None,
            "conflict": {"code": conflict.code, "name": conflict.name} if conflict else None,
        }
        if copy is None:
            unmatched.append(row)
        else:
            matched += 1
            groups[_group(account_type)].append(row)
    shown = [{"key": "unmatched", "title": "Not matched yet", "rows": unmatched}] if unmatched else []
    shown += [{"key": key, "title": title, "rows": groups[key]} for key, title in GROUPS if groups[key]]
    return {
        "organisation": entity.xero_tenant_name,
        # why this matching is open (Phase 3): first | new_accounts | new_org | change
        "reason": entity.xero_matching,
        "groups": shown,
        "options": [{"id": str(c.id), "code": c.code or "", "name": c.name,
                     "kind": "bank" if _copy_type(c) == "bank" else _class(_copy_type(c)),
                     "held_by": names_of.get(holder.get(c.id))} for c in copies],
        "matched": matched,
        "total": len(accounts),
        "blocking": len(unmatched),
        "contacts": read_contacts(entity),
        "pending": {"accounts": accounts_pending(entity), "contacts": contacts_pending(entity),
                    "reason": entity.xero_matching},
        "failed_contacts": [],
    }


# ---- begin, pick, refresh ------------------------------------------------------------------------


def _fill_copies(entity_id, access_token, org) -> None:
    accounts = sync.fetch_active_accounts(access_token, org)
    if not accounts:
        logger.error("xero matching: no chart of accounts from Xero for company %s (org %s)", entity_id, org)
        raise MirrorFailed("no chart of accounts")
    sync.lock(entity_id)
    sync._copy_accounts(entity_id, {a["AccountID"]: a for a in accounts if a.get("AccountID")}, org)
    contacts = sync.fetch_contacts(access_token, org)
    if contacts:
        sync._copy_contacts(entity_id, contacts, org)


def begin(entity_id, access_token, org, reason="first") -> None:
    """A connect that matches first (``needs_matching``): the Xero copies of ``org``, the marker, and
    the automatic picks - nothing mirrored, archived or deleted (inside the connect's transaction;
    ``MirrorFailed`` when Xero sends no chart, which rolls it back). The company is disconnected until
    Finish and its suppliers."""
    _fill_copies(entity_id, access_token, org)
    scope = scope_accounts(entity_id, org, reason)
    AccountMatchDraft.objects.filter(entity_id=entity_id).exclude(account_id__in=scope.values("id")).delete()
    Entity.objects.filter(id=entity_id).update(xero_matching=reason, status="disconnected")
    made = _prefill_links(entity_id, org, scope) + auto_pick(entity_id, org, reason)
    _drop_stale_contact_drafts(entity_id, org, reason)
    auto_pick_contacts(entity_id, org, reason)
    logger.info("xero matching: company %s began matching org %s (%s); %s picked automatically", entity_id, org,
                reason, made)


def start_change(entity_id) -> dict:
    """Change the matches (Phase 3): a connected company's matching page again, each account on its
    current Xero account. The company stays connected; publishing goes on."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if entity.xero_matching:
            raise Refused(ALREADY_OPEN)
        if entity.setup_mode != "xero" or not entity.xero_org_id or entity.status != "connected":
            raise Refused(NOT_CONNECTED)
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        _prefill_links(entity_id, entity.xero_org_id, _active_accounts(entity_id))
        Entity.objects.filter(id=entity_id).update(xero_matching="change")
        entity.xero_matching = "change"
    logger.info("xero matching: company %s is changing its matches", entity_id)
    return read(entity)


def cancel_change(entity_id) -> dict:
    """Leave a change of matches: nothing changed, the picks dropped."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if entity.xero_matching != "change":
            raise Refused(NOT_CHANGING)
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        Entity.objects.filter(id=entity_id).update(xero_matching=None)
        entity.xero_matching = None
    return read(entity)


def sync_during_change(entity_id, accounts: list[dict], org) -> dict:
    """A sync while the matches are being changed (the user, 2026-11-24): Xero's accounts come in as
    on any sync, except one the change already gives to an account (Finish links it there), and each
    new one starts matched to itself, so it never blocks Finish. Holds the company row, as Finish
    does, so the two never interleave; a change Finish or Cancel closed meanwhile is an ordinary sync."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if entity.xero_matching != "change":
            if entity.xero_matching is None and entity.setup_mode == "xero":
                return sync.apply_accounts(entity_id, accounts, org)
            sync.lock(entity_id)
            sync._copy_accounts(entity_id, {a["AccountID"]: a for a in accounts if a.get("AccountID")}, org)
            return {"copied": len(accounts)}
        picked = AccountMatchDraft.objects.filter(entity_id=entity_id).values_list("xero_account_id", flat=True)
        held = frozenset(XeroAccount.objects.filter(id__in=list(picked)).values_list("xero_guid", flat=True))
        counts = sync.apply_accounts(entity_id, accounts, org, skip=held)
        counts["prefilled"] = _prefill_links(entity_id, org, _active_accounts(entity_id))
    logger.info("xero sync: company %s is changing its matches; accounts %s", entity_id, counts)
    return counts


def _pending_entity(entity_id) -> Entity:
    entity = Entity.objects.get(id=entity_id)
    if not accounts_pending(entity):
        raise Refused(NOT_PENDING)
    return entity


def _cash_and_bank(entity_id) -> set:
    """Daily closing's petty cash and bank accounts (the pair that must stay two accounts)."""
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(
        "pettycash_account_id", "bank_account_id").first()
    return {v for v in (row or {}).values() if v}


#: Daily closing's contacts, in the Settings card's order, by the key the web names each one with.
PETTYCASH_CONTACTS = (
    ("cash_sale", "cash_sale_contact_id"),
    ("director", "director_contact_id"),
    ("discrepancy", "discrepancy_contact_id"),
)


def pettycash_contacts_missing(entity_id) -> list[str]:
    """The daily-closing contacts not chosen yet. Minty's own books need none, Xero's need all three
    (petty-cash-api ``xero_settings.PETTYCASH_SLOTS``), so after Finish Publish would refuse without
    them (production, 2026-11-23). A company banking every sale needs no cash-sale contact; one that
    never set daily closing up has its own Setup Required gate."""
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(
        *(column for _, column in PETTYCASH_CONTACTS)).first()
    if row is None:
        return []
    banks_all = Entity.objects.filter(id=entity_id, uses_sales_cash=False).exists()
    return [key for key, column in PETTYCASH_CONTACTS
            if not row[column] and not (key == "cash_sale" and banks_all)]


def _shares_cash_and_bank(entity_id, drafts: dict) -> bool:
    pair = _cash_and_bank(entity_id)
    picked = [drafts[a].xero_account_id for a in pair if a in drafts]
    return len(pair) == 2 and len(picked) == 2 and picked[0] == picked[1]


def pick(entity_id, account_id, xero_account_id, user_id) -> dict:
    """Save (or, with no Xero account, clear) one account's pick."""
    entity = _pending_entity(entity_id)
    account = scope_accounts(entity_id, entity.xero_org_id, entity.xero_matching).filter(id=account_id).first()
    if account is None:
        raise Refused(NOT_OWN, 404)
    with transaction.atomic():
        AccountMatchDraft.objects.filter(account_id=account.id).delete()
        if xero_account_id:
            copy = XeroAccount.objects.filter(id=xero_account_id, entity_id=entity_id,
                                              xero_org_id=str(entity.xero_org_id), status="ACTIVE").first()
            if copy is None or not _fits(_minty_type(account), copy):
                raise Refused(WRONG_KIND, 400)
            pair = _cash_and_bank(entity_id)
            if account.id in pair and AccountMatchDraft.objects.filter(
                    account_id__in=pair - {account.id}, xero_account_id=copy.id).exists():
                raise Refused(SAME_BANK, 400)
            other = AccountMatchDraft.objects.filter(entity_id=entity_id, xero_account_id=copy.id).first()
            if other is not None:
                # accounts are never merged (the user, 2026-11-24): one Minty account per Xero account
                name = AccountInfo.objects.filter(id=other.account_id).values_list("name", flat=True).first()
                raise Refused(TAKEN_PICK.format(other=name or "Another account"))
            AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id, xero_account_id=copy.id,
                                             picked_by=user_id)
    return read(entity)


def pick_contact(entity_id, user_id, *, contact_id=None, slot=None, xero_contact_id=None, new=False) -> dict:
    """Save (or, with no answer, clear) one answer of the contacts step: a supplier's Xero contact or
    "new in Xero", or a daily-closing contact's Xero contact."""
    from xero.services.merge import CONTACT_TAKEN, taken_by_archived

    entity = _pending_entity(entity_id)
    suppliers, slots = contact_scope(entity_id, entity.xero_org_id, entity.xero_matching)
    contact_id = uuid.UUID(str(contact_id)) if contact_id is not None else None
    if contact_id is not None:
        supplier = next((s for s in suppliers if s.id == contact_id), None)
        if supplier is None:
            raise Refused(CONTACT_NOT_OWN, 404)
        mine = ContactMatchDraft.objects.filter(contact_id=contact_id)
    elif slot in slots:
        supplier = None
        mine = ContactMatchDraft.objects.filter(entity_id=entity_id, slot=slot)
    else:
        raise Refused(CONTACT_NOT_OWN, 404)
    if new and (supplier is None or xero_contact_id is not None):
        raise Refused(SLOT_NEEDS_XERO if supplier is None else NOT_A_CONTACT, 400)
    copy = None
    if xero_contact_id is not None:
        copy = XeroContact.objects.filter(id=xero_contact_id, entity_id=entity_id, xero_org_id=str(entity.xero_org_id),
                                          status="ACTIVE").first()
        if copy is None:
            raise Refused(NOT_A_CONTACT, 400)
        holder = (XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=copy.xero_guid)
                  .exclude(id=contact_id).first())
        if holder is not None and (holder.record_status != "active" or holder.merged_into_id is not None):
            raise Refused(taken_by_archived(holder))
        if supplier is not None:
            # matching never merges (the user, 2026-11-25): another contact has it - merge the two first
            if holder is not None:
                raise Refused(CONTACT_TAKEN.format(name=holder.name))
            other = (ContactMatchDraft.objects.filter(entity_id=entity_id, xero_contact_id=copy.id,
                                                      contact_id__isnull=False).exclude(contact_id=contact_id).first())
            if other is not None:
                name = next((s.name for s in suppliers if s.id == other.contact_id), None)
                raise Refused(CONTACT_TAKEN.format(name=name or "Another contact"))
    with transaction.atomic():
        mine.delete()
        if copy is not None or new:
            ContactMatchDraft.objects.create(entity_id=entity_id, contact_id=contact_id, slot=None if contact_id
                                             else slot, xero_contact_id=copy.id if copy else None, add_to_xero=new,
                                             picked_by=user_id)
    return read(entity)


def read_recently(entity_id, org) -> bool:
    """The organisation's chart was copied under ``sync.SETTINGS_READ_EVERY`` ago (every fill stamps
    each copy's ``synced_at``)."""
    newest = XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org)).aggregate(
        newest=Max("synced_at"))["newest"]
    return newest is not None and timezone.now() - newest < sync.SETTINGS_READ_EVERY


def refresh(entity_id, access_token, *, throttle=False) -> dict:
    """Refresh from Xero: the copies again (an account created in Xero meanwhile appears) and any new
    automatic picks. ``throttle`` - opening the page (production, 2026-11-23: banks made in Xero after
    the connect never showed) - skips Xero when the chart was read under 2 minutes ago (C2)."""
    entity = _pending_entity(entity_id)
    if throttle and read_recently(entity_id, entity.xero_org_id):
        logger.info("xero matching: company %s's chart read under 2 minutes ago; not read again", entity_id)
        return read(entity)
    with transaction.atomic():
        _fill_copies(entity_id, access_token, entity.xero_org_id)
        auto_pick(entity_id, entity.xero_org_id, entity.xero_matching)
        _drop_stale_contact_drafts(entity_id, entity.xero_org_id, entity.xero_matching)
        auto_pick_contacts(entity_id, entity.xero_org_id, entity.xero_matching)
    return read(entity)


# ---- finish: the replace -------------------------------------------------------------------------


def _shown_code(account: AccountInfo) -> str:
    """The code records show and publishing sends: Xero's, else Minty's (the user, 2026-11-24)."""
    return (account.xero_code or account.code or "").strip()


def _log_code(code) -> str | None:
    return code if code and len(code) <= 10 else None  # account_change_log holds 10; longer is a name


def _log(batch, entity_id, account, kind, before, user_id, *, merged_into=None, copy=None, org=None) -> None:
    AccountChangeLog.objects.create(
        entity_id=entity_id, account_id=account.id, batch_id=batch, kind=kind,
        before_code=_log_code(before[0]) if before else None, before_name=before[1] if before else None,
        before_type=before[2] if before else None,
        after_code=_log_code(_shown_code(account)), after_name=account.name, after_type=_minty_type(account),
        merged_into_id=merged_into.id if merged_into else None, xero_org_id=str(org) if org else None,
        xero_guid=copy.xero_guid if copy else None, changed_by=user_id,
    )


def _link(entity_id, account: AccountInfo, copy: XeroAccount, batch, user_id) -> None:
    """The account takes Xero's link, Xero code, name and type in place - its Minty code stays (the
    user, 2026-11-24: Minty Code + Xero Code); its bill codes and every bill line on it follow,
    published ones included (the user's rule, 2026-11-24: records show the matched account; no
    Xero call - ``bill_text``)."""
    before = (_shown_code(account), account.name, _minty_type(account))
    old_codes = {c for c in ((account.xero_code or "").strip(), (account.code or "").strip()) if c}
    new_type = _copy_type(copy) or _minty_type(account)
    AccountInfo.objects.filter(id=account.id).update(
        xero_account_id=copy.xero_guid, xero_code=copy.code, type=copy.xero_type,
        account_type=new_type, name=(copy.name or account.name)[:80], status="ACTIVE", record_status="active",
        xero_account_link_id=copy.id,
    )
    account.refresh_from_db()
    if old_codes:
        # One bill code per Xero account (uq_ebax_entity_xero_account): the account's own (else the
        # first) takes the Xero account, any other of its codes goes.
        blank = EntityBillAccountXero.objects.filter(entity_id=entity_id, account_code__in=old_codes).filter(
            Q(xero_account_id__isnull=True) | Q(xero_account_id=""))
        rows = list(blank.order_by("sort_order", "account_code"))
        holder = EntityBillAccountXero.objects.filter(entity_id=entity_id, xero_account_id=copy.xero_guid).first()
        if holder is None and rows:
            holder = next((r for r in rows if r.account_code == before[0]), rows[0])
            EntityBillAccountXero.objects.filter(id=holder.id).update(
                account_code=copy.code or "", account_name=account.name, account_type=copy.xero_type,
                xero_account_id=copy.xero_guid)
        blank.exclude(id=holder.id if holder else None).delete()
        # lines saved before bill_line.account_id existed name the account by code only
        BillLine.objects.filter(bill__entity_id=entity_id, account_id__isnull=True,
                                account_code__in=old_codes).update(account_id=account.id)
    bill_text.refresh_accounts(entity_id, [account.id])
    EntityAccountXero.objects.filter(account_id=account.id).update(
        xero_account_id=copy.xero_guid, xero_org_id=copy.xero_org_id, name=account.name, type=copy.xero_type)
    # Minty used it, so it is one of the Xero accounts Minty uses (X2/X3's tick), and the sales
    # categories and Payment Settings keep listing it.
    if not account.is_system:
        AccountModule.objects.get_or_create(account_id=account.id, module_code="PETTY_CASH",
                                            defaults={"entity_id": entity_id, "is_active": True})
    changed = before != (_shown_code(account), account.name, _minty_type(account))
    _log(batch, entity_id, account, "overwritten" if changed else "linked", before, user_id, copy=copy,
         org=copy.xero_org_id)


def finish(entity_id, user_id) -> dict:
    """The replace (decision 5a's confirm), with the contacts step's answers. Refused, with nothing
    written, while an account has no pick, two share a Xero account, a pick names a Xero account or
    contact that is gone, or a contact the step asks has no answer."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if not accounts_pending(entity):
            raise Refused(NOT_PENDING)
        reason = entity.xero_matching
        sync.lock(entity_id)
        # Two accounts may swap Xero accounts (Phase 3's change): the one-active-link rule and the
        # active code/name rules are deferrable, checked at COMMIT.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        accounts = list(scope_accounts(entity_id, entity.xero_org_id, reason).select_for_update())
        in_scope = {a.id for a in accounts}
        drafts = {d.account_id: d for d in AccountMatchDraft.objects.filter(entity_id=entity_id)
                  if d.account_id in in_scope}
        unmatched = [a for a in accounts if a.id not in drafts]
        if unmatched:
            raise Refused(UNMATCHED.format(n=len(unmatched)))
        if len({d.xero_account_id for d in drafts.values()}) < len(drafts):
            raise Refused(SHARED)
        if _shares_cash_and_bank(entity_id, drafts):
            raise Refused(SAME_BANK)
        copies = {c.id: c for c in XeroAccount.objects.filter(
            entity_id=entity_id, xero_org_id=str(entity.xero_org_id), status="ACTIVE",
            id__in=[d.xero_account_id for d in drafts.values()])}
        if any(d.xero_account_id not in copies for d in drafts.values()):
            raise Refused(PICK_GONE)
        contacts_left = _contacts_blocking(entity_id, entity.xero_org_id, reason)
        if contacts_left:
            raise Refused(CONTACTS_UNMATCHED.format(n=contacts_left))
        used = usage.used_account_ids(entity_id)
        held = list(AccountInfo.objects.filter(entity_id=entity_id, xero_account_id__in=[
            c.xero_guid for c in copies.values()]).exclude(id__in=in_scope))
        if any(row.id in used for row in held):
            raise Refused("A Xero account you picked is already linked to another account here. Refresh from "
                          "Xero and pick again.")
        if held:
            # Xero-made rows nothing uses (a sync imported the chart before matching finished): the
            # company's own account takes the Xero account, so these go, with their old-reader rows.
            guids = [row.xero_account_id for row in held]
            EntityBillAccountXero.objects.filter(entity_id=entity_id, xero_account_id__in=guids).delete()
            AccountInfo.objects.filter(id__in=[row.id for row in held]).delete()
            logger.info("xero matching: company %s - %s unused Xero-made row(s) removed for its own accounts",
                        entity_id, len(held))
        batch = uuid.uuid4()
        changing = [a for a in accounts if a.xero_account_id != copies[drafts[a.id].xero_account_id].xero_guid]
        # A changed match first lets go of its old Xero id: (company, Xero id) is unique and not
        # deferrable, so two accounts swapping Xero accounts would otherwise collide mid-way.
        moving = [a.id for a in changing if a.xero_account_id]
        if moving:
            AccountInfo.objects.filter(id__in=moving).update(xero_account_id=None, xero_account_link_id=None)
        for account in changing:  # an unchanged match writes and logs nothing
            _link(entity_id, account, copies[drafts[account.id].xero_account_id], batch, user_id)
        if reason == "new_org":
            _let_go_of_old_links(entity_id, entity.xero_org_id, in_scope, batch, user_id)
        contacts = _apply_contacts(entity, batch, user_id)
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        done = {"setup_mode": "xero", "xero_matching": None}
        if reason in ("first", "new_org", None):
            # the day Finish ran (no longer a publish cut-off - owner, 2026-11-18)
            done["xero_publish_from"] = datetime.now(HK).date()
        Entity.objects.filter(id=entity_id).update(**done)
    logger.info("xero matching: company %s finished: %s linked, contacts %s (batch %s)", entity_id, len(changing),
                contacts, batch)
    return {"linked": len(changing), "contacts": contacts}


def _let_go_of_old_links(entity_id, org, in_scope, batch, user_id) -> None:
    """A new organisation's Finish (the user, 2026-11-25: no old organisation's link is left): each
    account Minty made that the matching didn't ask (archived, nothing uses it) lets go of its old Xero
    account and Xero code - its Minty code stays, so one brought back is Minty's own, to be matched.
    Xero-made rows are the sync's (it deletes the unused ones)."""
    current = set(XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org))
                  .values_list("xero_guid", flat=True))
    rows = (AccountInfo.objects.filter(entity_id=entity_id, merged_into__isnull=True, source__in=sync.MINTY_MADE,
                                       xero_account_id__isnull=False)
            .exclude(xero_account_id="").exclude(id__in=in_scope).exclude(xero_account_id__in=current))
    for account in list(rows):
        before = (_shown_code(account), account.name, _minty_type(account))
        old_guid = account.xero_account_id
        old_org = (EntityAccountXero.objects.filter(account_id=account.id).values_list("xero_org_id", flat=True)
                   .first())
        AccountInfo.objects.filter(id=account.id).update(xero_account_id=None, xero_account_link_id=None,
                                                         xero_code=None)
        EntityAccountXero.objects.filter(account_id=account.id).update(xero_account_id=None, xero_org_id=None)
        account.refresh_from_db()
        AccountChangeLog.objects.create(
            entity_id=entity_id, account_id=account.id, batch_id=batch, kind="unlinked",
            before_code=_log_code(before[0]), before_name=before[1], before_type=before[2],
            after_code=_log_code(_shown_code(account)), after_name=account.name, after_type=_minty_type(account),
            xero_org_id=old_org, xero_guid=old_guid, changed_by=user_id)
        logger.info("xero matching: company %s account %s let go of the old organisation's Xero account %s",
                    entity_id, account.id, old_guid)


def _apply_contacts(entity: Entity, batch, user_id) -> dict:
    """The contacts step's answers, in Finish's transaction: each supplier linked to its Xero contact,
    a new one left for its first publish; each daily-closing contact set to its Xero contact's row (made
    from the copy when there is none). A Xero contact another Minty contact already has refuses Finish -
    matching never merges (the user, 2026-11-25)."""
    from xero.services.merge import CONTACT_TAKEN, taken_by_archived

    org = str(entity.xero_org_id)
    suppliers, slots = contact_scope(entity.id, entity.xero_org_id, entity.xero_matching)
    by_contact, by_slot = _contact_drafts(entity.id, suppliers, slots)
    picked = [d.xero_contact_id for d in (*by_contact.values(), *by_slot.values()) if d.xero_contact_id]
    copies = {c.id: c for c in XeroContact.objects.filter(id__in=picked, entity_id=entity.id, xero_org_id=org,
                                                          status="ACTIVE")}
    if any(i not in copies for i in picked):
        raise Refused(CONTACT_GONE)
    counts = {"linked": 0, "new": 0, "slots": 0}
    for supplier in suppliers:
        draft = by_contact[supplier.id]
        if draft.add_to_xero:
            counts["new"] += 1  # Minty puts it in Xero at its first publish (C1)
            if supplier.xero_contact_id and supplier.xero_org_id and supplier.xero_org_id != org:
                # the old organisation's contact is let go, so that publish never names its id
                XeroContactSync.objects.filter(id=supplier.id).update(xero_contact_id=None, xero_org_id=None,
                                                                      xero_contact_link_id=None)
                ContactChangeLog.objects.create(entity_id=entity.id, contact_id=supplier.id, batch_id=batch,
                                                kind="unlinked", before_name=supplier.name,
                                                after_name=supplier.name, xero_org_id=supplier.xero_org_id,
                                                xero_guid=supplier.xero_contact_id, changed_by=user_id)
            continue
        copy = copies[draft.xero_contact_id]
        stored = (XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id=copy.xero_guid)
                  .exclude(id=supplier.id).first())
        if stored is not None:
            if stored.record_status != "active" or stored.merged_into_id is not None:
                raise Refused(taken_by_archived(stored))
            raise Refused(CONTACT_TAKEN.format(name=stored.name))
        _link_supplier(entity.id, supplier, copy.xero_guid, copy.name, org, batch, user_id, "linked")
        counts["linked"] += 1
    columns = dict(PETTYCASH_CONTACTS)
    for slot in slots:
        copy = copies[by_slot[slot].xero_contact_id]
        holder = XeroContactSync.objects.filter(entity_id=entity.id, xero_contact_id=copy.xero_guid).first()
        if holder is None:
            holder = XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id=copy.xero_guid, xero_org_id=org,
                                                    name=copy.name[:150], xero_contact_link_id=copy.id)
        elif holder.record_status != "active" or holder.merged_into_id is not None:
            raise Refused(taken_by_archived(holder))
        EntityPettycashSettings.objects.filter(entity_id=entity.id).update(**{columns[slot]: holder.id})
        counts["slots"] += 1
    ContactMatchDraft.objects.filter(entity_id=entity.id).delete()
    return counts


# ---- suppliers (frames 4, 5) ------------------------------------------------------------------------


def _link_supplier(entity_id, supplier, guid, name, org, batch, user_id, kind) -> None:
    copy = XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid).first()
    if copy is None:
        copy = XeroContact.objects.create(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid,
                                          name=(name or supplier.name)[:255], status="ACTIVE")
    XeroContactSync.objects.filter(id=supplier.id).update(xero_contact_id=guid, xero_org_id=str(org),
                                                          xero_contact_link_id=copy.id)
    ContactChangeLog.objects.create(entity_id=entity_id, contact_id=supplier.id, batch_id=batch, kind=kind,
                                    before_name=supplier.name, after_name=supplier.name, xero_org_id=str(org),
                                    xero_guid=guid, changed_by=user_id)


def link_suppliers(entity_id, access_token, org, user_id) -> list[str]:
    """Try again for a company the old rules left disconnected (before the contacts step, 2026-11-24):
    every own supplier linked to the Xero contact of its name (case, spaces and punctuation ignored -
    ``minty_shared.names``, the user 2026-11-23); one with no such contact stays in Minty and goes to
    Xero at its first publish - connecting creates nothing in Xero (owner's decisions doc C2,
    2026-11-19). One whose Xero contact another Minty contact already has stays unlinked - matching never
    merges (the user, 2026-11-25). Each in its own transaction. Returns no names: nothing can be refused
    (the list stays for the routes' answer)."""
    contacts = sync.fetch_contacts(access_token, org)
    if contacts is None:
        raise Refused(CONTACTS_UNANSWERED, 502)
    with transaction.atomic():
        sync.lock(entity_id)
        sync._copy_contacts(entity_id, contacts, org)
    by_name: dict = {}
    for contact in sorted(contacts, key=lambda c: (str(c.get("ContactStatus") or "ACTIVE").upper() != "ACTIVE",
                                                   str(c.get("ContactID") or ""))):
        key = names.name_key(contact.get("Name"))
        if key and contact.get("ContactID"):
            by_name.setdefault(key, contact)
    batch = uuid.uuid4()
    left = 0
    for supplier in list(own_suppliers(entity_id, org).order_by("name")):
        match = by_name.get(names.name_key(supplier.name))
        if match is None:
            left += 1  # not in Xero: it goes there at its first publish
            continue
        guid = match["ContactID"]
        if XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=guid).exists():
            left += 1  # another contact has it: the person merges the two (never matching)
            logger.info("xero matching: company %s supplier %s left unlinked - Xero contact %s is another "
                        "contact's", entity_id, supplier.id, guid)
            continue
        with transaction.atomic():
            _link_supplier(entity_id, supplier, guid, match.get("Name"), org, batch, user_id, "linked")
    logger.info("xero matching: company %s suppliers linked by name; %s stay in Minty until a publish", entity_id,
                left)
    return []


def complete(entity_id) -> None:
    """Matching done and every supplier in Xero: the company is connected."""
    Entity.objects.filter(id=entity_id).update(status="connected", last_connected_at=timezone.now())


# ---- one account, mapped again (the owner's decisions doc A3, 2026-11-19) ----------------------------

#: A3's sentence: shown at Settings and at Publish for an account Xero archived or removed.
GONE_FROM_XERO = ("This account is archived in Xero. Please check it in Xero, then map this account to the one "
                  "you want to use.")
TAKEN = "{other} is already linked to that Xero account. Choose a different Xero account."
NOT_A_XERO_ACCOUNT = "Choose one of your Xero accounts."


class Taken(Refused):
    """The Xero account picked is another active account's: accounts are never merged (the user,
    2026-11-24), so pick another."""

    def __init__(self, account: AccountInfo, holder: AccountInfo):
        super().__init__(TAKEN.format(other=holder.name), 409)
        self.holder = holder


def _mappable(entity_id, account_id) -> AccountInfo:
    account = AccountInfo.objects.filter(entity_id=entity_id, id=account_id, merged_into__isnull=True).first()
    if account is None:
        raise Refused(NOT_OWN, 404)
    return account


def read_one(entity: Entity, account_id) -> dict:
    """The one-account matching page (A3): the account, as the matching page draws a row, and the Xero
    accounts it can be mapped to."""
    account = _mappable(entity.id, account_id)
    copies = _copies(entity.id, entity.xero_org_id) if entity.xero_org_id else []
    account_type = _minty_type(account)
    suggestion = _suggestion(account, copies)
    gone = bool(account.xero_account_id) and (account.status or "").upper() != "ACTIVE"
    return {
        "organisation": entity.xero_tenant_name,
        "gone_from_xero": gone,
        "message": GONE_FROM_XERO if gone else None,
        "row": {"account_id": str(account.id), "code": _code(account), "name": account.name, "type": account_type,
                "kind": "bank" if account_type == "bank" else _class(account_type),
                "used": account.id in usage.used_account_ids(entity.id), "system": bool(account.is_system),
                "group": dict(GROUPS)[_group(account_type)], "pick": None,
                "suggestion": str(suggestion.id) if suggestion else None, "conflict": None},
        "options": [{"id": str(c.id), "code": c.code or "", "name": c.name,
                     "kind": "bank" if _copy_type(c) == "bank" else _class(_copy_type(c))} for c in copies],
    }


def map_one(entity: Entity, account_id, copy_id, user_id) -> dict:
    """The account linked to the Xero account picked - Xero's code, name and type, as at matching's
    Finish (``_link``). Only that account's link changes: no records move and nothing goes in the
    merging table (A3). Raises ``Taken`` when another active account holds that Xero account."""
    with transaction.atomic():
        sync.lock(entity.id)
        account = _mappable(entity.id, account_id)
        copy = XeroAccount.objects.filter(entity_id=entity.id, xero_org_id=str(entity.xero_org_id or ""), id=copy_id,
                                          status="ACTIVE").first()
        if copy is None:
            raise Refused(NOT_A_XERO_ACCOUNT, 422)
        holder = (AccountInfo.objects.filter(entity_id=entity.id, xero_account_id=copy.xero_guid,
                                             record_status="active").exclude(id=account.id).first())
        if holder is not None:
            raise Taken(account, holder)
        _link(entity.id, account, copy, uuid.uuid4(), user_id)
    logger.info("xero matching: company %s account %s mapped to Xero account %s", entity.id, account.id,
                copy.xero_guid)
    return {"message": f"{account.name} now uses {copy.name} in Xero.", "account_id": str(account.id)}
