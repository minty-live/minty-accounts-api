"""AI allowances for Minty support (owner, 2026-11-18): every superuser can see each company's AI
grants and use, and give a company a grant by hand - a top-up. The automatic ones are the 30-day AI
trial and one grant per billing period after it (the user, 2026-11-27; minty-shared v0.28).

    GET  /api/me/ai/companies[?q=&page=|?ids=]  the companies, each with what is left of its grants
    GET  /api/me/ai/companies/<id>            one company: its grants, its last reads
    GET  /api/me/ai/companies/<id>/reads?before=<read id>  the next older reads ("Show more")
    POST /api/me/ai/companies/<id>/grants     {tokens?, reads?, days?, note?} -> the company again
    POST /api/me/ai/companies/<id>/grants/<grant id>/end   end that grant now -> the company again

The allowance itself is minty-db's ``ai_policy``/``ai_grant``/``ai_read`` through
``minty_shared.django.ai_allowance`` - the same code the two readers (minty-petty-cash-api,
minty-payment-request-api) reserve and charge with, so this page and the read button never disagree.
The models, the trial and period sizes and the extra caps stay on minty-petty-cash-api's
``manage.py ai_policy`` (owner: grants only here). Superusers only; anyone else is refused (403).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from django.db import transaction
from django.db.models import Count, Max, Q, Subquery
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from minty_db.models import AiGrant, AiRead, Entity
from minty_shared.django import ai_allowance

from hub.policy import is_superuser
from hub.transport import guard, refuse, respond

logger = logging.getLogger("minty-accounts-api")

PAGE_SIZE = 25
#: ``?ids=``: the entity list's AI mode asks for the companies it shows, in one call.
MAX_IDS = 500
MAX_TOKENS = 10_000_000
MAX_READS = 10_000
MAX_DAYS = 3650
MAX_NOTE = 255

ONLY_SUPPORT = "Only Minty support can manage AI allowances."
NOT_FOUND = "I couldn't find that company."
READ_NOT_FOUND = "I couldn't find that read. Close the company and open it again."
GRANT_NOT_FOUND = "I couldn't find that grant."
GRANT_ENDED = "This grant has already ended."
READS_PAGE = 20
NO_POLICY = ("The AI settings are missing or can't be used, so AI reading is off. "
             "Check ai_policy with manage.py ai_policy.")
REFUSED = "The grant wasn't given. Fix the fields below and try again."
FIELD = {
    "nothing": "The grant gives nothing yet. Add tokens, reads or both to continue.",
    "tokens": f"Enter the tokens as a whole number from 1 to {MAX_TOKENS:,}.",
    "reads": f"Enter the reads as a whole number from 1 to {MAX_READS:,}.",
    "days": f"Enter the days as a whole number from 1 to {MAX_DAYS:,}, or leave it empty for no end date.",
    "note": f"Shorten the note to {MAX_NOTE} characters or fewer.",
}


def _superuser(request: HttpRequest, methods: tuple[str, ...]):
    """``(early, user)``: the method, the door, a superuser."""
    if request.method not in (*methods, "OPTIONS"):
        return refuse(request, "Method not allowed", 405), None
    early, user = guard(request)
    if early is not None:
        return early, None
    if not is_superuser(user):
        return refuse(request, ONLY_SUPPORT, 403), None
    return None, user


def _company(entity_id: str) -> Entity | None:
    try:
        return Entity.objects.filter(id=uuid.UUID(str(entity_id))).first()
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _open_totals(grants: list[ai_allowance.GrantUsage], now: datetime) -> tuple[int | None, int | None]:
    """``(tokens used, tokens given)`` over the grants that still have room - what "x of y" shows."""
    open_ = [g for g in grants if g.has_room(now) and g.tokens_total is not None]
    if not open_:
        return None, None
    return sum(g.tokens_used for g in open_), sum(g.tokens_total or 0 for g in open_)


# --------------------------------------------------------------------------- the list


def ai_companies(request: HttpRequest) -> HttpResponse:
    """``GET /api/me/ai/companies``: every company not archived, A to Z (``q`` filters by name), a
    page of ``PAGE_SIZE``, each with what is left of its grants, its answered reads and its last
    read. A grant due but not given yet (the trial, this period's) counts, as the readers count it.
    ``ids`` (comma-separated, at most ``MAX_IDS``) answers exactly those companies, no page - the
    entity list's "AI allowances" mode asks for the rows it shows."""
    early, _user = _superuser(request, ("GET",))
    if early is not None:
        return early
    policy = ai_allowance.load_policy()
    if policy is None:
        return refuse(request, NO_POLICY, 503)
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except ValueError:
        page = 1
    query = (request.GET.get("q") or "").strip()
    companies = Entity.objects.filter(archived_at__isnull=True)
    wanted = [i.strip() for i in (request.GET.get("ids") or "").split(",") if i.strip()]
    if wanted:
        try:
            ids_asked = [uuid.UUID(i) for i in wanted]
        except ValueError:
            return refuse(request, "I couldn't read the companies asked for. Reload the page and try again.", 422)
        if len(ids_asked) > MAX_IDS:
            return refuse(request, f"Ask for {MAX_IDS} companies or fewer at a time.", 422)
        companies = companies.filter(id__in=ids_asked)
    if query:
        companies = companies.filter(name__icontains=query)
    total = companies.count()
    ordered = companies.order_by("name", "id").values("id", "name")
    rows = list(ordered if wanted else ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])
    ids = [r["id"] for r in rows]
    now = timezone.now()
    grants = ai_allowance.grants_of_many(ids, now)
    subscriptions = ai_allowance.subscriptions_of_many(ids)
    reads = {
        r["entity_id"]: r
        for r in AiRead.objects.filter(entity_id__in=ids).values("entity_id").annotate(
            answered=Count("id", filter=Q(status="ok")), last=Max("created_at"))
    }
    out = []
    for r in rows:
        mine = grants.get(str(r["id"]), [])
        subscription = subscriptions.get(str(r["id"]), ai_allowance.Subscription())
        allowance = ai_allowance.grant_allowance(mine, policy, subscription, now)
        used, given = _open_totals(ai_allowance.with_due(mine, policy, subscription, now)[0], now)
        read = reads.get(r["id"]) or {}
        out.append({
            "id": str(r["id"]),
            "name": r["name"],
            "percent_left": allowance.percent_left,
            "blocked": allowance.blocked,
            "tokens_used": used,
            "tokens_given": given,
            "has_grant": bool(mine),
            "reads": int(read.get("answered") or 0),
            "last_read_at": _iso(read.get("last")),
        })
    return respond(request, {"companies": out, "page": page, "page_size": PAGE_SIZE, "total": total})


