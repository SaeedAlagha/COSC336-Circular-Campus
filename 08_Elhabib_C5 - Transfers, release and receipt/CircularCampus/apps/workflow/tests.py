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


class ApprovalTests(TestCase):
    def setUp(self):
        self.w = World()
        self.asset = self.w.asset()
        self.req, self.version, self.hold = self.w.submit(self.w.request(), self.asset, 2)

    def stage(self, name):
        return self.version.approvals.get(stage=name)

    def test_self_approval_rejected_even_with_approver_role(self):
        UserRole.objects.create(user=self.w.requester, role=Role.objects.get(code="APPROVER"), scope_department=self.w.cs)
        workflow.decide(self.w.eng_cust, self.stage("RELEASE_CHECK").pk, self.version.pk, "APPROVED", "ok")
        with self.assertRaises(BusinessError) as ctx:
            workflow.decide(self.w.requester, self.stage("AUTHORIZE").pk, self.version.pk, "APPROVED", "")
        self.assertEqual(ctx.exception.code, BusinessError.SELF_APPROVAL)

    def test_stages_must_be_decided_in_order(self):
        with self.assertRaises(BusinessError) as ctx:
            workflow.decide(self.w.approver, self.stage("AUTHORIZE").pk, self.version.pk, "APPROVED", "")
        self.assertEqual(ctx.exception.code, BusinessError.MISSING_REVIEW)

    def test_sysadmin_cannot_approve(self):
        workflow.decide(self.w.eng_cust, self.stage("RELEASE_CHECK").pk, self.version.pk, "APPROVED", "ok")
        with self.assertRaises(AccessDenied):
            workflow.decide(self.w.admin, self.stage("AUTHORIZE").pk, self.version.pk, "APPROVED", "")

    def test_reject_requires_reason_and_releases_hold(self):
        workflow.decide(self.w.eng_cust, self.stage("RELEASE_CHECK").pk, self.version.pk, "APPROVED", "ok")
        with self.assertRaises(BusinessError):
            workflow.decide(self.w.approver, self.stage("AUTHORIZE").pk, self.version.pk, "REJECTED", "")
        workflow.decide(self.w.approver, self.stage("AUTHORIZE").pk, self.version.pk, "REJECTED", "Budget freeze")
        self.hold.refresh_from_db(); self.req.refresh_from_db()
        self.assertEqual((self.hold.status, self.req.status), (ReservationStatus.REJECTED, RequestStatus.REJECTED))
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="REQUEST_REJECTED").exists())

    def test_return_then_resubmit_creates_new_version_and_old_decision_is_stale(self):
        workflow.decide(self.w.eng_cust, self.stage("RELEASE_CHECK").pk, self.version.pk, "RETURNED", "Clarify room")
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, RequestStatus.RETURNED)
        req, v2, _ = self.w.submit(self.req, self.asset, 2)
        self.assertEqual(v2.version_no, 2)
        with self.assertRaises(BusinessError) as ctx:
            workflow.decide(self.w.approver, self.stage("AUTHORIZE").pk, self.version.pk, "APPROVED", "")
        self.assertEqual(ctx.exception.code, BusinessError.STALE_VERSION)

    def test_approval_allocates_but_does_not_change_custody(self):
        self.w.approve_all(self.version)
        self.hold.refresh_from_db(); self.asset.refresh_from_db()
        self.assertEqual(self.hold.status, ReservationStatus.ALLOCATED)
        self.assertEqual(self.asset.owner_department, self.w.eng)
        self.assertEqual(self.asset.custodian, self.w.eng_cust)
        t = self.hold.transfers.get()
        self.assertEqual(t.status, TransferStatus.APPROVED)
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="REQUEST_APPROVED").exists())


class TransferTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_release_only_by_assigned_custodian_and_exact_quantity(self):
        req, t, a = self.w.approved_transfer()
        with self.assertRaises(AccessDenied):
            workflow.release(self.w.cs_cust, t.pk, t.version, 2, "evidence long")
        with self.assertRaises(BusinessError):
            workflow.release(self.w.eng_cust, t.pk, t.version, 1, "evidence long")
        self.w.release(t)
        a.refresh_from_db()
        self.assertEqual(a.owner_department, self.w.eng, "release alone never moves custody")

    def test_receipt_only_by_designated_receiver(self):
        req, t, a = self.w.approved_transfer()
        self.w.release(t)
        t.refresh_from_db()
        with self.assertRaises(AccessDenied):
            workflow.confirm_receipt(self.w.requester, t.pk, t.version, 2, "GOOD", True)

    def test_matching_receipt_splits_batch_moves_custody_and_records_lineage(self):
        req, t, a = self.w.approved_transfer(qty=2)
        self.w.release(t)
        self.w.receive(t)
        t.refresh_from_db(); a.refresh_from_db(); req.refresh_from_db()
        child = t.received_asset
        self.assertEqual(t.status, TransferStatus.COMPLETED)
        self.assertEqual((a.quantity, child.quantity), (2, 2))
        self.assertEqual((child.owner_department, child.custodian, child.building, child.room), (self.w.cs, self.w.cs_cust, "B3", "204"))
        self.assertEqual(child.status, AssetStatus.IN_USE)
        self.assertTrue(AssetLineage.objects.filter(parent_asset=a, child_asset=child, kind="SPLIT").exists())
        self.assertEqual((req.status, req.received_quantity), (RequestStatus.FULFILLED, 2))
        self.assertEqual(t.reservation.__class__.objects.get(pk=t.reservation_id).status, ReservationStatus.CONSUMED)
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="TRANSFER_RECEIVED").exists())

    def test_whole_batch_transfer_moves_the_record(self):
        a = self.w.asset(qty=2)
        req, t, _ = self.w.approved_transfer(qty=2, asset=a)
        self.w.release(t); self.w.receive(t)
        a.refresh_from_db()
        self.assertEqual((a.owner_department, a.status, a.quantity), (self.w.cs, AssetStatus.IN_USE, 2))

    def test_repeated_receipt_is_idempotent(self):
        req, t, a = self.w.approved_transfer()
        self.w.release(t); self.w.receive(t)
        before = Asset.objects.count()
        t.refresh_from_db()
        workflow.confirm_receipt(self.w.cs_cust, t.pk, t.version, 2, "GOOD", True)
        self.assertEqual(Asset.objects.count(), before)
        req.refresh_from_db()
        self.assertEqual(req.received_quantity, 2)

    def test_quantity_mismatch_enters_exception_and_preserves_custody(self):
        req, t, a = self.w.approved_transfer(qty=2)
        self.w.release(t)
        self.w.receive(t, qty=1)
        t.refresh_from_db(); a.refresh_from_db()
        self.assertEqual(t.status, TransferStatus.EXCEPTION)
        self.assertEqual((a.quantity, a.owner_department), (4, self.w.eng))
        # Requester may not resolve; an approver accepts the evidence and the shortfall gets a follow-up transfer.
        with self.assertRaises(AccessDenied):
            workflow.resolve_exception(self.w.requester, t.pk, t.version, "ACCEPT_RECEIVED", "accept partial evidence")
        workflow.resolve_exception(self.w.approver, t.pk, t.version, "ACCEPT_RECEIVED", "Accept one unit, one to follow")
        t.refresh_from_db(); req.refresh_from_db()
        self.assertEqual((t.status, req.received_quantity, req.status), (TransferStatus.COMPLETED, 1, RequestStatus.PARTIALLY_FULFILLED))
        follow = t.reservation.transfers.exclude(pk=t.pk).get()
        self.assertEqual((follow.quantity, follow.status), (1, TransferStatus.APPROVED))

    def test_damage_enters_exception_and_return_to_source_releases_allocation(self):
        req, t, a = self.w.approved_transfer(qty=2)
        self.w.release(t)
        self.w.receive(t, condition="POOR")
        t.refresh_from_db()
        self.assertEqual((t.status, t.exception_kind), (TransferStatus.EXCEPTION, "DAMAGE"))
        workflow.resolve_exception(self.w.approver, t.pk, t.version, "RETURN_TO_SOURCE", "Damaged in transit, returned")
        from apps.catalog.services import free_listed_quantity
        a.refresh_from_db()
        self.assertEqual(free_listed_quantity(a), 4)

    def test_failure_during_completion_rolls_everything_back(self):
        req, t, a = self.w.approved_transfer()
        self.w.release(t)
        events_before, assets_before = LifeCycleEvent.objects.count(), Asset.objects.count()
        t.refresh_from_db()
        with mock.patch("apps.exchange.services.record_receipt", side_effect=RuntimeError("db failure")):
            with self.assertRaises(RuntimeError):
                workflow.confirm_receipt(self.w.cs_cust, t.pk, t.version, 2, "GOOD", True)
        t.refresh_from_db(); a.refresh_from_db()
        self.assertEqual(t.status, TransferStatus.RELEASED)
        self.assertEqual(a.quantity, 4)
        self.assertEqual((LifeCycleEvent.objects.count(), Asset.objects.count()), (events_before, assets_before))

    def test_cancellation_after_approval_needs_approver_and_releases_unreleased_only(self):
        req, t, a = self.w.approved_transfer()
        req.refresh_from_db()
        version = exchange.request_cancellation(self.w.requester, req.pk, req.version, "Project cancelled")
        self.assertEqual(version.kind, ProposalVersion.CANCELLATION)
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.APPROVED, "nothing changes until the approver decides")
        workflow.decide(self.w.approver, version.approvals.get(stage="AUTHORIZE").pk, version.pk, "APPROVED", "ok")
        t.refresh_from_db(); req.refresh_from_db()
        self.assertEqual((t.status, req.status), (TransferStatus.CANCELLED, RequestStatus.CANCELLED))

    def test_released_transfer_cannot_be_cancelled(self):
        req, t, a = self.w.approved_transfer()
        self.w.release(t)
        req.refresh_from_db()
        with self.assertRaises(BusinessError):
            exchange.request_cancellation(self.w.requester, req.pk, req.version, "Changed mind")

    def test_audit_trail_records_actor_and_values(self):
        req, t, a = self.w.approved_transfer()
        self.w.release(t); self.w.receive(t)
        ev = AuditEvent.objects.get(action="Transfer completed")
        self.assertEqual(ev.actor, self.w.cs_cust)
        self.assertEqual(ev.after["department"], "CS")
        with self.assertRaises(Exception):
            ev.reason = "tamper"; ev.save()
