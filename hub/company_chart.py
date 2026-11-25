"""``/api/me/company/accounts[/<id>]`` and ``/api/me/company/contacts[/<id>]``: a company's chart of
accounts and contacts in the hub's Settings (owner, 2026-11-12).

minty-onboarding-api holds the one implementation (the owner's choice that day): its account rules
and side effects, who owns what, and the one Xero write left - a contact added while live, linked
or made there as a supplier (the owner's decisions doc C5; Minty never creates an account in Xero, A1). This is the hub's door to it - the person's
token, the company from ``?entity=``, the permission - then the call, as an internal one carrying
a short-lived service assertion (scope ``company-chart``) that names the company and the person.
Its answer and status come back as they are; onboarding-api checks the person's rights again.

Viewing is ``COA_VIEW`` (cashier and up); adding, changing and deleting accounts and contacts are
``COA_CREATE`` / ``COA_UPDATE`` / ``COA_DELETE`` (accountant and up).

``POST .../contacts/merge {"ids": [<id>, ...], "keep": <id>}`` merges contacts here, not in
onboarding-api: several ticked contacts absorbed into the one kept (the user, 2026-11-25), accounts-api's
one implementation (``xero/services/merge.py``, the owner's decisions doc M1-M4). ``COA_DELETE`` - a
merge takes some away. Accounts are never merged (the user, 2026-11-24).

Opening the contact list reads the company's contacts from Xero first - read only, at most once
every 2 minutes (owner's decisions doc C2, 2026-11-19), as Petty Cash Settings does.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from minty_db.models import Entity
from minty_shared import tokens

from hub.company import _body, _open, _uuid
from hub.policy import Permission
from hub.transport import refuse, respond
from xero.services import merge as merging
from xero.services import sync as xero_sync
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("accounts-api")

SCOPE = "company-chart"
TIMEOUT = (5, 45)  # a Xero write rides inside
UNAVAILABLE = "I couldn't reach the accounts just now. Mind trying again in a moment?"
DENIED = {
    "GET": "You don't have permission to see this company's accounts and contacts.",
    "POST": "You don't have permission to add accounts and contacts here.",
    "PATCH": "You don't have permission to change this company's accounts and contacts.",
    "DELETE": "You don't have permission to delete this company's accounts and contacts.",
}
NEEDS = {
    "GET": Permission.COA_VIEW,
    "POST": Permission.COA_CREATE,
    "PATCH": Permission.COA_UPDATE,
    "DELETE": Permission.COA_DELETE,
}


def _forward(request: HttpRequest, user, entity_id: str, path: str) -> HttpResponse:
    base = settings.ONBOARDING_API_URL
    if not base:
        logger.error("company chart: ONBOARDING_API_URL is not set; cannot reach the accounts")
        return refuse(request, UNAVAILABLE, 503)
    assertion = tokens.service_assertion(settings.SECRET_KEY, SCOPE, entity_id, user_id=str(user.id))
    body = _body(request) if request.method in ("POST", "PATCH") else None
    try:
        answer = requests.request(request.method, f"{base}/api/internal/chart/{path}",
                                  headers=tokens.assertion_headers(assertion), json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("company chart: onboarding-api unreachable for %s %s: %s", request.method, path, exc)
        return refuse(request, UNAVAILABLE, 503)
    try:
        payload = answer.json()
    except ValueError:
        logger.error("company chart: onboarding-api answered %s with no JSON for %s %s", answer.status_code,
                     request.method, path)
        return refuse(request, UNAVAILABLE, 503)
    if answer.status_code == 401:  # our own assertion refused: a key mismatch, never the person's fault
        logger.error("company chart: onboarding-api refused this service's assertion")
        return refuse(request, UNAVAILABLE, 503)
    return respond(request, payload, answer.status_code)


def _door(request: HttpRequest, methods: tuple[str, ...], path: str) -> HttpResponse:
    if request.method not in (*methods, "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    permission = NEEDS.get(request.method, Permission.COA_VIEW)
    early, user, entity_id = _open(request, permission, DENIED.get(request.method, DENIED["GET"]))
    if early is not None:
        return early
    if request.method == "GET" and path == "contacts":
        _read_contacts_from_xero(entity_id)
    return _forward(request, user, entity_id, path)


def _read_contacts_from_xero(entity_id) -> None:
    """A connected company's contacts read from Xero before the list is shown (read only, at most
    once every 2 minutes). Xero not answering leaves the stored list, logged."""
    if not Entity.objects.filter(id=entity_id, status="connected").exists() or xero_sync.read_recently(entity_id):
        return
    token = resolve_entity_access_token(entity_id)
    if token is None:
        return
    try:
        xero_sync.sync_company(entity_id, *token)
    except Exception:
        logger.exception("company chart: the Xero read for company %s failed; the stored contacts are shown",
                         entity_id)


def company_accounts(request: HttpRequest) -> HttpResponse:
    return _door(request, ("GET", "POST"), "accounts")


def company_account(request: HttpRequest, account_id: str) -> HttpResponse:
    account_id = _uuid(account_id)
    if account_id is None and request.method != "OPTIONS":
        return refuse(request, "That account isn't in this company. Reload the list and try again.", 404)
    return _door(request, ("PATCH", "DELETE"), f"accounts/{account_id}")


def company_contacts(request: HttpRequest) -> HttpResponse:
    return _door(request, ("GET", "POST"), "contacts")


def company_contact(request: HttpRequest, contact_id: str) -> HttpResponse:
    contact_id = _uuid(contact_id)
    if contact_id is None and request.method != "OPTIONS":
        return refuse(request, "That contact isn't in this company. Reload the list and try again.", 404)
    return _door(request, ("PATCH", "DELETE"), f"contacts/{contact_id}")


MERGE_DENIED = "You don't have permission to merge this company's contacts."
PICK_ONE = "Choose which one to keep, then merge."


def company_contacts_merge(request: HttpRequest) -> HttpResponse:
    """``POST {ids, keep}``: the ticked contacts merged into the one kept (``merge.merge_contacts``)."""
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.COA_DELETE, MERGE_DENIED)
    if early is not None:
        return early
    data = _body(request)
    ids, keep = data.get("ids"), _uuid(data.get("keep"))
    if not isinstance(ids, list) or any(_uuid(i) is None for i in ids):
        return refuse(request, merging.TICK_TWO, 400)
    if keep is None:
        return refuse(request, PICK_ONE, 400)
    try:
        return respond(request, merging.merge_contacts(entity_id, [_uuid(i) for i in ids], keep, user.id))
    except merging.Refused as refusal:
        return refuse(request, refusal.message, refusal.status)
