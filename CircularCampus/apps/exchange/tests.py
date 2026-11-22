"""Concurrency: two users race for the last unit (FR-49, NFR-14).

Runs only against PostgreSQL, where ``select_for_update`` takes real row locks.
SQLite results are NOT presented as evidence of PostgreSQL locking behaviour.
Run:  POSTGRES_DB=... python manage.py test apps.exchange
"""
import threading
import unittest

from django.db import connection, connections
from django.test import TransactionTestCase

from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.exchange.models import Reservation


@unittest.skipUnless(connection.vendor == "postgresql", "PostgreSQL-only row-locking test")
class PostgresLastUnitRaceTests(TransactionTestCase):
    def test_only_one_of_many_concurrent_holds_succeeds(self):
        w = World()
        asset = w.asset(qty=1)
        requesters = [w.user(f"1000608{i:02d}", f"Racer {i}", w.cs, [("REQUESTER", w.cs)]) for i in range(6)]
        requests = [w.request(qty=1, user=u) for u in requesters]
        barrier = threading.Barrier(len(requests))
        results = []

        def attempt(req, user):
            try:
                req.refresh_from_db()
                barrier.wait()
                w.submit(req, asset, 1, user=user)
                results.append("ok")
            except BusinessError as exc:
                results.append(exc.code)
            except Exception as exc:  # noqa: BLE001
                results.append(type(exc).__name__)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(r, u)) for r, u in zip(requests, requesters)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(Reservation.objects.filter(asset=asset).count(), 1)
        self.assertTrue(all(r in ("ok", BusinessError.STOCK_CONFLICT) for r in results), results)
