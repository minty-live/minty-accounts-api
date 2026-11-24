"""Merging two of a company's contacts (the owner's decisions doc "Minty Xero Sync & Report
Decisions", M1-M4, 2026-11-19). Accounts are never merged (the user, 2026-11-24): an account matches
its own Xero account, and Settings has no account Merge; accounts merged before then keep their
``merged_into_id`` and merging-table rows, and every reader still follows them.

A merge moves every record on the merged contact to the one kept - expenses, bills, daily closing's
slots - and archives the merged one with ``merged_into_id``, so it can't be picked for new records
and the AI readers never suggest it. No new record is made and Xero is not touched (M1).
``minty_merge`` keeps the merge (from, to, who, when, why) and ``minty_merge_record`` every record
that moved, so each one's original can always be found (M2); there is no undo in the app (M4).

The ONE merge behaviour (M3): Xero matching's Finish (``matching._merge_supplier``) runs it with
reason ``xero_matching``; the Settings Merge (``hub/company_chart.py``) runs ``merge_contacts``,
reason ``user``; a contact Xero merged into one the company already has (C4) runs
``merge_contacts``, reason ``merged_in_xero``. Callers hold the transaction and ``sync.lock``; the
public merges take both.
"""

from __future__ import annotations

import logging
import uuid

from django.db import transaction
from minty_db.models import (
    Bill,
    ContactChangeLog,
    EntityPettycashSettings,
    MintyMerge,
    MintyMergeRecord,
    ReportExpense,
    XeroContactSync,
)

from xero.services import bill_text, sync, usage

logger = logging.getLogger("minty-accounts-api")

NOT_FOUND = "That isn't one of this company's {kind}s any more. Reload the list and try again."
SAME = "Choose two different {kind}s to merge."
MERGED_AWAY = "{name} was already merged into another {kind}. Choose another one to keep."


class Refused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


class _Merge:
    """One ``minty_merge`` row and the ``minty_merge_record`` rows of what it moved."""

    def __init__(self, entity_id, kind: str, merged_id, kept_id, reason: str, user_id):
        self.row = MintyMerge.objects.create(entity_id=entity_id, kind=kind, merged_id=merged_id, kept_id=kept_id,
                                             reason=reason, merged_by_user_id=user_id)
        self.moved: dict[str, int] = {}

    def move(self, queryset, table: str, column: str, **values) -> None:
        """Every row of ``queryset`` recorded (by its primary key - ``entity_pettycash_settings``' is the
        company), then updated with ``values``."""
        ids = list(queryset.values_list("pk", flat=True))
        if not ids:
            return
        MintyMergeRecord.objects.bulk_create(
            [MintyMergeRecord(merge_id=self.row.id, table_name=table, column_name=column, record_id=i) for i in ids])
        queryset.model.objects.filter(pk__in=ids).update(**values)
        self.moved[table] = self.moved.get(table, 0) + len(ids)

    def record(self, table: str, column: str, ids) -> None:
        """Rows that went with the merge (deleted or moved by hand): recorded only."""
        ids = list(ids)
        MintyMergeRecord.objects.bulk_create(
            [MintyMergeRecord(merge_id=self.row.id, table_name=table, column_name=column, record_id=i) for i in ids])
        if ids:
            self.moved[table] = self.moved.get(table, 0) + len(ids)


def combine_contacts(entity_id, kept: XeroContactSync, merged: XeroContactSync, batch, user_id, *,
                     reason: str) -> dict[str, int]:
    """Every record of ``merged`` moved to ``kept``; ``merged`` archived with ``merged_into_id`` and
    logged (``contact_change_log`` + the merging table)."""
    merge = _Merge(entity_id, "contact", merged.id, kept.id, reason, user_id)
    merge.move(ReportExpense.objects.filter(contact_id=merged.id), "report_expense", "contact_id",
               contact_id=kept.id)
    merge.move(Bill.objects.filter(entity_id=entity_id, contact_id=merged.id), "bill", "contact_id",
               contact_id=kept.id)
    for column in usage.CONTACT_SLOTS:
        merge.move(EntityPettycashSettings.objects.filter(entity_id=entity_id, **{column: merged.id}),
                   "entity_pettycash_settings", column, **{column: kept.id})
    XeroContactSync.objects.filter(id=merged.id).update(record_status="archived", merged_into_id=kept.id)
    merged.record_status = "archived"
    ContactChangeLog.objects.create(entity_id=entity_id, contact_id=merged.id, batch_id=batch, kind="merged",
                                    before_name=merged.name, after_name=kept.name, merged_into_id=kept.id,
                                    xero_org_id=kept.xero_org_id, xero_guid=kept.xero_contact_id, changed_by=user_id)
    logger.info("merge: company %s contact %s merged into %s (%s): %s", entity_id, merged.id, kept.id, reason,
                merge.moved)
    bill_text.refresh_contacts(entity_id, [kept.id])  # its bills, published ones too, show its name
    return merge.moved


