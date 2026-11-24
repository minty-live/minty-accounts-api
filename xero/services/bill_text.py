"""Bills' text copies kept in step with the chart (the user's rule, 2026-11-24).

Every screen reads a bill's account and supplier by id (payment-request-api ``chart_view``); the text
copies - ``bill_line.account_code``/``account_name``, ``bill.xero_account_code`` (the first line's
code), ``bill.contact_name`` - are rewritten here whenever the chart changes under them: matching's
Finish, a merge, a rename in Xero. Every bill, published ones included. No Xero call: a published bill
is only relabelled in Minty, and its ``published_*`` record (what was sent) is never touched.
"""

from __future__ import annotations

import logging

from minty_db.models import AccountInfo, Bill, BillLine, XeroContactSync

logger = logging.getLogger("minty-accounts-api")

#: ``bill_line.account_code`` and ``bill.xero_account_code`` hold 20.
_CODE_MAX = 20


def _code(account: AccountInfo) -> str:
    return (account.xero_code or account.code or "").strip()


def refresh_accounts(entity_id, account_ids) -> int:
    """The bill lines on these accounts take each account's code and name now, and their bills'
    header code follows the first line. Returns the lines rewritten."""
    rewritten = 0
    for account in AccountInfo.objects.filter(entity_id=entity_id, id__in=[i for i in account_ids if i]):
        code, name = _code(account), (account.name or "")[:150]
        values = {"account_name": name}
        if code and len(code) <= _CODE_MAX:
            values["account_code"] = code
        lines = BillLine.objects.filter(bill__entity_id=entity_id, account_id=account.id)
        bill_ids = set(lines.values_list("bill_id", flat=True))
        rewritten += lines.update(**values)
        if not bill_ids or "account_code" not in values:
            continue
        firsts = {}
        for bill_id, line_account in (BillLine.objects.filter(bill_id__in=bill_ids)
                                      .order_by("bill_id", "sort_order", "created_at")
                                      .values_list("bill_id", "account_id")):
            firsts.setdefault(bill_id, line_account)
        heads = [b for b, first in firsts.items() if first == account.id]
        if heads:
            Bill.objects.filter(id__in=heads).exclude(xero_account_code=code).update(xero_account_code=code)
    if rewritten:
        logger.info("bill text: company %s, %s bill lines follow their accounts", entity_id, rewritten)
    return rewritten


def refresh_contacts(entity_id, contact_ids) -> int:
    """The bills of these suppliers take each one's name now. Returns the bills rewritten."""
    rewritten = 0
    for contact in XeroContactSync.objects.filter(entity_id=entity_id, id__in=[i for i in contact_ids if i]):
        name = (contact.name or "")[:100]
        if name:
            rewritten += Bill.objects.filter(entity_id=entity_id, contact_id=contact.id).exclude(
                contact_name=name).update(contact_name=name)
    if rewritten:
        logger.info("bill text: company %s, %s bills follow their suppliers", entity_id, rewritten)
    return rewritten
