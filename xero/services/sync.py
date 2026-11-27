"""A company's Xero accounts and contacts, brought into Minty's tables - the one sync every service
asks for (Phase 3 of the Flask retirement, 2026-11-06; the owner's choice: one sync, in
accounts-api).

Ported from Flask's ``entity/services/settings.py``: ``sync_xero_accounts_to_db`` /
``reconcile_accounts_with_snapshot``, ``sync_expense_account_info_from_xero``,
``sync_xero_coa_pettycash``, ``sync_xero_coa_bill`` and ``sync_contacts_if_changed``, and
``xero/services/integration.py``'s two fetches. Flask asked Xero for the same ACTIVE accounts three
times each time Petty Cash Settings opened; this asks once and does all of it:

- ``account_info`` matches Xero's ACTIVE accounts: new ones added, returning ones re-activated
  (an ARCHIVED row Xero has again comes back), renamed or recoded ones refreshed (a type Petty
  Cash's chart excludes keeps its old name, as in Flask). One Xero no longer has ACTIVE is deleted
  only when nothing uses it (``usage.used_account_ids``) and Minty didn't make it; otherwise it is
  ARCHIVED and kept, with every expense line, daily-closing account and sales method still pointing
  at it (2026-11-11: deleting set them NULL). A row with no Xero id - Minty's own - is never
  touched. One exception for daily closing's accounts (2026-11-13): when Xero REPLACED the account
  (one active account of the same code and type, a new AccountID), the settings move to it
  (``_repoint_replaced_settings``); expense and bill lines stay on the archived row.
- Every overwrite is logged (the user, 2026-11-24): a refresh writes ``account_change_log`` kind
  ``overwritten`` (Xero's code, name and type before and after, ``changed_by`` NULL), and a Minty
  account Xero takes over by code and type ``linked``. Xero's code goes in ``xero_code``; the Minty
  code (``code``) stays - the schema fills it from Xero's only while it is empty (minty-db v0.18).
- **Minty follows a merge done in Xero** (the user, 2026-11-25: merging is Minty's only for contacts
  not in Xero; the others are merged in Xero first). Xero's contact list leaves out archived contacts,
  and only ``GET /Contacts/{id}`` names ``MergedToContactID``: each linked contact missing from the list
  is asked for by id (``follow_xero_merges``, at most ``FOLLOW_MERGES_PER_RUN`` a run) and one Xero
  merged follows Xero's kept contact - relinked, or merged into the Minty contact that has it
  (``merge.relink_contact``, reason ``merged_in_xero``). This runs before new contacts are added, so
  Xero's kept contact is never made a second row first. One archived with no merge is left for publish
  to ask about (C4).
- ``entity_account_xero`` (Petty Cash's code list) gains a row per eligible account; the ticks
  (``is_active``) are never changed here.
- ``entity_bill_account_xero`` (Payment Requests' list) likewise, and loses rows whose account is gone.
- ``xero_contact_sync`` gains new contacts and renames changed ones. Nothing is removed: absence
  from Xero's answer is ambiguous (archived, a partial fetch).
- **The Xero copies (Xero optional, minty-db v0.5's EXPAND, 2026-11-10).** Every account and contact
  Xero sent is upserted into ``xero_account`` / ``xero_contact`` (keyed by company, organisation and
  Xero id; Xero's values verbatim, the code read as ``account_code_from_xero`` reads it), and the
  Minty row with that Xero id is linked to its copy (``xero_account_link_id`` /
  ``xero_contact_link_id``). An account copy Xero no longer sends is marked ``DELETED``, never
  removed: a link or a published record may name it. A row Minty made itself (no Xero id) is left
  alone.

A failed or empty fetch changes nothing. Each half runs in one transaction under the company's sync
lock (``lock``: ``pg_advisory_xact_lock(hashtextextended('xero-sync:<entity>', 0))``, Flask's key), so
two syncs of one company never overlap. It is taken before any lock on the company row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone
from minty_db.models import (
    AccountChangeLog,
    AccountInfo,
    AccountModule,
    EntityAccountXero,
    EntityBillAccountXero,
    XeroAccount,
    XeroContact,
    XeroContactSync,
)
from minty_shared import names

from xero.services import bill_text, usage

logger = logging.getLogger("minty-accounts-api")

TIMEOUT = (10, 60)
CONTACTS_PAGE = 1000
#: Opening Settings reads Xero at most this often per company (owner's decisions doc C2, 2026-11-19).
SETTINGS_READ_EVERY = timedelta(minutes=2)
#: Contacts a sync asks Xero about one by one to follow Xero's merges (Xero allows 60 calls a minute).
FOLLOW_MERGES_PER_RUN = 25
#: Types Petty Cash's chart never shows (Flask's ``COA_EXCLUDED_TYPES``): their names aren't refreshed.
COA_EXCLUDED_TYPES = frozenset({"BANK", "EQUITY", "OTHERINCOME", "SALES", "REVENUE"})
#: ``account_info.source`` of a row Minty made (Auto Fill, or added by a person): never deleted here.
MINTY_MADE = ("template", "user")
#: Petty Cash's code list (``COA_INCLUDED_TYPES``) and Payment Requests' (``BILL_COA_INCLUDED_TYPES``).
COA_INCLUDED_TYPES = frozenset({"DIRECTCOSTS", "EXPENSE", "OVERHEADS", "PREPAYMENT"})
BILL_COA_INCLUDED_TYPES = frozenset({"DIRECTCOSTS", "EXPENSE", "FIXED", "OVERHEADS", "PREPAYMENT"})


def _headers(access_token: str, org) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Xero-Tenant-Id": str(org), "Accept": "application/json"}


def _trunc(value, length: int):
    return str(value)[:length] if value else value


def fetch_active_accounts(access_token: str, org) -> list[dict] | None:
    """Xero's ACTIVE accounts; None when Xero could not be asked or refused (logged)."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Accounts", headers=_headers(access_token, org),
                                params={"where": 'Status=="ACTIVE"'}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("xero sync: accounts unreachable for org %s: %s", org, exc)
        return None
    if response.status_code != 200:
        logger.error("xero sync: accounts answered %s for org %s: %s", response.status_code, org,
                     (response.text or "")[:300])
        return None
    try:
        return (response.json() or {}).get("Accounts") or []
    except ValueError:
        logger.error("xero sync: accounts answer for org %s was not JSON", org)
        return None