# ---- the Settings Merge (and C4) ---------------------------------------------------------------------


def _active(model, entity_id, record_id):
    return model.objects.filter(entity_id=entity_id, id=record_id, record_status="active",
                                merged_into__isnull=True).first()


def merge_contacts(entity_id, merged_id, kept_id, user_id, *, reason: str = "user") -> dict:
    """``merged_id``'s records moved to ``kept_id`` (M1-M4). Raises ``Refused``."""
    if str(merged_id) == str(kept_id):
        raise Refused(SAME.format(kind="contact"), 400)
    with transaction.atomic():
        sync.lock(entity_id)
        merged = _active(XeroContactSync, entity_id, merged_id)
        kept = XeroContactSync.objects.filter(entity_id=entity_id, id=kept_id).first()
        if merged is None or kept is None:
            raise Refused(NOT_FOUND.format(kind="contact"), 404)
        if kept.merged_into_id is not None:
            raise Refused(MERGED_AWAY.format(name=kept.name, kind="contact"))
        if kept.record_status != "active":
            raise Refused(NOT_FOUND.format(kind="contact"), 404)
        moved = combine_contacts(entity_id, kept, merged, uuid.uuid4(), user_id, reason=reason)
    return {"moved": moved, "message": f"{merged.name} merged into {kept.name}."}


# ---- a contact Xero archived (owner's decisions doc C4) ---------------------------------------------

TAKEN_BY_ARCHIVED = ("That Xero contact belongs to {name}, which this company deleted. Add {name} back, or "
                     "choose another contact.")


def relink_contact(entity_id, contact_id, xero_guid: str, user_id, *, reason: str) -> dict:
    """The company's contact pointed at another Xero contact: the one Xero merged it into
    (``merged_in_xero``, at publish, no prompt) or the one the person picked (``user``, "map it
    now"). When that Xero contact is already a different Minty contact, the two are merged - the
    records move to that one and the merge is kept in the merging table. Raises ``Refused``."""
    from minty_db.models import Entity, XeroContact

    with transaction.atomic():
        sync.lock(entity_id)
        contact = _active(XeroContactSync, entity_id, contact_id)
        if contact is None:
            raise Refused(NOT_FOUND.format(kind="contact"), 404)
        holder = (XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=xero_guid)
                  .exclude(id=contact.id).first())
        if holder is not None:
            if holder.record_status != "active" or holder.merged_into_id is not None:
                raise Refused(TAKEN_BY_ARCHIVED.format(name=holder.name))
            moved = combine_contacts(entity_id, holder, contact, uuid.uuid4(), user_id, reason=reason)
            return {"contact_id": str(holder.id), "xero_contact_id": xero_guid, "merged": True, "moved": moved}
        org = Entity.objects.filter(id=entity_id).values_list("xero_org_id", flat=True).first()
        copy = XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(org), xero_guid=xero_guid).first()
        XeroContactSync.objects.filter(id=contact.id).update(xero_contact_id=xero_guid, xero_org_id=str(org),
                                                             xero_contact_link_id=copy.id if copy else None)
        ContactChangeLog.objects.create(entity_id=entity_id, contact_id=contact.id, batch_id=uuid.uuid4(),
                                        kind="linked", before_name=contact.name, after_name=contact.name,
                                        xero_org_id=str(org), xero_guid=xero_guid, changed_by=user_id)
        logger.info("merge: company %s contact %s relinked to Xero contact %s (%s)", entity_id, contact.id,
                    xero_guid, reason)
    return {"contact_id": str(contact.id), "xero_contact_id": xero_guid, "merged": False, "moved": {}}
