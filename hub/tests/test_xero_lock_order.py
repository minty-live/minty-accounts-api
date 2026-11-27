"""One lock order for a company's Xero rows: the sync lock first, then the company row.

Production, 2026-11-26: two syncs of a company changing its matches deadlocked. ``sync_during_change``
held the company row ``FOR UPDATE`` and waited for the sync lock (``sync.lock``), while the other sync
held the sync lock and its next write - any row with a foreign key to ``entities`` takes ``FOR KEY
SHARE`` on the company row - waited for that row. Every transaction that locks the company row must
take the sync lock first, so while it waits it holds nothing a sync needs.

Each test stands in for a running sync on a second connection: it holds the sync lock, checks the
function is waiting for it, then takes the company row the way a foreign-key insert does (``FOR KEY
SHARE NOWAIT``). Postgres only; committed rows (``transaction=True``), since three connections look.
"""

from __future__ import annotations

import os
import threading
import time

import psycopg
import pytest
from django.conf import settings
from django.db import connection, connections

from hub.tests.test_company import call
from hub.tests.test_xero_matching import BASE, ORG, connected, minty, xero  # noqa: F401 - fixtures
from xero.services import matching

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only (the sync lock)"),
    pytest.mark.django_db(transaction=True),
]


def _connect(autocommit: bool) -> psycopg.Connection:
    db = connection.settings_dict
    return psycopg.connect(host=db["HOST"], port=db["PORT"] or 5432, user=db["USER"], password=db["PASSWORD"],
                           dbname=db["NAME"], autocommit=autocommit)


def _waiting_for_the_sync_lock(watch: psycopg.Connection) -> bool:
    return watch.execute(
        "SELECT count(*) FROM pg_locks l JOIN pg_database d ON d.oid = l.database "
        "WHERE d.datname = current_database() AND l.locktype = 'advisory' AND NOT l.granted").fetchone()[0] > 0


def _run_beside_a_sync(entity_id, work) -> tuple[bool, bool]:
    """``work()`` in its own thread while a second connection holds the company's sync lock.
    Returns (it waited for the lock, the company row was free while it waited)."""
    sync_conn, watch = _connect(autocommit=False), _connect(autocommit=True)
    errors: list[BaseException] = []
    waited = row_free = False
    try:
        sync_conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"xero-sync:{entity_id}"])

        def run():
            try:
                work()
            except BaseException as exc:  # noqa: BLE001 - reported by the test below
                errors.append(exc)
            finally:
                connections.close_all()

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker.is_alive():
            if _waiting_for_the_sync_lock(watch):
                waited = True
                break
            time.sleep(0.05)
        if waited:
            try:
                # what the sync's next insert does to the company row (its foreign key)
                sync_conn.execute(f"SELECT 1 FROM {settings.DB_SCHEMA}.entities WHERE id = %s FOR KEY SHARE NOWAIT",
                                  [entity_id])
                row_free = True
            except psycopg.errors.LockNotAvailable:
                row_free = False
        sync_conn.rollback()  # the "sync" ends: its lock goes
        worker.join(timeout=30)
        assert not worker.is_alive(), "still blocked after the sync ended"
    finally:
        sync_conn.close()
        watch.close()
    assert not errors, errors
    return waited, row_free


def _changing(m):
    assert call("post", BASE + "/change", m["admin"], m["entity"].id).status_code == 200


def test_a_sync_during_a_change_waits_for_the_sync_lock_holding_nothing(connected, xero):  # noqa: F811
    entity = connected["entity"]
    _changing(connected)
    waited, row_free = _run_beside_a_sync(entity.id, lambda: matching.sync_during_change(entity.id, xero.accounts,
                                                                                         ORG))
    assert waited, "it takes the sync lock"
    assert row_free, "it holds the company row while it waits: a deadlock with the running sync"


def test_finish_waits_for_the_sync_lock_holding_nothing(connected):  # noqa: F811
    entity = connected["entity"]
    _changing(connected)
    waited, row_free = _run_beside_a_sync(entity.id, lambda: matching.finish(entity.id, connected["admin"].id))
    assert waited, "it takes the sync lock"
    assert row_free, "it holds the company row while it waits: a deadlock with the running sync"


def test_starting_a_change_waits_for_the_sync_lock_holding_nothing(connected):  # noqa: F811
    entity = connected["entity"]
    waited, row_free = _run_beside_a_sync(entity.id, lambda: matching.start_change(entity.id))
    assert waited, "it changes the company's account rows, so it takes the sync lock"
    assert row_free, "it holds the company row while it waits: a deadlock with the running sync"


def test_cancelling_a_change_waits_for_the_sync_lock_holding_nothing(connected):  # noqa: F811
    entity = connected["entity"]
    _changing(connected)
    waited, row_free = _run_beside_a_sync(entity.id, lambda: matching.cancel_change(entity.id))
    assert waited, "it changes the company's matching rows, so it takes the sync lock"
    assert row_free, "it holds the company row while it waits: a deadlock with the running sync"