def fetch_contacts(access_token: str, org) -> list[dict] | None:
    """Every contact, page by page; None when any page failed (a part list would read as removals)."""
    contacts: list[dict] = []
    page = 1
    while True:
        try:
            response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts", headers=_headers(access_token, org),
                                    params={"page": page, "pageSize": CONTACTS_PAGE, "order": "Name ASC"},
                                    timeout=TIMEOUT)
        except requests.RequestException as exc:
            logger.error("xero sync: contacts page %s unreachable for org %s: %s", page, org, exc)
            return None
        if response.status_code != 200:
            logger.error("xero sync: contacts page %s answered %s for org %s; %s gathered so far discarded",
                         page, response.status_code, org, len(contacts))
            return None
        try:
            found = (response.json() or {}).get("Contacts") or []
        except ValueError:
            logger.error("xero sync: contacts page %s for org %s was not JSON", page, org)
            return None
        contacts.extend(found)
        if len(found) < CONTACTS_PAGE:
            return contacts
        page += 1


def contact_by_id(access_token: str, org, guid: str) -> dict | None:
    """Xero's contact ``guid`` as ``GET /Contacts/{id}`` answers it (``ContactStatus``,
    ``MergedToContactID``), or None when Xero no longer has it. Raises ``requests.RequestException``
    on any other answer."""
    response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts/{guid}", headers=_headers(access_token, org),
                            timeout=TIMEOUT)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise requests.RequestException(f"Xero answered {response.status_code} for contact {guid}")
    found = (response.json() or {}).get("Contacts") or []
    return found[0] if found else None


