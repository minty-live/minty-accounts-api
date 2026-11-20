"""Merging two accounts or two contacts in Settings (``POST /api/me/company/{accounts|contacts}/<id>/merge``,
``xero/services/merge.py``; the owner's decisions doc M1-M4, 2026-11-19): every record moves to the
one kept, the merged one is archived with ``merged_into_id``, the merging table keeps what moved, and
Xero is not touched. Postgres only (the sync lock)."""

from __future__ import annotations

import os
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    AccountModule,
    Bill,
    BillLine,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    MintyMerge,
    MintyMergeRecord,
    Report,
    ReportExpense,
    XeroContactSync,
)

from hub.tests.test_company import call

pytestmark = pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only (the sync lock)")

ACCOUNTS = "/api/me/company/accounts"
CONTACTS = "/api/me/company/contacts"


def account(entity, name, code, account_type="expense", **extra):
    return AccountInfo.objects.create(entity_id=entity.id, name=name, code=code, xero_code=code, type="EXPENSE",
                                      account_type=account_type, status="ACTIVE", source="user", **extra)


def expenses(entity, account_row, n, contact=None):
    report = Report.objects.filter(entity_id=entity.id).first() or Report.objects.create(
        entity_id=entity.id, transaction_date="2026-11-19")
    return [ReportExpense.objects.create(report_id=report.id, account_id=account_row.id, amount=1,
                                         contact_id=contact.id if contact else None) for _ in range(n)]


def test_merging_taxi_into_transport_moves_every_record_and_keeps_what_moved(shop):
    """M1/M2: the doc's example - Taxi (12 expenses) into Transport (35): Transport has 47."""
    entity = shop["entity"]
    taxi, transport = account(entity, "Taxi", "471"), account(entity, "Transport", "470")
    expenses(entity, taxi, 12)
    expenses(entity, transport, 35)
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1")
    line = BillLine.objects.create(bill=bill, account_id=taxi.id, account_code="471")
    EntityPettycashSettings.objects.create(entity_id=entity.id, discrepancy_account_id=taxi.id)
    AccountModule.objects.create(account_id=taxi.id, entity_id=entity.id, module_code="PETTY_CASH", is_active=True)
    EntityAccountXero.objects.create(account_id=taxi.id, is_active=True)
    code = EntityBillAccountXero.objects.create(entity_id=entity.id, account_code="471", account_name="Taxi",
                                                account_type="EXPENSE", xero_account_id="", is_active=True)

    resp = call("post", f"{ACCOUNTS}/{taxi.id}/merge", shop["accountant"], entity.id, {"into": str(transport.id)})

    assert resp.status_code == 200, resp.content
    assert resp.json()["message"] == "Taxi merged into Transport."
    assert ReportExpense.objects.filter(account_id=transport.id).count() == 47
    line.refresh_from_db()
    assert (line.account_id, line.account_code) == (transport.id, "470"), "a republish sends Transport's code"
    assert EntityPettycashSettings.objects.get(entity_id=entity.id).discrepancy_account_id == transport.id
    assert AccountModule.objects.filter(account_id=transport.id, module_code="PETTY_CASH").exists()
    code.refresh_from_db()
    assert (code.is_active, code.is_deleted) == (False, True), "off Payment Settings and the invoice reader"
    taxi.refresh_from_db()
    assert (taxi.record_status, taxi.merged_into_id) == ("archived", transport.id)
    assert AccountInfo.objects.filter(entity_id=entity.id).count() == 2, "no new account"

    merge = MintyMerge.objects.get(entity_id=entity.id)
    assert (merge.kind, merge.merged_id, merge.kept_id, merge.reason, merge.merged_by_user_id) == (
        "account", taxi.id, transport.id, "user", shop["accountant"].id)
    moved = MintyMergeRecord.objects.filter(merge_id=merge.id)
    assert moved.filter(table_name="report_expense").count() == 12
    assert moved.filter(table_name="bill_line", record_id=line.id).exists()


