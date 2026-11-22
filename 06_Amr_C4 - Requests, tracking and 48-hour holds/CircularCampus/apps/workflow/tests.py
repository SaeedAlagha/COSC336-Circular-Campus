"""C4/C5/C6: requests, staged approval, transfer, custody, concurrency-safe stock and rollback."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Role, UserRole
from apps.accounts.policy import AccessDenied
from apps.catalog.models import Asset, AssetStatus
from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.exchange import services as exchange
from apps.exchange.models import RequestStatus, Reservation, ReservationStatus
from apps.maintenance.models import AssetLineage, LifeCycleEvent
from apps.reporting.models import AuditEvent, Notification
from apps.workflow import services as workflow
from apps.workflow.models import Approval, ProposalVersion, TransferStatus


class RequestAndHoldTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_submit_creates_48h_hold_and_routes_stages_in_order(self):
        a = self.w.asset(cost=None)
        req, version, hold = self.w.submit(self.w.request(qty=2), a, 2)
        self.assertEqual(req.status, RequestStatus.SUBMITTED)
        self.assertEqual(hold.status, ReservationStatus.PENDING)
        self.assertAlmostEqual((hold.expires_at - timezone.now()).total_seconds(), 48 * 3600, delta=60)
        stages = list(version.approvals.order_by("stage_order").values_list("stage", "decision"))
        self.assertEqual(stages, [("RELEASE_CHECK", None), ("COST_REVIEW", "NOT_APPLICABLE"), ("AUTHORIZE", None)])
        # Free stock excludes the hold.
        from apps.catalog.services import free_listed_quantity
        self.assertEqual(free_listed_quantity(Asset.objects.get(pk=a.pk)), 2)

    def test_cost_review_required_when_transfer_cost_present(self):
        a = self.w.asset(cost=40)
        _, version, _ = self.w.submit(self.w.request(), a, 2)
        self.assertIsNone(version.approvals.get(stage="COST_REVIEW").decision)

    def test_required_date_in_past_rejected_with_field_error(self):
        from datetime import date
        with self.assertRaises(BusinessError) as ctx:
            self.w.request(required_date=date.today() - timedelta(days=1))
        self.assertIn("required_date", ctx.exception.field_errors)

    def test_partial_hold_rejected_unless_allowed(self):
        a = self.w.asset(qty=1)
        with self.assertRaises(BusinessError):
            self.w.submit(self.w.request(qty=3), a, 1)
        req, _, hold = self.w.submit(self.w.request(qty=3, partial=True), a, 1)
        self.assertEqual(hold.quantity, 1)

    def test_last_item_cannot_be_held_twice(self):
        a = self.w.asset(qty=1)
        self.w.submit(self.w.request(qty=1), a, 1)
        other = self.w.request(qty=1, user=self.w.rep)
        with self.assertRaises(BusinessError) as ctx:
            self.w.submit(other, a, 1, user=self.w.rep)
        self.assertEqual(ctx.exception.code, BusinessError.STOCK_CONFLICT)
        self.assertEqual(Reservation.objects.filter(asset=a).count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.status, RequestStatus.DRAFT, "failed submission leaves no partial state")

    def test_pending_hold_expires_but_need_stays_open(self):
        a = self.w.asset()
        req, version, hold = self.w.submit(self.w.request(), a, 2)
        Reservation.objects.filter(pk=hold.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(exchange.expire_due_holds(), 1)
        hold.refresh_from_db(); req.refresh_from_db()
        self.assertEqual(hold.status, ReservationStatus.EXPIRED)
        self.assertEqual(req.status, RequestStatus.SUBMITTED)
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="HOLD_EXPIRED").exists())
        # Approving the stale version is refused.
        workflow.decide(self.w.eng_cust, version.approvals.get(stage="RELEASE_CHECK").pk, version.pk, "APPROVED", "ok")
        with self.assertRaises(BusinessError) as ctx:
            workflow.decide(self.w.approver, version.approvals.get(stage="AUTHORIZE").pk, version.pk, "APPROVED", "ok")
        self.assertEqual(ctx.exception.code, BusinessError.STALE_VERSION)

    def test_allocated_stock_never_auto_expires(self):
        req, t, a = self.w.approved_transfer()
        hold = t.reservation
        Reservation.objects.filter(pk=hold.pk).update(expires_at=timezone.now() - timedelta(days=5))
        exchange.expire_due_holds()
        hold.refresh_from_db()
        self.assertEqual(hold.status, ReservationStatus.ALLOCATED)

    def test_idempotent_repeat_submission_has_no_duplicate_effect(self):
        import uuid
        from apps.core.commands import execute
        a = self.w.asset()
        req = self.w.request()
        key = uuid.uuid4()
        payload = {"req": str(req.pk), "asset": str(a.pk), "qty": 2}
        fn = lambda: {"v": exchange.submit(self.w.requester, req.pk, req.version, a.pk, 2)[1].version_no}  # noqa: E731
        r1, replay1 = execute(self.w.requester, "submit", key, payload, fn)
        r2, replay2 = execute(self.w.requester, "submit", key, payload, fn)
        self.assertFalse(replay1); self.assertTrue(replay2); self.assertEqual(r1, r2)
        self.assertEqual(Reservation.objects.filter(request=req).count(), 1)
        with self.assertRaises(BusinessError) as ctx:
            execute(self.w.requester, "submit", key, {**payload, "qty": 1}, fn)
        self.assertEqual(ctx.exception.code, BusinessError.IDEMPOTENCY_CONFLICT)

    def test_withdraw_before_approval_releases_hold_once(self):
        a = self.w.asset()
        req, _, hold = self.w.submit(self.w.request(), a, 2)
        req.refresh_from_db()
        exchange.withdraw(self.w.requester, req.pk, req.version, "No longer needed")
        hold.refresh_from_db(); req.refresh_from_db()
        self.assertEqual((hold.status, req.status), (ReservationStatus.CANCELLED, RequestStatus.CANCELLED))
        with self.assertRaises(BusinessError):
            exchange.release_hold(hold, ReservationStatus.CANCELLED, None, "again")

