"""Merging contacts in Settings (``POST /api/me/company/contacts/merge {ids, keep}``,
``xero/services/merge.py``; the owner's decisions doc M1-M4, 2026-11-19, and the user's of 2026-11-25):
several ticked contacts are absorbed into the one kept - always one of them, never a new contact; every
record moves to it, the others are archived with ``merged_into_id``, the merging table keeps what moved,
and Xero is not touched. While Xero is connected a contact that is in Xero is merged in Xero first.
Accounts are never merged (the user, 2026-11-24). Postgres only (the sync lock)."""

from __future__ import annotations

import os
import uuid

import pytest
from minty_db.models import (
    AccountInfo,
    Bill,
    ContactChangeLog,
    EntityPettycashSettings,
    MintyMerge,
    MintyMergeRecord,
    Report,
    ReportExpense,
    XeroContactSync,
)

from hub.tests.test_company import call
from xero.services import merge as merging

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


def _merge(shop, ids, keep, person="accountant"):
    return call("post", f"{CONTACTS}/merge", shop[person], shop["entity"].id,
                {"ids": [str(i) for i in ids], "keep": str(keep) if keep else None})


def test_several_contacts_merge_into_the_one_kept(shop):
    entity = shop["entity"]
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    kee2 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", source="user")
    kee3 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah HK", source="user")
    rent = account(entity, "Rent", "469")
    expenses(entity, rent, 2, contact=kee2)
    expenses(entity, rent, 1, contact=kee3)
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1", contact_id=kee2.id)
    EntityPettycashSettings.objects.create(entity_id=entity.id, cash_sale_contact_id=kee3.id)

    resp = _merge(shop, [kee2.id, kee.id, kee3.id], kee.id)

    assert resp.status_code == 200, resp.content
    assert resp.json()["message"] == "Kee Wah Bakery and Kee Wah HK merged into Kee Wah. Kee Wah stays."
    assert resp.json()["kept"] == str(kee.id)
    assert ReportExpense.objects.filter(contact_id=kee.id).count() == 3
    bill.refresh_from_db()
    assert bill.contact_id == kee.id
    assert EntityPettycashSettings.objects.get(entity_id=entity.id).cash_sale_contact_id == kee.id
    for gone in (kee2, kee3):
        gone.refresh_from_db()
        assert (gone.record_status, gone.merged_into_id) == ("archived", kee.id)
    kee.refresh_from_db()
    assert (kee.record_status, kee.merged_into_id) == ("active", None)
    merges = MintyMerge.objects.filter(entity_id=entity.id)
    assert sorted(m.merged_id for m in merges) == sorted([kee2.id, kee3.id])
    assert {(m.kind, m.reason, m.kept_id) for m in merges} == {("contact", "user", kee.id)}
    assert MintyMergeRecord.objects.filter(merge_id__in=[m.id for m in merges]).count() == 5


def test_the_one_kept_is_one_of_the_ticked_and_two_at_least(shop):
    """A merge never makes a new contact (the user, 2026-11-25): the kept one is among those ticked."""
    entity = shop["entity"]
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    kee2 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", source="user")
    other = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies", source="user")
    for ids, keep in (([kee.id, kee2.id], other.id), ([kee.id, kee2.id], None), ([kee.id], kee.id),
                      ([kee.id, kee.id], kee.id)):
        resp = _merge(shop, ids, keep)
        assert resp.status_code == 400, (ids, keep, resp.content)
    assert _merge(shop, [kee.id, kee2.id], kee.id, person="cashier").status_code == 403
    assert not MintyMerge.objects.exists()


def test_a_stranger_or_another_companys_record_gets_nowhere(shop):
    entity, other = shop["entity"], shop["other"]
    mine = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    theirs = XeroContactSync.objects.create(entity_id=other.id, name="Kee Wah Bakery", source="user")
    assert _merge(shop, [mine.id, theirs.id], mine.id).status_code == 404
    assert _merge(shop, [mine.id, uuid.uuid4()], mine.id).status_code == 404
    gone = XeroContactSync.objects.create(entity_id=entity.id, name="Old Kee", source="user", record_status="archived")
    assert _merge(shop, [mine.id, gone.id], mine.id).status_code == 404
    assert call("post", f"{CONTACTS}/merge", shop["accountant"], entity.id, {"ids": "x", "keep": 1}).status_code == 400
    # the pairwise route is gone
    assert call("post", f"{CONTACTS}/{mine.id}/merge", shop["accountant"], entity.id,
                {"into": str(mine.id)}).status_code == 404
    assert not MintyMerge.objects.exists()