def test_a_merge_needs_the_right_two_accounts(shop):
    entity = shop["entity"]
    taxi, transport = account(entity, "Taxi", "471"), account(entity, "Transport", "470")
    bank = account(entity, "HSBC", "090", account_type="bank")
    petty = account(entity, "Petty Cash", "091", account_type="bank", is_system=True)

    def merge(merged, into, person="accountant"):
        return call("post", f"{ACCOUNTS}/{merged.id}/merge", shop[person], entity.id, {"into": str(into.id)})

    assert merge(taxi, transport, person="manager").status_code == 403
    assert merge(taxi, taxi).status_code == 400
    refused = merge(taxi, bank)
    assert (refused.status_code, refused.json()["error"]) == (
        409, "Taxi and HSBC are different kinds of account, so they can't be merged.")
    assert merge(petty, bank).status_code == 409, "daily closing's own accounts stay"
    assert merge(taxi, transport).status_code == 200
    again = merge(transport, taxi)
    assert again.status_code == 409 and "already merged" in again.json()["error"], "M4: never a merged-away target"
    assert MintyMerge.objects.filter(entity_id=entity.id).count() == 1


def test_an_account_xero_archived_can_be_merged_away_but_never_kept(shop):
    """A3's "merge instead" (2026-11-20): a used account whose Xero account was archived is archived
    here too (the sync sets its status, a trigger its record status) - it can still be merged into
    an account Xero has, and its records move; it can't be the one kept."""
    entity = shop["entity"]
    gone = account(entity, "Taxi", "471", xero_account_id="x-taxi")
    transport = account(entity, "Transport", "470", xero_account_id="x-transport")
    expenses(entity, gone, 3)
    AccountInfo.objects.filter(id=gone.id).update(status="ARCHIVED")
    gone.refresh_from_db()
    assert gone.record_status == "archived", "the trigger follows Xero's status"

    kept_gone = call("post", f"{ACCOUNTS}/{transport.id}/merge", shop["accountant"], entity.id,
                     {"into": str(gone.id)})
    assert kept_gone.status_code == 404, "an account Xero archived is never the one kept"

    resp = call("post", f"{ACCOUNTS}/{gone.id}/merge", shop["accountant"], entity.id, {"into": str(transport.id)})

    assert resp.status_code == 200, resp.content
    assert ReportExpense.objects.filter(account_id=transport.id).count() == 3
    gone.refresh_from_db()
    assert (gone.record_status, gone.merged_into_id) == ("archived", transport.id)
    again = call("post", f"{ACCOUNTS}/{gone.id}/merge", shop["accountant"], entity.id, {"into": str(transport.id)})
    assert again.status_code == 404, "merged away once: gone for good"


def test_an_account_archived_in_minty_is_still_not_mergeable(shop):
    entity = shop["entity"]
    old, transport = account(entity, "Old", "472"), account(entity, "Transport", "470")
    AccountInfo.objects.filter(id=old.id).update(record_status="archived")

    resp = call("post", f"{ACCOUNTS}/{old.id}/merge", shop["accountant"], entity.id, {"into": str(transport.id)})

    assert resp.status_code == 404


def test_merging_contacts_moves_expenses_bills_and_daily_closings(shop):
    entity = shop["entity"]
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    kee2 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", source="user")
    rent = account(entity, "Rent", "469")
    expenses(entity, rent, 2, contact=kee2)
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1", contact_id=kee2.id)
    EntityPettycashSettings.objects.create(entity_id=entity.id, cash_sale_contact_id=kee2.id)

    resp = call("post", f"{CONTACTS}/{kee2.id}/merge", shop["accountant"], entity.id, {"into": str(kee.id)})

    assert resp.status_code == 200 and resp.json()["message"] == "Kee Wah Bakery merged into Kee Wah."
    assert ReportExpense.objects.filter(contact_id=kee.id).count() == 2
    bill.refresh_from_db()
    assert bill.contact_id == kee.id
    assert EntityPettycashSettings.objects.get(entity_id=entity.id).cash_sale_contact_id == kee.id
    kee2.refresh_from_db()
    assert (kee2.record_status, kee2.merged_into_id) == ("archived", kee.id)
    merge = MintyMerge.objects.get(entity_id=entity.id)
    assert (merge.kind, merge.reason) == ("contact", "user")
    assert MintyMergeRecord.objects.filter(merge_id=merge.id).count() == 4


