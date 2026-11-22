"""Maintenance safety, verified return and evidence-backed disposition (FR-20..24, FR-53/54)."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.catalog.models import AssetStatus
from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.exchange.models import Reservation
from apps.maintenance import services as maint
from apps.maintenance.models import AssetLineage, Disposition, MaintenanceStatus
from apps.workflow import services as workflow
from apps.workflow.models import Approval


class MaintenanceTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_defect_restricts_stock_flags_holds_and_blocks_release(self):
        req, t, a = self.w.approved_transfer()
        rec = maint.report_defect(self.w.requester, a.pk, "Screen flickers badly when powered on")
        a.refresh_from_db()
        self.assertEqual((a.status, a.is_published), (AssetStatus.UNDER_INSPECTION, False))
        self.assertTrue(Reservation.objects.get(pk=t.reservation_id).flagged_for_review)
        t.refresh_from_db()
        with self.assertRaises(BusinessError) as ctx:
            workflow.release(self.w.eng_cust, t.pk, t.version, 2, "Should be blocked now")
        self.assertEqual(ctx.exception.code, BusinessError.UNSAFE_STOCK)
        # Inspection safe -> custodian (not technician) verifies -> back in use, flags cleared.
        rec = maint.start_inspection(self.w.tech, rec.pk, rec.version)
        rec = maint.record_inspection(self.w.tech, rec.pk, rec.version, "Loose cable reseated", "SAFE_NO_REPAIR")
        from apps.accounts.policy import AccessDenied
        with self.assertRaises((BusinessError, AccessDenied)):
            maint.verify(self.w.tech, rec.pk, rec.version, True, "self verification attempt")
        maint.verify(self.w.eng_cust, rec.pk, rec.version, True, "Powered on for 1 hour, no flicker")
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.IN_USE)
        self.assertFalse(Reservation.objects.get(pk=t.reservation_id).flagged_for_review)
        t.refresh_from_db()
        workflow.release(self.w.eng_cust, t.pk, t.version, 2, "Released after verified safe return")

    def test_repair_requires_approval_before_work(self):
        a = self.w.asset(tag="A-700", publish=False, expected_retirement_date=date.today() + timedelta(days=30))
        rec = maint.report_defect(self.w.eng_cust, a.pk, "Backlight failed on the left side")
        rec = maint.start_inspection(self.w.tech, rec.pk, rec.version)
        rec = maint.record_inspection(self.w.tech, rec.pk, rec.version, "Backlight inverter failed", "NEEDS_REPAIR", "REPAIR", "Replace inverter board", Decimal("90"))
        self.assertEqual(rec.status, MaintenanceStatus.AWAITING_APPROVAL)
        with self.assertRaises(BusinessError):
            maint.record_work(self.w.tech, rec.pk, rec.version, "Replaced inverter early", Decimal("90"), "GOOD")
        version = rec.proposal_versions.get()
        self.assertIsNone(version.approvals.get(stage="COST_REVIEW").decision, "cost present -> finance review")
        for appr in version.approvals.filter(decision__isnull=True).order_by("stage_order"):
            who = self.w.finance if appr.stage == Approval.COST_REVIEW else appr.reviewer
            workflow.decide(who, appr.pk, version.pk, "APPROVED", "ok")
        rec.refresh_from_db(); a.refresh_from_db()
        self.assertEqual((rec.status, a.status), (MaintenanceStatus.IN_SERVICE, AssetStatus.IN_MAINTENANCE))
        rec = maint.record_work(self.w.tech, rec.pk, rec.version, "Inverter replaced and tested", None, "GOOD")
        self.assertIsNone(rec.actual_cost_aed, "unknown cost stays unknown")
        maint.verify(self.w.eng_cust, rec.pk, rec.version, True, "Burn-in passed", "GOOD", date.today() + timedelta(days=400))
        from apps.reporting.models import SustainabilityImpact
        ext = SustainabilityImpact.objects.get(asset=a, indicator="LIFE_EXTENSION")
        self.assertGreater(ext.value, 0)

    def test_unrepairable_goes_unavailable_never_auto_disposed(self):
        a = self.w.asset(tag="A-701", publish=False)
        rec = maint.report_defect(self.w.eng_cust, a.pk, "Cracked panel after a fall")
        rec = maint.start_inspection(self.w.tech, rec.pk, rec.version)
        maint.record_inspection(self.w.tech, rec.pk, rec.version, "Panel cracked beyond repair", "UNREPAIRABLE")
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.UNAVAILABLE)
        self.assertFalse(Disposition.objects.exists())


class DispositionTests(TestCase):
    def setUp(self):
        self.w = World()
        self.a = self.w.asset(tag="A-800", qty=3, publish=False)
        from apps.catalog import services as catalog
        catalog.transition(self.a, AssetStatus.UNDER_INSPECTION, self.w.eng_cust)
        catalog.transition(self.a, AssetStatus.UNAVAILABLE, self.w.eng_cust)
        self.a.refresh_from_db()

    def propose(self, action="RECYCLE", qty=3):
        return maint.propose_disposition(self.w.eng_cust, self.a.pk, self.a.version, action, qty, "No longer serviceable or wanted",
                                         "Reuse and repair assessed, not viable", "Inspection M-1", None, "City Recycling Co")

    def test_completion_needs_approval_and_evidence(self):
        d = self.propose()
        with self.assertRaises(BusinessError):
            maint.complete_disposition(self.w.eng_cust, d.pk, date.today(), 3, "City Recycling Co", "Certificate CR-1")
        version = d.proposal_versions.get()
        workflow.decide(self.w.eng_approver, version.approvals.get(stage="AUTHORIZE").pk, version.pk, "APPROVED", "ok")
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, AssetStatus.DISPOSITION_PENDING, "approval alone is not completion")
        with self.assertRaises(BusinessError):
            maint.complete_disposition(self.w.eng_cust, d.pk, date.today(), 3, "City Recycling Co", "short")
        with self.assertRaises(BusinessError):
            maint.complete_disposition(self.w.eng_cust, d.pk, date.today(), 2, "City Recycling Co", "Certificate CR-2026-55")
        maint.complete_disposition(self.w.eng_cust, d.pk, date.today(), 3, "City Recycling Co", "Certificate CR-2026-55")
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, AssetStatus.RECYCLED)

    def test_proposer_cannot_authorise_own_disposition(self):
        from apps.accounts.models import Role, UserRole
        UserRole.objects.create(user=self.w.eng_cust, role=Role.objects.get(code="APPROVER"), scope_department=self.w.eng)
        d = self.propose()
        v = d.proposal_versions.get()
        with self.assertRaises(BusinessError) as ctx:
            workflow.decide(self.w.eng_cust, v.approvals.get(stage="AUTHORIZE").pk, v.pk, "APPROVED", "")
        self.assertEqual(ctx.exception.code, BusinessError.SELF_APPROVAL)

    def test_partial_disposition_splits_and_component_recovery_creates_linked_stock(self):
        d = self.propose("COMPONENT_RECOVERY", qty=1)
        self.a.refresh_from_db()
        self.assertEqual(self.a.quantity, 2)
        self.assertTrue(AssetLineage.objects.filter(parent_asset=self.a, kind="SPLIT").exists())
        v = d.proposal_versions.get()
        workflow.decide(self.w.eng_approver, v.approvals.get(stage="AUTHORIZE").pk, v.pk, "APPROVED", "ok")
        maint.complete_disposition(self.w.eng_cust, d.pk, date.today(), 1, "", "Workshop log WL-9 disassembly", "16GB RAM module; 2")
        rec = AssetLineage.objects.get(kind="RECOVERED_COMPONENT")
        self.assertEqual((rec.child_asset.quantity, rec.child_asset.status), (2, AssetStatus.DRAFT))

    def test_rejection_returns_asset_to_unavailable(self):
        d = self.propose()
        v = d.proposal_versions.get()
        workflow.decide(self.w.eng_approver, v.approvals.get(stage="AUTHORIZE").pk, v.pk, "REJECTED", "Try donation first")
        self.a.refresh_from_db()
        self.assertEqual(self.a.status, AssetStatus.UNAVAILABLE)
