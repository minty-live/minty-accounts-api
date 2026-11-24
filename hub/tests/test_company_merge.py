"""Merging two contacts in Settings (``POST /api/me/company/contacts/<id>/merge``,
``xero/services/merge.py``; the owner's decisions doc M1-M4, 2026-11-19): every record moves to the
one kept, the merged one is archived with ``merged_into_id``, the merging table keeps what moved, and
Xero is not touched. Accounts are never merged (the user, 2026-11-24): their merge is gone. Postgres
only (the sync lock)."""

from __future__ import annotations

import os
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    Bill,
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
    mine = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    theirs = XeroContactSync.objects.create(entity_id=other.id, name="Kee Wah Bakery", source="user")
    resp = call("post", f"{CONTACTS}/{mine.id}/merge", shop["accountant"], entity.id, {"into": str(theirs.id)})
    assert resp.status_code == 404
    assert call("post", f"{CONTACTS}/{uuid.uuid4()}/merge", shop["accountant"], entity.id,
                {"into": str(mine.id)}).status_code == 404
    assert call("post", f"{CONTACTS}/{mine.id}/merge", shop["accountant"], entity.id, {}).status_code == 400
    assert not MintyMerge.objects.exists()


def test_accounts_are_never_merged(shop):
    """The user, 2026-11-24: no merging for the chart of accounts - only contacts merge. The route is gone."""
    entity = shop["entity"]
    taxi, transport = account(entity, "Taxi", "471"), account(entity, "Transport", "470")
    resp = call("post", f"{ACCOUNTS}/{taxi.id}/merge", shop["accountant"], entity.id, {"into": str(transport.id)})
    assert resp.status_code == 404
    taxi.refresh_from_db()
    assert (taxi.record_status, taxi.merged_into_id) == ("active", None)
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


def test_a_contact_merge_renames_its_bills_published_too(shop):
    entity = shop["entity"]
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    kee2 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", source="user")
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1", contact_id=kee2.id, contact_name="Kee Wah Bakery",
                               published="published", published_contact_name="Kee Wah Bakery")

    resp = call("post", f"{CONTACTS}/{kee2.id}/merge", shop["accountant"], entity.id, {"into": str(kee.id)})

    assert resp.status_code == 200, resp.content
    bill.refresh_from_db()
    assert (bill.contact_id, bill.contact_name, bill.published_contact_name) == (kee.id, "Kee Wah", "Kee Wah Bakery")