def test_a_stranger_or_another_companys_record_gets_nowhere(shop):
    entity, other = shop["entity"], shop["other"]
    mine = account(entity, "Taxi", "471")
    theirs = account(other, "Transport", "470")
    resp = call("post", f"{ACCOUNTS}/{mine.id}/merge", shop["accountant"], entity.id, {"into": str(theirs.id)})
    assert resp.status_code == 404
    assert call("post", f"{ACCOUNTS}/{uuid.uuid4()}/merge", shop["accountant"], entity.id,
                {"into": str(mine.id)}).status_code == 404
    assert call("post", f"{ACCOUNTS}/{mine.id}/merge", shop["accountant"], entity.id, {}).status_code == 400
    assert not MintyMerge.objects.exists()


# ---- C4: a contact Xero archived, relinked (POST /api/internal/xero/contacts/<id>/relink) -----------


def _relink(entity_id, contact_id, body, scope="xero-contact-relink", user_id=None):
    import json
    from datetime import UTC, datetime, timedelta

    import jwt
    from django.conf import settings
    from django.test import Client

    claims = {"entity_id": str(entity_id), "scope": scope, "exp": datetime.now(UTC) + timedelta(minutes=1)}
    if user_id:
        claims["user_id"] = str(user_id)
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
    return Client().post(f"/api/internal/xero/contacts/{contact_id}/relink", data=json.dumps(body),
                         content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")


def test_a_contact_xero_merged_follows_xeros_kept_contact(shop):
    entity = shop["entity"]
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-old", source="xero")
    resp = _relink(entity.id, kee.id, {"xero_contact_id": "x-kept", "reason": "merged_in_xero"})
    assert (resp.status_code, resp.json()["merged"]) == (200, False)
    kee.refresh_from_db()
    assert kee.xero_contact_id == "x-kept"
    assert not MintyMerge.objects.exists()


def test_xeros_kept_contact_already_another_minty_contact_merges_the_two(shop):
    """The user's C4 clash rule: Minty follows Xero - the records move, the merge is kept."""
    entity = shop["entity"]
    one = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-1", source="xero")
    two = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", xero_contact_id="x-2",
                                         source="xero")
    rent = account(entity, "Rent", "469")
    expenses(entity, rent, 3, contact=one)

    resp = _relink(entity.id, one.id, {"xero_contact_id": "x-2", "reason": "merged_in_xero"})

    assert resp.status_code == 200 and resp.json()["merged"] is True and resp.json()["contact_id"] == str(two.id)
    assert ReportExpense.objects.filter(contact_id=two.id).count() == 3
    one.refresh_from_db()
    assert (one.record_status, one.merged_into_id) == ("archived", two.id)
    merge = MintyMerge.objects.get(entity_id=entity.id)
    assert (merge.reason, merge.merged_by_user_id) == ("merged_in_xero", None)


def test_mapping_to_a_contact_the_company_deleted_is_refused(shop):
    entity = shop["entity"]
    one = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-1", source="xero")
    XeroContactSync.objects.create(entity_id=entity.id, name="Old Kee", xero_contact_id="x-2", source="xero",
                                   record_status="archived")
    resp = _relink(entity.id, one.id, {"xero_contact_id": "x-2", "reason": "user"}, user_id=shop["accountant"].id)
    assert resp.status_code == 409 and "Old Kee" in resp.json()["error"]


def test_the_relink_needs_its_own_assertion_and_good_input(shop):
    entity = shop["entity"]
    one = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-1", source="xero")
    assert _relink(entity.id, one.id, {"xero_contact_id": "x-2", "reason": "user"},
                   scope="xero-sync").status_code == 401
    assert _relink(entity.id, one.id, {"xero_contact_id": "", "reason": "user"}).status_code == 400
    assert _relink(entity.id, one.id, {"xero_contact_id": "x-2", "reason": "restore"}).status_code == 400
    assert _relink(entity.id, uuid.uuid4(), {"xero_contact_id": "x-2", "reason": "user"}).status_code == 404
