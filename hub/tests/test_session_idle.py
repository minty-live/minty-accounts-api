"""Sign-out after 60 minutes without activity (Phase 9, owner's S1-S3, 2026-11-19): the hub session's
idle limit, the ``sid`` every hand-off token carries, ``POST /api/internal/session/touch`` (how the
other APIs keep a session alive and learn it has ended), and the hub's own bearer routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as PlainStore
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone
from minty_db.models import User
from minty_shared import tokens

from hub import handoffs, parked, session
from hub.session_store import SessionStore, session_key_for
from hub.tests.test_handoffs import claims, landing, login, shop  # noqa: F401 - shop is the fixture
from hub.tests.test_public_and_transport import make_user, token
from hub.tests.test_session import request_with_session

TOUCH = "/api/internal/session/touch"


def signed_in_store(user: User) -> tuple[SessionStore, str]:
    """A signed-in session made the way ``sign_in`` makes one; ``(store, sid)``."""
    request = request_with_session()
    session.sign_in(request, user)
    request.session.save()
    return request.session, session.login_sid(request)


def assertion(scope=tokens.SESSION_TOUCH_SCOPE, secret=None, **claims):
    return tokens.service_assertion(secret or settings.SECRET_KEY, scope, "", **claims)


def touch(sid, *, auth=None, body=None):
    headers = {} if auth is False else {"HTTP_AUTHORIZATION": f"Bearer {auth or assertion()}"}
    payload = json.dumps({"sid": sid} if body is None else body)
    return Client().post(TOUCH, payload, content_type="application/json", **headers)


def expiry(store) -> datetime:
    return Session.objects.get(session_key=store.session_key).expire_date


# ---- the idle limit ------------------------------------------------------------------------------


def test_a_session_ends_after_60_minutes_without_activity():
    assert settings.SESSION_COOKIE_AGE == 60 * 60
    assert settings.SESSION_ENGINE == "hub.session_store"


def test_the_row_expires_60_minutes_after_the_last_activity_while_the_cookie_outlives_it(db):
    store, _sid = signed_in_store(make_user())
    left = expiry(store) - timezone.now()
    assert timedelta(minutes=59) < left <= timedelta(minutes=60)
    # The browser keeps the cookie longer, so activity in another app (which extends the row,
    # never the cookie) is not undone by the cookie lapsing first; the row decides.
    assert store.get_expiry_age() == 24 * 60 * 60


def test_signing_in_binds_the_session_key_to_the_sign_ins_sid(db):
    store, sid = signed_in_store(make_user())
    assert store.session_key == session_key_for(sid) != sid
    assert len(store.session_key) == 32 and session_key_for(sid) == session_key_for(sid)


def test_the_key_needs_the_secret(db, settings):
    before = session_key_for("abc")
    settings.SECRET_KEY = "another-secret-another-secret-another"
    assert session_key_for("abc") != before


# ---- POST /api/internal/session/touch ------------------------------------------------------------


def test_a_live_session_is_kept_alive(db):
    store, sid = signed_in_store(make_user())
    Session.objects.filter(session_key=store.session_key).update(expire_date=timezone.now() + timedelta(minutes=2))
    resp = touch(sid)
    assert (resp.status_code, resp.json()) == (200, {"alive": True})
    assert expiry(store) - timezone.now() > timedelta(minutes=59)
    # The data is untouched: only the expiry moves.
    assert SessionStore(store.session_key).load()[session.USER_KEY] == str(store[session.USER_KEY])


@pytest.mark.parametrize("ending", ["signed_out", "idle", "switched_off", "never"])
def test_a_session_that_has_ended_answers_410(db, ending):
    me = make_user()
    store, sid = signed_in_store(me)
    if ending == "signed_out":
        Session.objects.filter(session_key=store.session_key).delete()
    elif ending == "idle":
        Session.objects.filter(session_key=store.session_key).update(expire_date=timezone.now() - timedelta(seconds=1))
    elif ending == "switched_off":
        User.objects.filter(id=me.id).update(approved=False)
    else:
        sid = "0" * 32
    resp = touch(sid)
    assert resp.status_code == 410 and resp.json() == {"error": "This sign-in has ended."}


def test_a_session_without_the_same_sid_inside_is_not_this_sign_in(db):
    store, sid = signed_in_store(make_user())
    store[session.LOGIN_SID_KEY] = "someone-else"
    store.save()
    assert touch(sid).status_code == 410


@pytest.mark.parametrize("auth", [
    False, "garbage", "wrong-scope", "wrong-secret", "expired", "a-person's-token"])
def test_only_a_minty_backend_may_touch(db, auth):
    store, sid = signed_in_store(make_user())
    before = expiry(store)
    value = {
        False: False, "garbage": "garbage",
        "wrong-scope": assertion(scope="xero-sync"),
        "wrong-secret": assertion(secret="not-the-key-not-the-key-not-the-key!"),
        "expired": jwt.encode({"scope": tokens.SESSION_TOUCH_SCOPE, "entity_id": "",
                               "exp": datetime.now(UTC) - timedelta(minutes=5)}, settings.SECRET_KEY,
                              algorithm="HS256"),
        "a-person's-token": token(store[session.USER_KEY], sid=sid),
    }[auth]
    resp = touch(sid, auth=value)
    assert resp.status_code == 401 and resp.json() == {"error": "Unauthorized"}
    assert expiry(store) == before


@pytest.mark.parametrize("body", [{}, {"sid": ""}, {"sid": 5}, {"sid": "x" * 200}, ["sid"]])
def test_a_missing_or_malformed_sid_is_refused(db, body):
    resp = touch(None, body=body)
    assert resp.status_code == 422 and resp.json()["error"]


def test_the_touch_is_post_only(db):
    assert Client().get(TOUCH, HTTP_AUTHORIZATION=f"Bearer {assertion()}").status_code == 405


# ---- the hand-off tokens' sid --------------------------------------------------------------------


def test_the_onboarding_token_names_the_sign_in_too(db):
    client = login(Client(), make_user(), sid="onb-sid")
    query = parse_qs(urlsplit(client.get("/entity/create")["Location"]).query)
    data = parked.redeem(query["code"][0], salt=handoffs.HANDOFF_CODE_SALT, max_age=handoffs.CODE_MAX_AGE,
                         what="test")
    minted = jwt.decode(data["token"], settings.SECRET_KEY, algorithms=["HS256"])
    assert (minted["scope"], minted["sid"]) == ("onboarding", "onb-sid")
    assert minted["exp"] - minted["iat"] == 30 * 60  # every app: 30 minutes, renewed quietly


def test_a_session_from_before_the_binding_is_bound_at_its_next_hand_off(shop):  # noqa: F811
    client = login(Client(), shop["owner"], sid="old-sid")
    store = PlainStore()  # a random key, as every session made before Phase 9
    store.update({session.USER_KEY: str(shop["owner"].id), session.LOGIN_SID_KEY: "old-sid"})
    store.create()
    client.cookies["session"] = old_key = store.session_key
    resp = client.get("/handoff/petty-cash-web", {"entity_id": str(shop["entity"].id)})
    minted = claims(landing(resp, settings.PETTY_CASH_WEB_URL))
    assert minted["sid"] != "old-sid" and resp.cookies["session"].value == session_key_for(minted["sid"])
    assert not PlainStore().exists(old_key)
    assert touch(minted["sid"]).status_code == 200


# ---- the hub's own bearer routes -----------------------------------------------------------------


def test_a_hub_call_whose_sign_in_has_ended_is_401(db):
    me = make_user()
    store, sid = signed_in_store(me)
    Session.objects.filter(session_key=store.session_key).delete()
    resp = Client().get("/api/me/terms", HTTP_AUTHORIZATION=f"Bearer {token(me.id, sid=sid)}")
    # The reason (owner, 2026-11-19): minty-web says "Your session has expired", never renews.
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized", "reason": "session_expired"}


def expired_token(user_id, **claims) -> str:
    payload = {"user_id": str(user_id), "exp": datetime.now(UTC) - timedelta(hours=1), **claims}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def test_an_expired_hub_token_whose_sign_in_has_ended_says_so(db):
    me = make_user()
    store, sid = signed_in_store(me)
    Session.objects.filter(session_key=store.session_key).update(expire_date=timezone.now() - timedelta(minutes=1))
    resp = Client().get("/api/me/terms", HTTP_AUTHORIZATION=f"Bearer {expired_token(me.id, sid=sid)}")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized", "reason": "session_expired"}


def test_an_expired_hub_token_whose_sign_in_lives_is_renewed_quietly(db):
    me = make_user()
    _store, sid = signed_in_store(me)
    resp = Client().get("/api/me/terms", HTTP_AUTHORIZATION=f"Bearer {expired_token(me.id, sid=sid)}")
    assert resp.status_code == 401 and resp.json() == {"error": "unauthorized"}


def test_the_onboarding_hub_door_says_so_too(db):
    me = make_user()
    store, sid = signed_in_store(me)
    Session.objects.filter(session_key=store.session_key).delete()
    resp = Client().post("/api/onboarding/modules", "{}", content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token(me.id, sid=sid)}")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized", "reason": "session_expired"}


# ---- the front door after the sign-in has ended (owner, 2026-11-19) ------------------------------


def door_and_sign_in_page(client, path):
    """The hand-off's redirect to the front door, and the front door's to the hub's sign-in page."""
    door = client.get(path)
    assert door.status_code == 302 and door["Location"].startswith("/?"), door["Location"]
    page = urlsplit(client.get(door["Location"])["Location"])
    assert f"{page.scheme}://{page.netloc}{page.path}" == settings.MINTY_WEB_URL + "/login"
    return parse_qs(urlsplit(door["Location"]).query), parse_qs(page.query)


