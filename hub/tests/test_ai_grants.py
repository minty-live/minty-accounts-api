"""AI allowances for Minty support (``hub/ai_grants.py``, owner 2026-11-18): every superuser sees
each company's AI grants and use and gives a company a grant; nobody else sees or gives anything.
The allowance rules themselves (charging, the default grant, the lock) are minty-petty-cash-api's
tests and minty-shared-py's ``tests/test_ai_allowance.py``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from minty_db.models import AiGrant, AiPolicy, AiRead, Entity
from minty_shared.django import ai_allowance

from hub.tests.test_archive import archive, superuser
from hub.tests.test_company import call

LIST = "/api/me/ai/companies"


def detail(entity_id):
    return f"/api/me/ai/companies/{entity_id}"


def grants(entity_id):
    return f"/api/me/ai/companies/{entity_id}/grants"


@pytest.fixture
def policy(db):
    return AiPolicy.objects.create(id=1, primary_model="gemini-3.8-flash", backup_model=None,
                                   default_grant={"tokens": 20000}, limit_rules=[])


def read(entity, grant=None, *, tokens=5000, status="ok", module="petty_cash"):
    return AiRead.objects.create(entity_id=entity.id, grant_id=grant.id if grant else None, module=module,
                                 status=status, tokens_in=tokens, model_answered="gemini-3.8-flash",
                                 attempts="gemini-3.8-flash:ok")


# ---- who ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["admin", "accountant", "cashier"])
def test_only_a_superuser_sees_or_gives(shop, policy, who):
    person = shop[who]
    assert call("get", LIST, person).status_code == 403
    assert call("get", detail(shop["entity"].id), person).status_code == 403
    refused = call("post", grants(shop["entity"].id), person, body={"tokens": 50000})
    assert refused.status_code == 403 and refused.json() == {"error": "Only Minty support can manage AI allowances."}
    assert not AiGrant.objects.exists()


def test_no_token_is_unauthorized(shop, policy):
    assert call("get", LIST, None).status_code == 401


# ---- the list ----------------------------------------------------------------------------------


def test_the_list_shows_each_company_s_allowance(shop, policy):
    spent = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=20000, source="default")
    read(shop["entity"], spent, tokens=5000)
    read(shop["entity"], spent, tokens=9000, status="failed")  # a failed read is not charged

    resp = call("get", LIST, superuser())

    assert resp.status_code == 200
    rows = {r["name"]: r for r in resp.json()["companies"]}
    mine, other = rows["Olive & Vine"], rows["Other Co"]
    assert (mine["percent_left"], mine["tokens_used"], mine["tokens_given"]) == (75, 5000, 20000)
    assert (mine["has_grant"], mine["reads"], mine["blocked"]) == (True, 1, False)
    assert mine["last_read_at"] is not None
    # never given a grant: the default it will get on its first read
    assert (other["percent_left"], other["tokens_given"], other["has_grant"]) == (100, 20000, False)
    assert resp.json()["total"] == 2


def test_the_list_finds_a_company_by_name_and_leaves_archived_ones_out(shop, policy):
    boss = superuser()
    assert [r["name"] for r in call("get", LIST + "?q=vine", boss).json()["companies"]] == ["Olive & Vine"]
    archive(shop["other"])
    assert [r["name"] for r in call("get", LIST, boss).json()["companies"]] == ["Olive & Vine"]


def test_the_entity_list_asks_for_exactly_the_companies_it_shows(shop, policy):
    boss = superuser()
    resp = call("get", f"{LIST}?ids={shop['other'].id}", boss)
    assert [r["name"] for r in resp.json()["companies"]] == ["Other Co"] and resp.json()["total"] == 1
    assert call("get", f"{LIST}?ids=not-an-id", boss).status_code == 422


def test_a_used_up_company_reads_zero_and_blocked(shop, policy):
    spent = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=20000, source="default")
    read(shop["entity"], spent, tokens=21000)

    row = next(r for r in call("get", LIST, superuser()).json()["companies"] if r["name"] == "Olive & Vine")
    assert (row["percent_left"], row["blocked"], row["tokens_given"]) == (0, True, None)


def test_without_a_usable_policy_the_page_says_reading_is_off(shop, policy):
    AiPolicy.objects.all().delete()
    resp = call("get", LIST, superuser())
    assert resp.status_code == 503 and "AI reading is off" in resp.json()["error"]


# ---- one company -------------------------------------------------------------------------------


def test_a_company_shows_its_grants_newest_first_and_its_reads(shop, policy):
    first = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=20000, source="default",
                                   granted_by="ai_policy")
    read(shop["entity"], first, tokens=20500)
    AiGrant.objects.filter(id=first.id).update(created_at=datetime.now(UTC) - timedelta(days=2))
    AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=50000, source="manual",
                           granted_by="boss@test.com", note="pilot")

    body = call("get", detail(shop["entity"].id), superuser()).json()

    assert body["company"] == {"id": str(shop["entity"].id), "name": "Olive & Vine"}
    assert body["allowance"]["percent_left"] == 100 and body["allowance"]["blocked"] is False
    assert body["default_grant"] == {"tokens": 20000, "reads": None, "days": None}
    newest, oldest = body["grants"]
    assert (newest["source"], newest["tokens_total"], newest["open"], newest["note"]) == ("manual", 50000, True, "pilot")
    assert (oldest["source"], oldest["tokens_used"], oldest["open"]) == ("default", 20500, False)
    [last] = body["reads"]
    assert (last["module"], last["status"], last["tokens"], last["model"]) == ("petty_cash", "ok", 20500,
                                                                                "gemini-3.8-flash")


def reads_after(entity_id, before):
    return f"/api/me/ai/companies/{entity_id}/reads?before={before}"


def test_its_reads_come_twenty_at_a_time_and_show_more_reaches_the_older_ones(shop, policy):
    # owner, 2026-11-19: "Show more" instead of a fixed 20. Reads can share a created_at, so the id
    # breaks the tie - the cursor holds both.
    for _ in range(25):
        read(shop["entity"], tokens=100)
    boss = superuser()

    body = call("get", detail(shop["entity"].id), boss).json()
    assert len(body["reads"]) == 20 and body["reads_more"] is True
    first_page = [r["id"] for r in body["reads"]]

    more = call("get", reads_after(shop["entity"].id, first_page[-1]), boss).json()
    assert len(more["reads"]) == 5 and more["more"] is False
    rest = [r["id"] for r in more["reads"]]
    expected = [str(r.id) for r in AiRead.objects.filter(entity_id=shop["entity"].id).order_by("-created_at", "-id")]
    assert first_page + rest == expected


def test_a_short_history_says_there_is_nothing_more(shop, policy):
    read(shop["entity"])
    body = call("get", detail(shop["entity"].id), superuser()).json()
    assert len(body["reads"]) == 1 and body["reads_more"] is False


@pytest.mark.parametrize("before", ["not-a-read", "6d8f2c9a-0000-4000-8000-000000000000", "other"])
def test_show_more_only_follows_a_read_of_that_company(shop, policy, before):
    if before == "other":
        before = str(read(shop["other"]).id)
    assert call("get", reads_after(shop["entity"].id, before), superuser()).status_code == 404


@pytest.mark.parametrize("who", ["admin", "accountant", "cashier"])
def test_only_a_superuser_sees_more_reads(shop, policy, who):
    mine = read(shop["entity"])
    assert call("get", reads_after(shop["entity"].id, mine.id), shop[who]).status_code == 403


def test_an_unknown_company_is_not_found(shop, policy):
    boss = superuser()
    assert call("get", detail("not-a-company"), boss).status_code == 404
    assert call("post", grants("6d8f2c9a-0000-4000-8000-000000000000"), boss,
                body={"tokens": 5}).status_code == 404


# ---- giving a grant ----------------------------------------------------------------------------


def test_a_superuser_gives_a_grant_and_the_company_can_read_again(shop, policy):
    spent = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=20000, source="default")
    read(shop["entity"], spent, tokens=20500)
    boss = superuser()

    resp = call("post", grants(shop["entity"].id), boss,
                body={"tokens": "50,000", "reads": "", "days": 90, "note": " pilot "})

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["message"] == "Grant given to Olive & Vine."
    assert (body["allowance"]["percent_left"], body["allowance"]["blocked"]) == (100, False)
    given = AiGrant.objects.get(entity_id=shop["entity"].id, source="manual")
    assert (given.tokens_total, given.reads_total, given.granted_by, given.note) == (50000, None, "boss@test.com",
                                                                                     "pilot")
    assert timedelta(days=89) < given.expires_at - datetime.now(UTC) <= timedelta(days=90)


def test_a_grant_of_reads_only_with_no_end_date(shop, policy):
    resp = call("post", grants(shop["other"].id), superuser(), body={"reads": 10})
    assert resp.status_code == 201
    given = AiGrant.objects.get(entity_id=shop["other"].id)
    assert (given.tokens_total, given.reads_total, given.expires_at) == (None, 10, None)


@pytest.mark.parametrize("body, fields", [
    ({}, {"tokens": "The grant gives nothing yet. Add tokens, reads or both to continue."}),
    ({"tokens": "lots"}, {"tokens": "Enter the tokens as a whole number from 1 to 10,000,000."}),
    ({"tokens": 0}, {"tokens": "Enter the tokens as a whole number from 1 to 10,000,000."}),
    ({"tokens": 10.5}, {"tokens": "Enter the tokens as a whole number from 1 to 10,000,000."}),
    ({"reads": 10_001}, {"reads": "Enter the reads as a whole number from 1 to 10,000."}),
    ({"tokens": 5, "days": -1},
     {"days": "Enter the days as a whole number from 1 to 3,650, or leave it empty for no end date."}),
    ({"tokens": 5, "note": "x" * 256}, {"note": "Shorten the note to 255 characters or fewer."}),
])
def test_a_grant_the_form_should_have_caught_is_refused_per_field(shop, policy, body, fields):
    resp = call("post", grants(shop["entity"].id), superuser(), body=body)
    assert resp.status_code == 422
    assert resp.json() == {"error": "The grant wasn't given. Fix the fields below and try again.", "fields": fields}
    assert not AiGrant.objects.exists()


def test_only_post_gives(shop, policy):
    assert call("put", grants(shop["entity"].id), superuser(), body={"tokens": 5}).status_code == 405
    assert Entity.objects.count() == 2 and not AiGrant.objects.exists()


# ---- ending a grant (owner, 2026-11-20) ----------------------------------------------------------


def end(entity_id, grant_id):
    return f"/api/me/ai/companies/{entity_id}/grants/{grant_id}/end"


def test_a_superuser_ends_a_grant_and_it_stops_counting_at_once(shop, policy):
    grant = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=50000, source="manual",
                                   granted_by="boss@test.com")
    boss = superuser()

    resp = call("post", end(shop["entity"].id, grant.id), boss)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["message"] == "Grant ended."
    [shown] = body["grants"]
    assert shown["open"] is False and shown["ended_by"] == boss.email and shown["ended_at"]
    assert shown["expires_at"] == shown["ended_at"]
    assert body["allowance"]["blocked"] is True and body["allowance"]["percent_left"] == 0
    grant.refresh_from_db()
    assert grant.ended_by == boss.email and grant.ended_at == grant.expires_at


def test_an_ended_grant_still_keeps_the_default_from_coming_back(shop, policy):
    first = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=20000, source="default",
                                   granted_by="ai_policy")
    call("post", end(shop["entity"].id, first.id), superuser())

    allowance = ai_allowance.status(shop["entity"].id, ai_allowance.load_policy())

    assert allowance.blocked is True  # no fresh default grant: it is given once, ever
    assert AiGrant.objects.filter(entity_id=shop["entity"].id).count() == 1


def test_a_grant_already_ended_or_expired_is_not_ended_again(shop, policy):
    boss = superuser()
    grant = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=5000, source="manual")
    assert call("post", end(shop["entity"].id, grant.id), boss).status_code == 200
    again = call("post", end(shop["entity"].id, grant.id), boss)
    assert again.status_code == 409 and again.json() == {"error": "This grant has already ended."}

    expired = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=5000, source="manual",
                                     expires_at=datetime.now(UTC) - timedelta(days=1))
    assert call("post", end(shop["entity"].id, expired.id), boss).status_code == 409


@pytest.mark.parametrize("which", ["unknown", "not-a-uuid", "other"])
def test_only_a_grant_of_that_company_is_ended(shop, policy, which):
    grant_id = {"unknown": "6d8f2c9a-0000-4000-8000-000000000000", "not-a-uuid": "x"}.get(which)
    if which == "other":
        grant_id = AiGrant.objects.create(entity_id=shop["other"].id, tokens_total=5000, source="manual").id
    resp = call("post", end(shop["entity"].id, grant_id), superuser())
    assert resp.status_code == 404 and resp.json() == {"error": "I couldn't find that grant."}


@pytest.mark.parametrize("who", ["admin", "accountant", "cashier"])
def test_only_a_superuser_ends_a_grant(shop, policy, who):
    grant = AiGrant.objects.create(entity_id=shop["entity"].id, tokens_total=5000, source="manual")
    assert call("post", end(shop["entity"].id, grant.id), shop[who]).status_code == 403
    assert call("get", end(shop["entity"].id, grant.id), superuser()).status_code == 405
    grant.refresh_from_db()
    assert grant.ended_at is None