def follow_xero_merges(entity_id, access_token: str, org, contacts: list[dict]) -> int:
    """Each linked contact Xero's list no longer has, asked for by id: one Xero merged follows the
    contact Xero kept (``merge.relink_contact``, ``merged_in_xero``). Returns how many followed."""
    from minty_db.models import XeroContactSync as Contact

    from xero.services import merge

    listed = {c.get("ContactID") for c in contacts if c.get("ContactID")}
    missing = list(Contact.objects.filter(entity_id=entity_id, xero_org_id=str(org), record_status="active",
                                          merged_into__isnull=True)
                   .exclude(xero_contact_id__isnull=True).exclude(xero_contact_id="")
                   .exclude(xero_contact_id__in=listed).order_by("name", "id")[:FOLLOW_MERGES_PER_RUN + 1])
    if len(missing) > FOLLOW_MERGES_PER_RUN:
        logger.info("xero sync: company %s has more than %s contacts gone from Xero's list; the rest next sync",
                    entity_id, FOLLOW_MERGES_PER_RUN)
    followed = 0
    for row in missing[:FOLLOW_MERGES_PER_RUN]:
        try:
            found = contact_by_id(access_token, org, row.xero_contact_id)
        except (requests.RequestException, ValueError) as exc:
            logger.error("xero sync: company %s - Xero didn't answer for contact %s (%s); merges not followed",
                         entity_id, row.xero_contact_id, exc)
            break
        target = str((found or {}).get("MergedToContactID") or "").strip()
        if not target or target == row.xero_contact_id:
            continue  # archived with no merge, or gone: publish asks (C4)
        try:
            merge.relink_contact(entity_id, row.id, target, None, reason="merged_in_xero")
            followed += 1
        except merge.Refused as refusal:
            logger.warning("xero sync: company %s contact %s not following Xero's merge into %s: %s", entity_id,
                           row.id, target, refusal.message)
    return followed


def _contacts(entity_id, access_token, org, contacts: list[dict] | None) -> dict | None:
    """Xero's merges followed first, then the list applied (``apply_contacts``)."""
    if contacts is None:
        return None
    followed = follow_xero_merges(entity_id, access_token, org, contacts)
    return {**apply_contacts(entity_id, contacts, org), "followed_merges": followed}