def test_while_xero_is_connected_contacts_in_xero_are_merged_in_xero_first(shop):
    """The user, 2026-11-25: while Xero is connected, a contact in Xero is merged there first, and the
    sync follows. Two Minty-only contacts still merge here."""
    from minty_db.models import Entity

    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(status="connected", xero_org_id="org-1")
    linked = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies", xero_contact_id="x-abc",
                                            xero_org_id="org-1", source="xero")
    linked2 = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies Ltd", xero_contact_id="x-abc2",
                                             xero_org_id="org-1", source="xero")
    mine = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    mine2 = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", source="user")

    resp = _merge(shop, [mine.id, linked.id, linked2.id], mine.id)
    assert resp.status_code == 409
    assert resp.json()["error"] == merging.MERGE_IN_XERO.format(names="ABC Supplies and ABC Supplies Ltd")
    linked.refresh_from_db()
    assert linked.merged_into_id is None and not MintyMerge.objects.exists()

    assert _merge(shop, [mine.id, mine2.id], mine.id).status_code == 200


def test_while_xero_is_off_contacts_linked_to_it_merge_here_and_xero_keeps_its_own(shop):
    """The user's pick, 2026-11-25: disconnected, contacts linked to Xero merge in Minty (the page says
    Xero keeps them separate). The merged one keeps its Xero link, archived; a later pick of that Xero
    contact says it was merged, not deleted."""
    from minty_db.models import Entity

    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(status="disconnected", xero_org_id="org-1")
    kept = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies", xero_contact_id="x-abc",
                                          xero_org_id="org-1", source="xero")
    gone = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies Ltd", xero_contact_id="x-abc2",
                                          xero_org_id="org-1", source="xero")

    assert _merge(shop, [kept.id, gone.id], kept.id).status_code == 200
    gone.refresh_from_db()
    assert (gone.record_status, gone.merged_into_id, gone.xero_contact_id) == ("archived", kept.id, "x-abc2")

    other = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-kee",
                                           source="xero")
    resp = _relink(entity.id, other.id, {"xero_contact_id": "x-abc2", "reason": "user"})
    assert resp.status_code == 409
    assert resp.json()["error"] == merging.TAKEN_BY_MERGED.format(name="ABC Supplies Ltd", kept="ABC Supplies")


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


def test_a_relinked_contact_takes_its_xero_contacts_name_and_its_bills_follow(shop):
    """Xero's name at the link (the user, 2026-11-27): the stored copy's name, the bills relabelled."""
    from minty_db.models import Entity, XeroContact

    entity = shop["entity"]
    org = str(uuid.uuid4())
    Entity.objects.filter(id=entity.id).update(xero_org_id=org)
    XeroContact.objects.create(entity_id=entity.id, xero_org_id=org, xero_guid="x-kept", name="Kee Wah Bakery",
                               status="ACTIVE")
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-old", source="xero")
    bill = Bill.objects.create(entity_id=entity.id, reference="B-KEE", contact_id=kee.id, contact_name="Kee Wah")
    assert _relink(entity.id, kee.id, {"xero_contact_id": "x-kept", "reason": "merged_in_xero"}).status_code == 200
    kee.refresh_from_db()
    assert (kee.xero_contact_id, kee.name) == ("x-kept", "Kee Wah Bakery")
    assert Bill.objects.get(id=bill.id).contact_name == "Kee Wah Bakery"
    log = ContactChangeLog.objects.get(contact_id=kee.id)
    assert (log.before_name, log.after_name) == ("Kee Wah", "Kee Wah Bakery")


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


def test_mapping_now_to_a_xero_contact_another_contact_has_is_refused_never_merged(shop):
    """Matching and merging are separate (the user, 2026-11-25): "map it now" (reason ``user``) never
    merges - the person merges the two contacts first. Following Xero's own merge still moves them."""
    entity = shop["entity"]
    one = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", xero_contact_id="x-1", source="xero")
    XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah Bakery", xero_contact_id="x-2", source="xero")
    resp = _relink(entity.id, one.id, {"xero_contact_id": "x-2", "reason": "user"}, user_id=shop["accountant"].id)
    assert resp.status_code == 409
    assert resp.json()["error"] == merging.CONTACT_TAKEN.format(name="Kee Wah Bakery")
    one.refresh_from_db()
    assert (one.xero_contact_id, one.merged_into_id) == ("x-1", None)
    assert not MintyMerge.objects.exists()


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

    resp = _merge(shop, [kee2.id, kee.id], kee.id)

    assert resp.status_code == 200, resp.content
    bill.refresh_from_db()
    assert (bill.contact_id, bill.contact_name, bill.published_contact_name) == (kee.id, "Kee Wah", "Kee Wah Bakery")
