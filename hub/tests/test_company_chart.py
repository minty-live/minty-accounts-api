"""Settings' accounts and contacts (``hub/company_chart.py``, owner 2026-11-12): the hub's door -
the person, the company, the right - then onboarding-api, which holds the one implementation,
asked with a service assertion naming the company and the person. onboarding-api is faked at
``requests.request``."""

from __future__ import annotations

import uuid

import jwt
import pytest
from django.conf import settings

from hub import company_chart
from hub.tests.test_company import call

ACCOUNTS = "/api/me/company/accounts"
CONTACTS = "/api/me/company/contacts"


class Onboarding:
    def __init__(self):
        self.asked = []
        self.answer = (200, {"accounts": []})
        self.down = False

    def request(self, method, url, headers=None, json=None, timeout=None):
        import requests

        if self.down:
            raise requests.ConnectionError("down")
        claims = jwt.decode(headers["Authorization"][7:], settings.SECRET_KEY, algorithms=["HS256"])
        self.asked.append((method, url.split("/api/internal/chart/")[1], json, claims))
        status, body = self.answer

        class Answer:
            status_code = status

            def json(self):
                return body

        return Answer()


@pytest.fixture
def onboarding(monkeypatch, settings):
    settings.ONBOARDING_API_URL = "http://onboarding.test"
    fake = Onboarding()
    monkeypatch.setattr(company_chart.requests, "request", fake.request)
    return fake


def test_a_cashier_views_and_the_call_names_the_company_and_the_person(shop, onboarding):
    entity = shop["entity"].id
    resp = call("get", ACCOUNTS, shop["cashier"], entity)
    assert (resp.status_code, resp.json()) == (200, {"accounts": []})
    method, path, body, claims = onboarding.asked[0]
    assert (method, path, body) == ("GET", "accounts", None)
    assert (claims["scope"], claims["entity_id"], claims["user_id"]) == (
        "company-chart", str(entity), str(shop["cashier"].id))


def test_changes_need_an_accountant_and_never_reach_onboarding_otherwise(shop, onboarding):
    entity = shop["entity"].id
    refused = call("post", CONTACTS, shop["manager"], entity, {"name": "Kee Wah"})
    assert (refused.status_code, refused.json()) == (
        403, {"error": "You don't have permission to add accounts and contacts here."})
    assert onboarding.asked == []

    onboarding.answer = (201, {"contact": {"name": "Kee Wah"}, "message": "Kee Wah added."})
    made = call("post", CONTACTS, shop["accountant"], entity, {"name": "Kee Wah", "entity_id": "ignored"})
    assert made.status_code == 201 and made.json()["message"] == "Kee Wah added."
    assert onboarding.asked[-1][:3] == ("POST", "contacts", {"name": "Kee Wah", "entity_id": "ignored"})
    # the company is the assertion's, never the body's
    assert onboarding.asked[-1][3]["entity_id"] == str(entity)


def test_onboardings_refusal_comes_back_as_it_is(shop, onboarding):
    account_id = uuid.uuid4()
    onboarding.answer = (403, {"error": "Wages comes with Minty, so it can't be deleted."})
    resp = call("delete", f"{ACCOUNTS}/{account_id}", shop["admin"], shop["entity"].id)
    assert (resp.status_code, resp.json()) == (403, {"error": "Wages comes with Minty, so it can't be deleted."})
    assert onboarding.asked[-1][:2] == ("DELETE", f"accounts/{account_id}")


def test_a_bad_id_a_stranger_and_a_wrong_method_stop_here(shop, onboarding):
    assert call("patch", f"{ACCOUNTS}/nope", shop["admin"], shop["entity"].id, {}).status_code == 404
    assert call("get", ACCOUNTS, shop["admin"], shop["other"].id).status_code == 200  # their other company
    from hub.tests.test_public_and_transport import make_user

    stranger = make_user("stranger@test.com")
    assert call("get", ACCOUNTS, stranger, shop["entity"].id).status_code == 403
    assert call("put", ACCOUNTS, shop["admin"], shop["entity"].id, {}).status_code == 405
    assert all(path != "accounts/nope" for _m, path, _b, _c in onboarding.asked)


def test_onboarding_unreachable_or_unset_is_said_and_logged(shop, onboarding, settings, caplog):
    onboarding.down = True
    resp = call("get", CONTACTS, shop["admin"], shop["entity"].id)
    assert (resp.status_code, resp.json()) == (503, {"error": company_chart.UNAVAILABLE})
    settings.ONBOARDING_API_URL = ""
    resp = call("get", CONTACTS, shop["admin"], shop["entity"].id)
    assert resp.status_code == 503
    assert "ONBOARDING_API_URL is not set" in caplog.text


# ---- opening General reads Xero's contacts (owner's decisions doc C2, 2026-11-19) ------------------


@pytest.fixture
def xero_read(monkeypatch):
    """The sync faked: each read stamps a Xero copy, as the real one does (``synced_at``)."""
    from django.utils import timezone
    from minty_db.models import XeroContact

    reads = []

    def fake_sync(entity_id, access_token, org):
        reads.append((entity_id, access_token, org))
        XeroContact.objects.update_or_create(entity_id=entity_id, xero_org_id=org, xero_guid="c-1",
                                             defaults={"name": "ABC", "synced_at": timezone.now()})
        return {"accounts": None, "contacts": {"added": 0}}

    monkeypatch.setattr(company_chart.xero_sync, "sync_company", fake_sync)
    monkeypatch.setattr(company_chart, "resolve_entity_access_token", lambda entity_id: ("tok", "org-1"))
    return reads