def lock(entity_id) -> None:
    """One sync at a time writes this company's Xero rows (Flask's ``lock_entity_xero_sync``, the same
    key). Transaction-scoped: call inside ``transaction.atomic``; taking it again in the same
    transaction is free.

    **Take it before any row lock on the company, its ``entities`` row included.** Every write under
    it to a table with a foreign key to ``entities`` takes ``FOR KEY SHARE`` on the company row, so a
    transaction holding that row ``FOR UPDATE`` while it waits here deadlocks with the sync that holds
    the lock (production, 2026-11-26: ``matching.sync_during_change``; ``hub/tests/test_xero_lock_order.py``)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"xero-sync:{entity_id}"])


def _status(account: dict) -> str:
    raw = account.get("Status")
    return str(raw).strip().upper() if raw is not None and str(raw).strip() else "ACTIVE"


def _log_code(code) -> str | None:
    """A code as account_change_log holds it (10 characters): longer is a name, not a code."""
    code = str(code or "").strip()
    return code if 0 < len(code) <= 10 else None


def _log(entity_id, row: AccountInfo, kind: str, before: tuple, batch, org) -> None:
    """One line of ``account_change_log`` for what the sync changed (``changed_by`` NULL)."""
    row.refresh_from_db(fields=["account_type"])
    AccountChangeLog.objects.create(
        entity_id=entity_id, account_id=row.id, batch_id=batch, kind=kind, before_code=_log_code(before[0]),
        before_name=(before[1] or None) and before[1][:150], before_type=before[2],
        after_code=_log_code(row.xero_code), after_name=row.name[:150] if row.name else None,
        after_type=row.account_type, xero_org_id=str(org) if org else None, xero_guid=row.xero_account_id,
    )


def apply_accounts(entity_id, accounts: list[dict], org, skip: frozenset = frozenset()) -> dict:
    """Make the company's account tables match Xero's ACTIVE ``accounts`` (non-empty). ``skip``: Xero
    ids not to add as accounts of their own (an open change already gives each to an account)."""
    counts = {"added": 0, "linked": 0, "activated": 0, "refreshed": 0, "archived": 0, "deleted": 0}
    batch = uuid.uuid4()
    with transaction.atomic():
        lock(entity_id)
        snapshot = {a["AccountID"]: a for a in accounts if a.get("AccountID")}
        rows = list(AccountInfo.objects.filter(entity_id=entity_id))
        # No duplicates (owner, 2026-11-12): a new Xero account takes the place of the company's own
        # unlinked account of the same code and type, rather than arriving beside it.
        own = {((r.xero_code or r.code or "").strip(), r.type): r for r in rows
               if r.source in MINTY_MADE and not r.xero_account_id and r.merged_into_id is None
               and r.record_status == "active"}
        used = usage.used_account_ids(entity_id)
        kept = {}
        renamed = []
        for row in rows:
            if not row.xero_account_id or row.merged_into_id is not None:
                # Minty's own account, or one merged away - kept as the record of its merge
                # (owner's decisions doc M2): the sync never touches it
                continue
            if row.xero_account_id not in snapshot:
                if row.id in used or row.source in MINTY_MADE:
                    if row.status != "ARCHIVED":
                        row.status = "ARCHIVED"
                        row.save(update_fields=["status"])
                        counts["archived"] += 1
                else:
                    row.delete()
                    counts["deleted"] += 1
                continue
            kept[row.xero_account_id] = row
        for account_id, account in snapshot.items():
            row = kept.get(account_id)
            if row is None and account_id in skip:
                continue
            if row is None:
                mine = own.pop((str(account.get("Code") or "").strip(), account.get("Type")), None)
                if mine is not None:
                    before = (mine.xero_code, mine.name, mine.account_type)
                    mine.xero_account_id, mine.status = account_id, "ACTIVE"
                    mine.xero_code = _trunc(account.get("Code", ""), 50)
                    mine.save(update_fields=["xero_account_id", "status", "xero_code"])
                    _log(entity_id, mine, "linked", before, batch, org)
                    counts["linked"] += 1
                    continue
                try:
                    with transaction.atomic():
                        AccountInfo.objects.create(
                            entity_id=entity_id, xero_account_id=account_id, status="ACTIVE",
                            name=_trunc(account.get("Name", ""), 80) or "", type=_trunc(account.get("Type", ""), 50) or "",
                            xero_code=_trunc(account.get("Code", ""), 50),
                            class_type=_trunc(account.get("Class", ""), 50),
                            bank_account_number=_trunc(account.get("BankAccountNumber", ""), 50),
                            bank_account_type=_trunc(account.get("BankAccountType", ""), 50),
                            description=_trunc(account.get("Description", ""), 255),
                        )
                    counts["added"] += 1
                except IntegrityError:  # uq_account_entity_xero: a writer that skipped the lock
                    logger.warning("xero sync: account %s already stored for company %s", account_id, entity_id)
                continue
            changed = []
            if row.status != "ACTIVE" and _status(account) == "ACTIVE":
                row.status = "ACTIVE"
                changed.append("status")
                counts["activated"] += 1
            elif row.record_status == "archived" and row.status == "ACTIVE" and _status(account) == "ACTIVE":
                # archived in Minty while Xero was off (Minty's mark, Xero's status untouched): once
                # connected the chart is Xero's, so it comes back - in the pickers again (the user, 2026-11-27)
                row.record_status = "active"
                changed.append("record_status")
                counts["activated"] += 1
                EntityAccountXero.objects.filter(account_id=row.id).update(is_active=True)
                AccountModule.objects.filter(account_id=row.id).update(is_active=True)
                EntityBillAccountXero.objects.filter(entity_id=entity_id, xero_account_id=row.xero_account_id).update(
                    is_active=True, is_deleted=False)
            code = str(account.get("Code") or "").strip()
            before = (row.xero_code, row.name, row.account_type)
            if account.get("Type") not in COA_EXCLUDED_TYPES and code:
                # cut to what Minty holds: Xero's names run to 150 (a long one failed the whole sync)
                for field, value in (("name", _trunc(account.get("Name", row.name), 80) or row.name),
                                     ("type", _trunc(account.get("Type", row.type), 50) or row.type),
                                     ("xero_code", _trunc(code, 50))):
                    if getattr(row, field) != value:
                        setattr(row, field, value)
                        changed.append(field)
            if changed:
                row.save(update_fields=sorted(set(changed)))
                if set(changed) - {"status", "record_status"}:
                    _log(entity_id, row, "overwritten", before, batch, org)
                    counts["refreshed"] += 1
                    renamed.append(row.id)
        # a rename in Xero shows on every bill (the user's rule, 2026-11-24)
        bill_text.refresh_accounts(entity_id, renamed)
        _repoint_replaced_settings(entity_id, snapshot)
        _mirror_petty_cash(entity_id, org)
        _mirror_bills(entity_id)
        if org:
            _copy_accounts(entity_id, snapshot, org)
    logger.info("xero sync: accounts for company %s: %s", entity_id, counts)
    return counts


def _repoint_replaced_settings(entity_id, snapshot: dict) -> None:
    """Move daily closing's accounts off an account Xero replaced (2026-11-13).

    Xero archiving an account and making it again gives it a new AccountID. The old row was archived
    above, with the settings still on it, so every publish into it failed. A setting on an archived
    Xero-made row that Xero no longer sends moves to the ONE active row of the same code and type
    that Xero does send. No code, or no single match, leaves it where it is: the settings card marks
    it archived. State-based, so a row archived before this rule is mended by the next sync.
    """
    from minty_db.models import EntityPettycashSettings

    settings_row = EntityPettycashSettings.objects.filter(entity_id=entity_id).first()
    if settings_row is None:
        return
    chosen = {slot: getattr(settings_row, slot) for slot in usage.ACCOUNT_SLOTS if getattr(settings_row, slot)}
    if not chosen:
        return
    rows = {r.id: r for r in AccountInfo.objects.filter(entity_id=entity_id)}
    replacements: dict = {}
    for slot, account_id in chosen.items():
        old = rows.get(account_id)
        if (old is None or old.status != "ARCHIVED" or not old.xero_account_id
                or old.xero_account_id in snapshot or not (old.xero_code or "").strip()):
            continue
        if old.id not in replacements:
            matches = [r for r in rows.values()
                       if r.id != old.id and r.status == "ACTIVE" and r.record_status == "active"
                       and r.merged_into_id is None and r.xero_account_id in snapshot
                       and r.type == old.type and (r.xero_code or "").strip() == old.xero_code.strip()]
            replacements[old.id] = matches[0] if len(matches) == 1 else None
            if len(matches) > 1:
                logger.warning("xero sync: company %s: %s accounts could replace archived account %s (code %s); "
                               "its settings stay on it", entity_id, len(matches), old.id, old.xero_code)
        new = replacements[old.id]
        if new is not None:
            setattr(settings_row, slot, new.id)
            logger.info("xero sync: company %s: %s moved from archived account %s to %s (code %s, replaced in Xero)",
                        entity_id, slot, old.id, new.id, old.xero_code)
    moved = [slot for slot in chosen if getattr(settings_row, slot) != chosen[slot]]
    if moved:
        settings_row.save(update_fields=[slot.removesuffix("_id") for slot in moved])


def _code(account: dict) -> str | None:
    """Xero's code as Minty reads it (the schema's ``account_code_from_xero``, minty-db v0.17): none
    for an empty one, or for one longer than 10 - an account's name in the code field, of any type."""
    code = str(account.get("Code") or "").strip()
    return code if 0 < len(code) <= 10 else None