def test_a_hand_off_to_a_sign_in_that_has_ended_tells_the_sign_in_page(shop):  # noqa: F811
    client = login(Client(), shop["owner"], sid="idle-sid")
    Session.objects.filter(session_key=session_key_for("idle-sid")).update(
        expire_date=timezone.now() - timedelta(minutes=1))  # 60 minutes without activity
    path = f"/handoff/petty-cash-web?entity_id={shop['entity'].id}&next=%2Fx"
    door, page = door_and_sign_in_page(client, path)
    assert door["expired"] == ["1"] and page["expired"] == ["1"]
    assert page["next"] == [path]
    assert "flash" not in page  # the pop-up says it; no "Please log in" as well


def test_a_hand_off_with_no_sign_in_before_is_not_expired(shop):  # noqa: F811
    door, page = door_and_sign_in_page(Client(), f"/handoff/petty-cash-web?entity_id={shop['entity'].id}")
    assert "expired" not in door and "expired" not in page and "flash" in page


def test_a_live_session_that_never_held_a_sign_in_is_not_expired(db):
    client = Client()
    store = SessionStore()
    store.update({"_flashes": []})
    store.create()
    client.cookies["session"] = store.session_key
    door, page = door_and_sign_in_page(client, "/handoff/minty-web")
    assert "expired" not in door and "expired" not in page


def test_signed_in_the_front_door_follows_next(shop):  # noqa: F811
    # The apps' "Your session has expired" OK goes to the front door with the hand-off as next;
    # someone who signed in again meanwhile (another tab) goes straight back.
    client = login(Client(), shop["owner"])
    assert client.get("/?next=/handoff/minty-web%3Fnext%3D%252Fx")["Location"] == "/handoff/minty-web?next=%2Fx"
    assert client.get("/?next=//evil.example/x")["Location"] == "/entity"


def test_a_hub_call_is_activity(db):
    me = make_user()
    store, sid = signed_in_store(me)
    Session.objects.filter(session_key=store.session_key).update(expire_date=timezone.now() + timedelta(minutes=2))
    resp = Client().get("/api/me/terms", HTTP_AUTHORIZATION=f"Bearer {token(me.id, sid=sid)}")
    assert resp.status_code == 200
    assert expiry(store) - timezone.now() > timedelta(minutes=59)


def test_a_token_from_before_the_sid_still_opens_the_hub_until_it_expires(db):
    me = make_user()
    resp = Client().get("/api/me/terms", HTTP_AUTHORIZATION=f"Bearer {token(me.id)}")
    assert resp.status_code == 200