# --------------------------------------------------------------------------- one company


def _detail(entity: Entity, policy: ai_allowance.Policy) -> dict:
    now = timezone.now()
    usage = {g.id: g for g in ai_allowance.grants_of(entity.id, now)}
    grants = []
    for g in AiGrant.objects.filter(entity_id=entity.id).order_by("-created_at", "-id"):
        u = usage.get(g.id)
        grants.append({
            "id": str(g.id),
            "source": g.source,
            "tokens_total": g.tokens_total,
            "tokens_used": u.tokens_used if u else 0,
            "reads_total": g.reads_total,
            "reads_used": u.reads_used if u else 0,
            "expires_at": _iso(g.expires_at),
            "open": bool(u and u.has_room(now)),
            "granted_by": g.granted_by,
            "note": g.note,
            "created_at": _iso(g.created_at),
            "ended_at": _iso(g.ended_at),
            "ended_by": g.ended_by,
        })
    reads, more = _reads(entity.id)
    spec = policy.default_grant
    return {
        "company": {"id": str(entity.id), "name": entity.name},
        "allowance": ai_allowance.status(entity.id, policy).as_dict(),
        "default_grant": None if spec is None else {"tokens": spec.tokens, "reads": spec.reads, "days": spec.days},
        "cycle_tokens": policy.cycle_tokens,
        "grants": grants,
        "reads": reads,
        "reads_more": more,
    }


def _reads(entity_id, before: AiRead | None = None) -> tuple[list[dict], bool]:
    """``READS_PAGE`` reads newest first, older than ``before`` when given, and whether any older
    ones are left (owner, 2026-11-19: "Show more", not a fixed 20). The cursor is (created_at, id):
    reads can share a created_at, and the order breaks the tie on the id."""
    rows = AiRead.objects.filter(entity_id=entity_id)
    if before is not None:
        # compared in the database: the cursor's created_at never round-trips through Python
        at = Subquery(AiRead.objects.filter(id=before.id).values("created_at")[:1])
        rows = rows.filter(Q(created_at__lt=at) | Q(created_at=at, id__lt=before.id))
    page = list(rows.order_by("-created_at", "-id")[:READS_PAGE + 1])
    return [
        {"id": str(r.id), "created_at": _iso(r.created_at), "module": r.module, "status": r.status,
         "model": r.model_answered, "attempts": r.attempts,
         "tokens": r.tokens_in + r.tokens_out + r.tokens_thought}
        for r in page[:READS_PAGE]
    ], len(page) > READS_PAGE


def ai_company(request: HttpRequest, entity_id: str) -> HttpResponse:
    """``GET /api/me/ai/companies/<id>``: one company's allowance (its grants and the extra caps),
    every grant it was given (newest first, with what was charged to it), its last 20 reads and
    ``reads_more`` when older ones are left."""
    early, _user = _superuser(request, ("GET",))
    if early is not None:
        return early
    entity = _company(entity_id)
    if entity is None:
        return refuse(request, NOT_FOUND, 404)
    policy = ai_allowance.load_policy()
    if policy is None:
        return refuse(request, NO_POLICY, 503)
    return respond(request, _detail(entity, policy))