def _copy_accounts(entity_id, snapshot: dict, org) -> None:
    """Upsert ``xero_account`` from Xero's answer, mark the ones it no longer sends ``DELETED``, and
    link each Xero-made ``account_info`` row to its copy."""
    now = timezone.now()
    copies = {m.xero_guid: m for m in XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org))}
    for guid, account in snapshot.items():
        values = {
            "code": _code(account), "name": _trunc(account.get("Name", ""), 150) or "",
            "xero_type": _trunc(account.get("Type", ""), 50) or "", "xero_class": _trunc(account.get("Class"), 50),
            "status": _status(account), "bank_account_number": _trunc(account.get("BankAccountNumber"), 50),
            "bank_account_type": _trunc(account.get("BankAccountType"), 50), "synced_at": now,
        }
        copy = copies.get(guid)
        if copy is None:
            copies[guid] = XeroAccount.objects.create(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid,
                                                      **values)
        else:
            for field, value in values.items():
                setattr(copy, field, value)
            copy.save(update_fields=list(values))
    XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org)).exclude(
        xero_guid__in=list(snapshot)).exclude(status="DELETED").update(status="DELETED", synced_at=now)
    for row in AccountInfo.objects.filter(entity_id=entity_id, xero_account_id__in=list(snapshot)):
        copy = copies[row.xero_account_id]
        if row.xero_account_link_id != copy.id:
            row.xero_account_link_id = copy.id
            row.save(update_fields=["xero_account_link"])


def _mirror_petty_cash(entity_id, org) -> None:
    """Every Petty Cash-eligible account has an ``entity_account_xero`` row; ticks untouched."""
    existing = {e.account_id: e for e in EntityAccountXero.objects.filter(account__entity_id=entity_id)}
    for account in AccountInfo.objects.filter(entity_id=entity_id, type__in=COA_INCLUDED_TYPES):
        row = existing.get(account.id)
        if row is None:
            EntityAccountXero.objects.create(account_id=account.id, name=account.name, type=account.type,
                                             xero_org_id=org, xero_account_id=account.xero_account_id,
                                             is_active=account.status == "ACTIVE")
        else:
            row.name, row.type, row.xero_account_id = account.name, account.type, account.xero_account_id
            if org:
                row.xero_org_id = org
            row.save(update_fields=["name", "type", "xero_account_id", "xero_org_id"])


