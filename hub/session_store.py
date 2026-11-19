"""The hub session's store and its touch - sign-out after 60 minutes without activity (Phase 9,
owner's S1-S3, 2026-11-19).

**The idle limit.** ``SESSION_COOKIE_AGE`` (60 min) is the ROW's life: ``django_session.expire_date``
is pushed 60 minutes on at every save (the front door's presence refresh, at most once a minute)
and every touch below. The browser's cookie lives longer (``COOKIE_SECONDS``): activity in another
app extends the row but cannot re-set the cookie, so a cookie that lapsed first would end a live
sign-in. The row decides; a cookie naming an expired row is nobody.

**The sid.** Every sign-in has a random 128-bit ``login_sid`` (``hub/session.py``), and every
hand-off token carries it as ``sid``. The session's KEY is derived from it -
``session_key_for(sid)``, HMAC-SHA256 over ``SECRET_KEY`` - so this service finds the row from a
token's ``sid`` with one primary-key read and no table of its own. A token never carries the key
(the cookie's value): holding a token for one app for 30 minutes must not let anyone become the
person in the hub. Going from ``sid`` to key needs ``SECRET_KEY``, which already mints every token.

**The touch.** ``POST /api/internal/session/touch {"sid"}`` - a Minty backend's service assertion,
scoped ``session-touch`` (``minty_shared.tokens.session_alive``, at most once a minute per sid per
process): 200 ``{"alive": true}`` and the row's expiry pushed on (the data is never rewritten, so a
hub request saving at the same moment loses nothing); 410 when the sign-in has ended - signed out,
60 idle minutes, the person gone or switched off, or no such sign-in. The caller then refuses the
token (401), and each app's 401 path hands the person over again, which reaches the sign-in page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import jwt
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as DbSessionStore
from django.contrib.sessions.models import Session
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.utils import timezone
from minty_db.models import User
from minty_shared import tokens

from core.flask_json import flask_json

logger = logging.getLogger("minty-accounts-api")

#: How long the browser keeps the cookie; the row's expiry (60 idle minutes) is what ends a sign-in.
COOKIE_SECONDS = 24 * 60 * 60
KEY_SALT = b"minty-hub-session:"
#: A ``login_sid`` is 32 hex characters; anything much longer is not one.
SID_MAX_LENGTH = 64
ENDED = "This sign-in has ended."
NO_SID = "The sign-in id is missing. Send the token's sid to continue."


def session_key_for(sid: str) -> str:
    """The session key of the sign-in ``sid`` names (32 hex characters, Django's length)."""
    digest = hmac.new(settings.SECRET_KEY.encode(), KEY_SALT + sid.encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


class SessionStore(DbSessionStore):
    """Django's database session, with a key that can be bound to a sign-in's sid."""

    _bound_key: str | None = None

    def bind(self, sid: str) -> None:
        """A new key derived from ``sid``, keeping the data (``cycle_key``, so no fixation)."""
        self._bound_key = session_key_for(sid)
        self.cycle_key()
        if self.session_key != session_key_for(sid):
            raise RuntimeError("session: the bound key was taken; the sign-in was not bound")

    def _get_new_session_key(self):
        key, self._bound_key = self._bound_key, None
        return key or super()._get_new_session_key()

    def get_expiry_age(self, **kwargs):
        """The COOKIE's max-age (``SessionMiddleware`` is its only reader here); the row's expiry
        is ``get_expiry_date``, 60 minutes on from the last save."""
        if "expiry" not in kwargs and not self.get("_session_expiry"):
            return COOKIE_SECONDS
        return super().get_expiry_age(**kwargs)


def touch(sid: str) -> bool:
    """Keep the sign-in ``sid`` names alive: True, its row's expiry pushed on; False when it has
    ended (no live row, another sign-in's row, or the person gone or switched off)."""
    key = session_key_for(sid)
    now = timezone.now()
    row = Session.objects.filter(session_key=key, expire_date__gt=now).first()
    if row is None:
        return False
    data = SessionStore().decode(row.session_data)
    if data.get("login_sid") != sid:
        return False
    user_id = data.get("user_id")
    if not user_id or not User.objects.filter(id=user_id, approved=True).exists():
        return False
    Session.objects.filter(session_key=key).update(expire_date=now + timedelta(seconds=settings.SESSION_COOKIE_AGE))
    return True


def _authenticated(request: HttpRequest) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    try:
        claims = tokens.decode_bearer(header[7:].strip(), settings.SECRET_KEY, options={"require": ["exp", "scope"]})
    except jwt.PyJWTError:
        return False
    return claims.get("scope") == tokens.SESSION_TOUCH_SCOPE


def internal_session_touch(request: HttpRequest) -> HttpResponse:
    """``POST /api/internal/session/touch`` (see the module note)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _authenticated(request):
        logger.warning("session touch: rejected caller")
        return flask_json({"error": "Unauthorized"}, 401)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = None
    sid = body.get("sid") if isinstance(body, dict) else None
    if not isinstance(sid, str) or not sid or len(sid) > SID_MAX_LENGTH:
        return flask_json({"error": NO_SID, "fields": {"sid": NO_SID}}, 422)
    if not touch(sid):
        logger.info("session touch: the sign-in has ended")
        return flask_json({"error": ENDED}, 410)
    return flask_json({"alive": True})
