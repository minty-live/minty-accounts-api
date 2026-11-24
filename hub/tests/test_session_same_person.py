"""A Xero connect or reconnect keeps the person's live sign-in (the user, 2026-11-24).

A connect or reconnect signs its person in at the callback. A NEW sign-in there ended the one the
person was already in, so every tab and app still holding a token of it was refused ("its sign-in
has ended") and showed "Your session has expired" - and the OK there signed in again, ending the
other tabs' sign-in in turn (production, 24 Nov: eight Xero sign-ins in 75 minutes for one person).
Connect and reconnect (``keep_live``): the same person, still signed in, keeps that sign-in. A
sign-in by code or with Xero always starts a new one, as does anyone else or an ended sign-in."""

from __future__ import annotations

import os

import pytest
from django.contrib.sessions.models import Session

from hub import session
from hub.session_store import touch
from hub.tests import test_xero_oauth as oauth
from hub.tests.test_public_and_transport import make_user
from hub.tests.test_session import request_with_session
from hub.tests.test_xero_oauth import callback, connect_flow, fresh, reconnect_flow, state_of

shop, xero = oauth.shop, oauth.xero  # the fixtures
client_signed_in = oauth.signed_in

#: A connect and reconnect take the Xero sync's lock (``hashtextextended``), so they run on Postgres.
postgres_only = pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only (the sync lock)")


def signed_in(user):
    request = request_with_session()
    session.sign_in(request, user)
    request.session.save()
    return request, session.login_sid(request)


def test_keep_live_keeps_the_same_persons_sign_in_and_its_tokens(db):
    me = make_user()
    request, sid = signed_in(me)
    key = request.session.session_key
    request.session["terms_ok"] = True
    session.sign_in(request, me, keep_live=True)
    request.session.save()
    assert request.session.session_key == key and session.login_sid(request) == sid
    assert "terms_ok" not in request.session  # the Terms are still checked afresh
    assert touch(sid)  # tokens other tabs and apps hold still work


def test_a_sign_in_without_keep_live_always_starts_a_new_one(db):
    me = make_user()
    request, sid = signed_in(me)
    key = request.session.session_key
    session.sign_in(request, me)
    request.session.save()
    assert request.session.session_key != key and session.login_sid(request) != sid
    assert not touch(sid)


def test_keep_live_for_someone_else_ends_the_first_sign_in(db):
    first, second = make_user(), make_user("second@test.com")
    request, sid = signed_in(first)
    key = request.session.session_key
    session.sign_in(request, second, keep_live=True)
    request.session.save()
    assert request.session.session_key != key and session.login_sid(request) != sid
    assert session.signed_in_user(request) == second
    assert not touch(sid)


def test_keep_live_does_not_keep_a_sign_in_that_has_ended(db):
    me = make_user()
    request, sid = signed_in(me)
    Session.objects.filter(session_key=request.session.session_key).delete()  # e.g. 60 idle minutes
    stale = request_with_session(type(request.session)(request.session.session_key))
    session.sign_in(stale, me, keep_live=True)
    stale.session.save()
    assert session.login_sid(stale) != sid and touch(session.login_sid(stale))


# ---- the callbacks ------------------------------------------------------------------------------


@postgres_only
def test_a_connect_keeps_the_persons_sign_in(shop, xero):
    client = client_signed_in(shop["owner"])
    sid = client.session[session.LOGIN_SID_KEY]
    callback(client, connect_flow(client, shop["entity"]))
    assert fresh(shop["entity"]).xero_org_id == "t-1"  # connected, so the callback signed in
    assert client.session[session.LOGIN_SID_KEY] == sid and touch(sid)


@postgres_only
def test_a_reconnect_keeps_the_persons_sign_in(shop, xero):
    xero.tokens[str(shop["entity"].id)] = "GRANT"
    client = client_signed_in(shop["owner"])
    sid = client.session[session.LOGIN_SID_KEY]
    callback(client, reconnect_flow(client, shop["entity"]))
    assert fresh(shop["entity"]).status == "connected"
    assert client.session[session.LOGIN_SID_KEY] == sid and touch(sid)


@postgres_only
def test_a_sign_in_with_xero_starts_a_new_sign_in(shop, xero):
    client = client_signed_in(shop["owner"])
    sid = client.session[session.LOGIN_SID_KEY]
    callback(client, state_of(client.get("/xero_auth")))
    assert client.session[session.LOGIN_SID_KEY] != sid and not touch(sid)