def _mirror_bills(entity_id) -> None:
    """Every bill-eligible account has an ``entity_bill_account_xero`` row (``is_active`` and
    ``is_deleted`` untouched); a row naming a Xero account that is gone is deleted. A row with no Xero
    id - a code added by hand, or a Minty company's own account - is never touched."""
    existing = {b.xero_account_id: b for b in EntityBillAccountXero.objects.filter(entity_id=entity_id)}
    for account in AccountInfo.objects.filter(entity_id=entity_id, type__in=BILL_COA_INCLUDED_TYPES,
                                              xero_account_id__isnull=False):
        code = str(account.xero_code or "").strip()
        row = existing.get(account.xero_account_id)
        if row is None:
            EntityBillAccountXero.objects.create(
                entity_id=entity_id, xero_account_id=account.xero_account_id, account_code=code,
                account_name=account.name or "", account_type=account.type or "", is_default=False,
                is_active=account.status == "ACTIVE", is_deleted=False, sort_order=0)
        else:
            row.account_code, row.account_name, row.account_type = code, account.name or "", account.type or ""
            row.save(update_fields=["account_code", "account_name", "account_type"])
    if AccountInfo.objects.filter(entity_id=entity_id).exists():  # never wipe on a transiently empty table
        live = [x for x in AccountInfo.objects.filter(entity_id=entity_id).values_list("xero_account_id", flat=True)
                if x]
        EntityBillAccountXero.objects.filter(entity_id=entity_id).exclude(xero_account_id__isnull=True).exclude(
            xero_account_id="").exclude(xero_account_id__in=live).delete()


def take_xero_name(entity_id, contact_id, current: str, xero_name) -> str:
    """A Minty contact linked to a Xero contact takes Xero's name at the link (the user, 2026-11-27:
    linked contacts are changed in Xero and Minty follows - never a silent flip at the next sync), and
    the bills on it follow at once (``bill_text``). Returns the name it has now."""
    name = _trunc(xero_name or "", 150) or ""
    if not name or name == current:
        return current
    XeroContactSync.objects.filter(id=contact_id).update(name=name)
    bill_text.refresh_contacts(entity_id, [contact_id])
    return name


def apply_contacts(entity_id, contacts: list[dict], org) -> dict:
    """New contacts added, changed names updated; nothing removed."""
    counts = {"added": 0, "linked": 0, "renamed": 0, "restored": 0}
    with transaction.atomic():
        lock(entity_id)
        stored = {c.xero_contact_id: c for c in XeroContactSync.objects.filter(entity_id=entity_id)}
        # No duplicates (owner, 2026-11-12): a new Xero contact is the company's own unlinked one of
        # the same name (case, spaces and punctuation ignored - ``minty_shared.names``, the user
        # 2026-11-23: "7 Eleven" is "7-Eleven"), rather than a second row beside it.
        own = {names.name_key(c.name): c for c in XeroContactSync.objects.filter(
            entity_id=entity_id, source__in=MINTY_MADE, merged_into__isnull=True, record_status="active")
            .filter(models.Q(xero_contact_id__isnull=True) | models.Q(xero_contact_id=""))}
        own.pop("", None)  # a name with no letters or digits matches nothing
        renamed = []
        for contact in contacts:
            contact_id = contact.get("ContactID")
            if not contact_id:
                continue
            name = _trunc(contact.get("Name", ""), 150) or ""
            row = stored.get(contact_id)
            if row is None and (mine := own.pop(names.name_key(name) or None, None)) is not None:
                XeroContactSync.objects.filter(id=mine.id).update(xero_contact_id=contact_id, xero_org_id=str(org))
                take_xero_name(entity_id, mine.id, mine.name, name)
                stored[contact_id] = True
                counts["linked"] += 1
                continue
            if row is None:
                XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=contact_id, xero_org_id=str(org),
                                               name=name)
                stored[contact_id] = True
                counts["added"] += 1
            elif row is not True:
                if row.record_status == "archived" and row.merged_into_id is None:
                    # archived in Minty while Xero was off: Xero still has it, so it comes back (the
                    # user, 2026-11-27); a merged-away row stays the record of its merge
                    row.record_status = "active"
                    row.save(update_fields=["record_status"])
                    counts["restored"] += 1
                if (row.name or "") != name:
                    row.name = name
                    row.save(update_fields=["name"])
                    counts["renamed"] += 1
                    renamed.append(row.id)
        bill_text.refresh_contacts(entity_id, renamed)  # the user's rule, 2026-11-24
        if org:
            _copy_contacts(entity_id, contacts, org)
    logger.info("xero sync: contacts for company %s: %s", entity_id, counts)
    return counts


