"""``POST /api/internal/xero/token`` - Flask's route of the same path, moved (Phase 2, 2026-11-05).

The caller is a Minty backend, not a browser: it presents a short-lived HS256 assertion signed
with the shared SECRET_KEY, scoped ``xero-access-token``. The company comes from the signed
claims, never the body, so a leaked assertion cannot be pointed at another company.

The answers are Flask's ``jsonify`` byte for byte (sorted keys, compact, a trailing newline): the
callers' ``main`` branches read them today.

SECURITY: this hands out live Xero access tokens; nothing but another Minty backend should reach it.
"""

from __future__ import annotations

import json
import logging
import uuid

import jwt
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from minty_shared import logctx, tokens

from core.flask_json import flask_json
from xero.services.sync import read_recently, sync_company
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

SCOPE = "xero-access-token"
SYNC_SCOPE = "xero-sync"


def _authenticate(request: HttpRequest, scope: str = SCOPE) -> tuple[dict | None, str | None]:
    """``(claims, None)`` for a valid assertion for ``scope`` (each route has its own), else
    ``(None, reason)``."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "missing bearer token"
    try:
        claims = tokens.decode_bearer(header[7:].strip(), settings.SECRET_KEY,
                                      options={"require": ["exp", "scope", "entity_id"]})
    except jwt.ExpiredSignatureError:
        return None, "token expired"
    except jwt.InvalidTokenError as exc:
        return None, f"invalid token: {exc}"
    if claims.get("scope") != scope:
        return None, "wrong scope"
    # A Minty backend's own assertion: its company (and person, when it names one) go on the
    # log line, with the caller's trace.
    logctx.bind(user=claims.get("user_id"), entity=claims["entity_id"])
    return claims, None


def internal_xero_access_token(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claims, error = _authenticate(request)
    if error:
        logger.warning("internal xero token: rejected caller (%s)", error)
        return flask_json({"status": "error", "message": "unauthorized"}, 401)

    raw_entity_id = claims["entity_id"]
    logger.info("internal xero token: request for entity %s", raw_entity_id)
    try:
        entity_id = uuid.UUID(str(raw_entity_id))
    except ValueError:
        # Signed by a Minty backend, so not an attack - a caller bug. No such company.
        logger.error("internal xero token: entity_id %r is not a uuid", raw_entity_id)
        result = None
    else:
        result = resolve_entity_access_token(entity_id)
    if not result:
        # Not a server error: the connection needs a person to reconnect, or a concurrent refresh
        # is still in flight. Callers show a reconnect prompt rather than send Xero a dead token.
        logger.info("internal xero token: no usable token for entity %s", raw_entity_id)
        return flask_json({"status": "reconnect_required",
                           "message": "No valid Xero token for this entity"}, 409)
    access_token, xero_org_id = result
    logger.info("internal xero token: issued for entity %s", raw_entity_id)
    return flask_json({"access_token": access_token, "xero_org_id": xero_org_id}, 200)


def internal_xero_sync(request: HttpRequest) -> HttpResponse:
    """``POST /api/internal/xero/sync`` (Phase 3, 2026-11-06): bring the company's Xero accounts and
    contacts into Minty's tables now (``xero/services/sync.py``) - the one sync, asked for by
    Petty Cash Settings (and, as their routes move, onboarding and Payment Requests).

    The caller's 60-second assertion is scoped ``xero-sync``; the company is its ``entity_id``. 200
    ``{"accounts": counts | null, "contacts": counts | null}`` - null when Xero could not be asked
    for that half (nothing changed); 409 ``{"error", "reconnect": true}`` when there is no usable
    token; 401 for a bad assertion. It only reads Xero (owner's decisions doc C2, 2026-11-19).

    A body ``{"throttle": true}`` - opening a Settings page - skips the read when the company's
    contacts were read under 2 minutes ago (``read_recently``): 200 ``{"accounts": null,
    "contacts": null, "skipped": true}``.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claims, error = _authenticate(request, SYNC_SCOPE)
    if error:
        logger.warning("internal xero sync: rejected caller (%s)", error)
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        entity_id = uuid.UUID(str(claims["entity_id"]))
    except ValueError:
        logger.error("internal xero sync: entity_id %r is not a uuid", claims["entity_id"])
        return JsonResponse({"error": "Xero needs to be connected again.", "reconnect": True}, status=409)
    token = resolve_entity_access_token(entity_id)
    if not token:
        return JsonResponse({"error": "Xero needs to be connected again.", "reconnect": True}, status=409)
    if _wants_throttle(request) and read_recently(entity_id):
        logger.info("internal xero sync: company %s read under 2 minutes ago; skipped", entity_id)
        return JsonResponse({"accounts": None, "contacts": None, "skipped": True})
    access_token, xero_org_id = token
    return JsonResponse(sync_company(entity_id, access_token, xero_org_id))


def _wants_throttle(request: HttpRequest) -> bool:
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("throttle") is True


#: The relink's own scope: Petty Cash's and Payment Requests' publish and their "map it now".
CONTACT_SCOPE = "xero-contact-relink"


def internal_contact_relink(request: HttpRequest, contact_id: str) -> HttpResponse:
    """``POST /api/internal/xero/contacts/<id>/relink`` ``{"xero_contact_id", "reason"}`` (owner's
    decisions doc C4, 2026-11-19): the company's contact pointed at the Xero contact Xero merged it
    into (``merged_in_xero``) or the one the person picked (``user``) - merged into the Minty contact
    already holding it, if one does (``xero/services/merge.relink_contact``). The caller's assertion
    (scope ``xero-contact-relink``) names the company and, for ``user``, the person; the caller checks
    the person's right first. 200 ``{"contact_id", "xero_contact_id", "merged", "moved"}``; 400 bad
    input; 404 / 409 with a sentence."""
    from xero.services import merge

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claims, error = _authenticate(request, CONTACT_SCOPE)
    if error:
        logger.warning("internal contact relink: rejected caller (%s)", error)
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        body = json.loads(request.body or b"{}")
        entity_id, record = uuid.UUID(str(claims["entity_id"])), uuid.UUID(str(contact_id))
    except ValueError:
        return JsonResponse({"error": "That contact isn't in this company. Reload the list and try again."},
                            status=400)
    guid, reason = (body or {}).get("xero_contact_id"), (body or {}).get("reason")
    if not isinstance(guid, str) or not guid.strip() or reason not in ("merged_in_xero", "user"):
        return JsonResponse({"error": "Choose the Xero contact to use."}, status=400)
    user_id = claims.get("user_id") if reason == "user" else None
    try:
        return JsonResponse(merge.relink_contact(entity_id, record, guid.strip(), user_id, reason=reason))
    except merge.Refused as refusal:
        return JsonResponse({"error": refusal.message}, status=refusal.status)
