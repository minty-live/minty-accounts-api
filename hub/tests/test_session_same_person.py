"""Signing in again as the person already signed in keeps that sign-in (2026-11-24).

A Xero connect or reconnect signs its person in at the callback. When the same person was already
signed in, a NEW sign-in ended the old one, so every tab and app still holding a token of the old
sign-in was refused ("its sign-in has ended") and showed "Your session has expired" - and the OK
there signed in again, ending the other tabs' sign-in in turn (production, 24 Nov: eight Xero
sign-ins in 75 minutes for one person). Same person and a live sign-in: kept. Anyone else, or a
sign-in that has ended: a new one, as before (no fixation)."""

from __future__ import annotations

from django.contrib.sessions.models import Session

from hub import session
from hub.session_store import touch
from hub.tests.test_public_and_transport import make_user
from hub.tests.test_session import request_with_session


def signed_in(user):
    request = request_with_session()
    session.sign_in(request, user)
    request.session.save()
    return request, session.login_sid(request)


def test_signing_in_again_as_the_same_person_keeps_the_sign_in_and_its_tokens(db):
    me = make_user()
    request, sid = signed_in(me)
    key = request.session.session_key
    request.session["terms_ok"] = True
    session.sign_in(request, me)
    request.session.save()
    assert request.session.session_key == key and session.login_sid(request) == sid
    assert "terms_ok" not in request.session  # the Terms are still checked afresh
    assert touch(sid)  # tokens other tabs and apps hold still work


def test_signing_in_as_someone_else_ends_the_first_sign_in(db):
    first, second = make_user(), make_user("second@test.com")
    request, sid = signed_in(first)
    key = request.session.session_key
    session.sign_in(request, second)
    request.session.save()
    assert request.session.session_key != key and session.login_sid(request) != sid
    assert session.signed_in_user(request) == second
    assert not touch(sid)


def test_a_sign_in_that_has_ended_is_not_kept(db):
    me = make_user()
    request, sid = signed_in(me)
    Session.objects.filter(session_key=request.session.session_key).delete()  # e.g. 60 idle minutes
    stale = request_with_session(type(request.session)(request.session.session_key))
    session.sign_in(stale, me)
    stale.session.save()
    assert session.login_sid(stale) != sid and touch(session.login_sid(stale))