def _copy_contacts(entity_id, contacts: list[dict], org) -> None:
    """Upsert ``xero_contact`` from Xero's answer and link each Xero-made supplier to its copy.
    Nothing is marked: the contact fetch is the whole list only when every page answered."""
    now = timezone.now()
    copies = {m.xero_guid: m for m in XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(org))}
    for contact in contacts:
        guid = contact.get("ContactID")
        if not guid:
            continue
        name = _trunc(contact.get("Name", ""), 255) or ""
        status = str(contact.get("ContactStatus") or "ACTIVE").strip().upper() or "ACTIVE"
        copy = copies.get(guid)
        if copy is None:
            copies[guid] = XeroContact.objects.create(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid,
                                                      name=name, status=status, synced_at=now)
        else:
            copy.name, copy.status, copy.synced_at = name, status, now
            copy.save(update_fields=["name", "status", "synced_at"])
    for row in XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id__in=list(copies)):
        copy = copies[row.xero_contact_id]
        if row.xero_contact_link_id != copy.id:
            row.xero_contact_link_id = copy.id
            row.save(update_fields=["xero_contact_link"])


def read_recently(entity_id) -> bool:
    """The company's Xero contacts were read under ``SETTINGS_READ_EVERY`` ago: every sync stamps
    each copy's ``synced_at`` (``_copy_contacts``), so the newest stamp is the last read. Two workers
    may both read once at the edge; nothing worse."""
    newest = XeroContact.objects.filter(entity_id=entity_id).aggregate(newest=models.Max("synced_at"))["newest"]
    return newest is not None and timezone.now() - newest < SETTINGS_READ_EVERY


def sync_company(entity_id, access_token: str, org) -> dict:
    """Both halves. ``{"accounts": counts | None, "contacts": counts | None}``; None = Xero
    could not be asked (nothing changed)."""
    accounts = fetch_active_accounts(access_token, org)
    contacts = fetch_contacts(access_token, org)
    from minty_db.models import Entity

    if Entity.objects.filter(id=entity_id, setup_mode="xero", xero_matching="change").exists():
        # A change of matches (the user, 2026-11-24): the company stays connected to the same
        # organisation, so Xero's new accounts come in as on any sync - an open change held them back
        # for days in production - each matched to itself on the page (``matching.sync_during_change``).
        from xero.services import matching

        return {
            "accounts": matching.sync_during_change(entity_id, accounts, org) if accounts else None,
            "contacts": _contacts(entity_id, access_token, org, contacts),
        }
    if Entity.objects.filter(id=entity_id).filter(
            models.Q(setup_mode="minty") | models.Q(xero_matching__isnull=False)).exists():
        # Still Minty's books, or a first or new-organisation matching open (Phase 3, 2026-11-12) -
        # its accounts are being matched (Settings Phase 2, 2026-11-11): only the Xero copies are
        # refreshed. A new organisation's chart would otherwise archive the old one's. Importing Xero's
        # chart as accounts would put rows beside the company's own holding the very Xero ids
        # matching's Finish links.
        with transaction.atomic():
            lock(entity_id)
            if accounts:
                _copy_accounts(entity_id, {a["AccountID"]: a for a in accounts if a.get("AccountID")}, org)
            if contacts:
                _copy_contacts(entity_id, contacts, org)
        logger.info("xero sync: company %s is matching its accounts; only the Xero copies refreshed", entity_id)
        return {"accounts": {"copied": len(accounts or [])} if accounts else None,
                "contacts": {"copied": len(contacts)} if contacts is not None else None}
    # The sync only reads (owner's decisions doc C2, 2026-11-19): a contact made in Minty reaches
    # Xero when it is added while connected (C5) or at its first publish (C1), never here.
    return {
        "accounts": apply_accounts(entity_id, accounts, org) if accounts else None,
        "contacts": _contacts(entity_id, access_token, org, contacts),
    }