def test_the_contact_list_reads_xero_first_at_most_every_two_minutes(shop, onboarding, xero_read):
    from minty_db.models import Entity

    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(status="connected", xero_org_id="org-1")
    onboarding.answer = (200, {"contacts": []})
    assert call("get", CONTACTS, shop["cashier"], entity.id).status_code == 200
    assert call("get", CONTACTS, shop["cashier"], entity.id).status_code == 200
    assert len(xero_read) == 1, "once every 2 minutes"
    call("get", ACCOUNTS, shop["cashier"], entity.id)
    assert len(xero_read) == 1, "the accounts list doesn't read Xero"


def test_a_company_not_connected_never_reads_xero(shop, onboarding, xero_read):
    onboarding.answer = (200, {"contacts": []})
    assert call("get", CONTACTS, shop["cashier"], shop["entity"].id).status_code == 200
    assert xero_read == []


def test_xero_failing_still_shows_the_stored_contacts_and_is_logged(shop, onboarding, monkeypatch, caplog):
    from minty_db.models import Entity

    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(status="connected", xero_org_id="org-1")
    monkeypatch.setattr(company_chart, "resolve_entity_access_token", lambda entity_id: ("tok", "org-1"))

    def broken(*_a):
        raise RuntimeError("xero down")

    monkeypatch.setattr(company_chart.xero_sync, "sync_company", broken)
    onboarding.answer = (200, {"contacts": [{"name": "Kee Wah"}]})
    resp = call("get", CONTACTS, shop["cashier"], entity.id)
    assert (resp.status_code, resp.json()) == (200, {"contacts": [{"name": "Kee Wah"}]})
    assert "the Xero read for company" in caplog.text


@pytest.mark.django_db
def test_a_rename_in_settings_relabels_the_bills_on_it(shop, onboarding):
    """Records keep the id and show the name as it is now - their text copies too (the user,
    2026-11-27): an account or contact renamed in Settings rewrites the bill text on it
    (``bill_text``), as matching, merges and Xero renames do. A refused edit rewrites nothing."""
    from minty_db.models import AccountInfo, Bill, BillLine, XeroContactSync

    entity = shop["entity"]
    rent = AccountInfo.objects.create(entity_id=entity.id, name="Rent", code="469", xero_code="469", type="EXPENSE",
                                      account_type="expense", status="ACTIVE", source="user")
    kee = XeroContactSync.objects.create(entity_id=entity.id, name="Kee Wah", source="user")
    bill = Bill.objects.create(entity_id=entity.id, reference="B-1", contact_id=kee.id, contact_name="Kee Wah",
                               xero_account_code="469")
    line = BillLine.objects.create(bill=bill, account_id=rent.id, account_code="469", account_name="Rent")

    onboarding.answer = (409, {"error": "That code is already taken."})
    AccountInfo.objects.filter(id=rent.id).update(name="Office Rent")  # as if saved, but refused: no rewrite
    assert call("patch", f"{ACCOUNTS}/{rent.id}", shop["accountant"], entity.id, {"name": "x"}).status_code == 409
    assert BillLine.objects.get(id=line.id).account_name == "Rent"

    onboarding.answer = (200, {"message": "Office Rent saved."})  # onboarding-api saved the new name
    assert call("patch", f"{ACCOUNTS}/{rent.id}", shop["accountant"], entity.id, {"name": "x"}).status_code == 200
    assert BillLine.objects.get(id=line.id).account_name == "Office Rent"

    XeroContactSync.objects.filter(id=kee.id).update(name="Kee Wah Bakery")
    onboarding.answer = (200, {"message": "Kee Wah Bakery saved."})
    assert call("patch", f"{CONTACTS}/{kee.id}", shop["accountant"], entity.id, {"name": "x"}).status_code == 200
    assert Bill.objects.get(id=bill.id).contact_name == "Kee Wah Bakery"


def test_archive_and_restore_are_forwarded_with_their_own_rights(shop, onboarding):
    """Archive takes a row off the pickers - the delete right; restore brings one back - the add
    right (the user, 2026-11-27). Both are onboarding-api's; the hub opens the door."""
    entity, row = shop["entity"].id, uuid.uuid4()
    onboarding.answer = (200, {"message": "Rent archived."})
    for kind in ("accounts", "contacts"):
        for action in ("archive", "restore"):
            path = f"/api/me/company/{kind}/{row}/{action}"
            refused = call("post", path, shop["manager"], entity, {})
            assert refused.status_code == 403, (path, refused.content)
            done = call("post", path, shop["accountant"], entity, {})
            assert done.status_code == 200, (path, done.content)
            assert onboarding.asked[-1][:2] == ("POST", f"{kind}/{row}/{action}")
    assert call("get", f"{ACCOUNTS}/{row}/archive", shop["accountant"], entity).status_code == 405
    assert call("post", f"{ACCOUNTS}/not-an-id/archive", shop["accountant"], entity, {}).status_code == 404