def ai_company_reads(request: HttpRequest, entity_id: str) -> HttpResponse:
    """``GET /api/me/ai/companies/<id>/reads?before=<read id>``: the next ``READS_PAGE`` reads
    older than that one (a read of THIS company), ``{reads, more}``."""
    early, _user = _superuser(request, ("GET",))
    if early is not None:
        return early
    entity = _company(entity_id)
    if entity is None:
        return refuse(request, NOT_FOUND, 404)
    try:
        before = AiRead.objects.filter(id=uuid.UUID(request.GET.get("before", "")), entity_id=entity.id).first()
    except ValueError:
        before = None
    if before is None:
        return refuse(request, READ_NOT_FOUND, 404)
    reads, more = _reads(entity.id, before)
    return respond(request, {"reads": reads, "more": more})


# --------------------------------------------------------------------------- giving a grant


def _whole(value, low: int, high: int) -> int | None | bool:
    """A whole number in range, None when empty, False when not acceptable."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return False
    try:
        number = int(str(value).replace(",", "").strip())
    except ValueError:
        return False
    if isinstance(value, float) and value != number:
        return False
    return number if low <= number <= high else False


def check_grant(body: dict) -> tuple[dict, dict[str, str]]:
    """``(clean values, problems by field)`` - one Part 11 sentence per field."""
    problems: dict[str, str] = {}
    tokens = _whole(body.get("tokens"), 1, MAX_TOKENS)
    reads = _whole(body.get("reads"), 1, MAX_READS)
    days = _whole(body.get("days"), 1, MAX_DAYS)
    for name, value in (("tokens", tokens), ("reads", reads), ("days", days)):
        if value is False:
            problems[name] = FIELD[name]
    note = str(body.get("note") or "").strip()
    if len(note) > MAX_NOTE:
        problems["note"] = FIELD["note"]
    if not problems and tokens is None and reads is None:
        problems["tokens"] = FIELD["nothing"]
    clean = {"tokens": tokens or None, "reads": reads or None, "days": days or None, "note": note or None}
    return clean, problems


def ai_company_grants(request: HttpRequest, entity_id: str) -> HttpResponse:
    """``POST /api/me/ai/companies/<id>/grants``: give the company a grant - ``tokens`` and/or
    ``reads``, ``days`` until it expires (empty = never), a ``note``. It adds to whatever the
    company still has, and it can read again at once. ``granted_by`` is the superuser's email.
    Refused per field (422) with the form's sentences; answers the company as the GET does."""
    early, user = _superuser(request, ("POST",))
    if early is not None:
        return early
    entity = _company(entity_id)
    if entity is None:
        return refuse(request, NOT_FOUND, 404)
    policy = ai_allowance.load_policy()
    if policy is None:
        return refuse(request, NO_POLICY, 503)
    try:
        body = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return refuse(request, REFUSED, 422)
    clean, problems = check_grant(body)
    if problems:
        return respond(request, {"error": REFUSED, "fields": problems}, 422)
    spec = ai_allowance.GrantSpec(tokens=clean["tokens"], reads=clean["reads"], days=clean["days"])
    grant_id = ai_allowance.give_grant(entity.id, spec, granted_by=user.email, note=clean["note"])
    logger.info("AI grant given entity=%s grant=%s actor=%s tokens=%s reads=%s days=%s", entity.id, grant_id,
                user.id, spec.tokens, spec.reads, spec.days)
    return respond(request, {**_detail(entity, policy), "message": f"Grant given to {entity.name}."}, 201)


def ai_company_grant_end(request: HttpRequest, entity_id: str, grant_id: str) -> HttpResponse:
    """``POST /api/me/ai/companies/<id>/grants/<grant id>/end`` (owner, 2026-11-20): end that grant
    of THIS company now - its unused tokens and reads can't be used anymore. It sets ``expires_at``
    (what the allowance counts by) and ``ended_at``/``ended_by`` (the superuser's email) together;
    the grant stays in the list, so the one-time default grant never comes back. 404 for another
    company's grant, 409 when it has already ended or expired. Answers the company as the GET does."""
    early, user = _superuser(request, ("POST",))
    if early is not None:
        return early
    entity = _company(entity_id)
    if entity is None:
        return refuse(request, NOT_FOUND, 404)
    policy = ai_allowance.load_policy()
    if policy is None:
        return refuse(request, NO_POLICY, 503)
    try:
        grant_uuid = uuid.UUID(str(grant_id))
    except ValueError:
        return refuse(request, GRANT_NOT_FOUND, 404)
    now = timezone.now()
    with transaction.atomic():
        grant = AiGrant.objects.select_for_update().filter(id=grant_uuid, entity_id=entity.id).first()
        if grant is None:
            return refuse(request, GRANT_NOT_FOUND, 404)
        if grant.ended_at is not None or (grant.expires_at is not None and grant.expires_at <= now):
            return refuse(request, GRANT_ENDED, 409)
        AiGrant.objects.filter(id=grant.id).update(expires_at=now, ended_at=now, ended_by=user.email)
    logger.info("AI grant ended entity=%s grant=%s actor=%s", entity.id, grant.id, user.id)
    return respond(request, {**_detail(entity, policy), "message": "Grant ended."})
