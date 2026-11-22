"""C7/C8/C9 and AI-5: deterministic metrics, notifications, CSV export, narrative review/publication."""
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.reporting import metrics, services
from apps.reporting.models import EmissionFactor, Notification, Report, SustainabilityImpact
from apps.workflow import services as workflow


def period():
    today = timezone.localdate()
    return today - timedelta(days=30), today


class MetricTests(TestCase):
    def setUp(self):
        self.w = World()

    def complete(self, qty=2, asset=None):
        req, t, a = self.w.approved_transfer(qty=qty, asset=asset)
        self.w.release(t)
        self.w.receive(t)
        t.refresh_from_db()
        return req, t, a

    def test_zero_denominator_and_missing_evidence_are_unavailable_not_zero(self):
        m = metrics.compute(*period())["indicators"]
        for key in ("reuse_rate", "waste_diversion", "avoided_value", "net_savings", "carbon", "kg_diverted", "life_extension"):
            self.assertEqual(m[key]["status"], "unavailable", key)
            self.assertIsNone(m[key]["value"], key)

    def test_reuse_and_diversion_formulas_and_repeat_transfer_counted_once(self):
        a = self.w.asset(qty=4, unit_weight_kg=Decimal("5"))
        req, t, _ = self.complete(qty=2, asset=a)
        m = metrics.compute(*period())["indicators"]
        self.assertEqual(m["items_reused"]["value"], 2)
        self.assertEqual(m["reuse_rate"]["value"], 50.0)  # 2 of 4 cohort units
        self.assertEqual(m["kg_diverted"]["value"], 10.0)
        # Re-publish the received batch and transfer the same units again: no double count.
        from apps.catalog import services as catalog
        child = t.received_asset
        child.refresh_from_db()
        catalog.publish(self.w.cs_cust, child.pk, child.version)
        req2 = self.w.request(qty=2, user=self.w.requester)
        # A department cannot request its own stock; move the second transfer to ENG instead.
        from apps.exchange import services as exchange
        eng_req = exchange.create_draft(self.w.user("100060998", "Eng Requester", self.w.eng, [("REQUESTER", self.w.eng)]), dict(
            title="Back to ENG", category=self.w.monitor_cat, purpose="Return", specifications="", mandatory_specs="", quantity=2,
            preferred_condition="GOOD", urgency="NORMAL", destination_campus="Main Campus", destination_building="E2", destination_room="1",
            required_date=timezone.localdate() + timedelta(days=5), allow_partial=False, receiving_custodian=self.w.eng_cust))
        _, v, hold = exchange.submit(eng_req.requester, eng_req.pk, eng_req.version, child.pk, 2)
        for appr in v.approvals.filter(decision__isnull=True).order_by("stage_order"):
            who = self.w.cs_cust if appr.stage == "RELEASE_CHECK" else appr.reviewer
            workflow.decide(who, appr.pk, v.pk, "APPROVED", "ok")
        t2 = hold.transfers.get()
        workflow.release(self.w.cs_cust, t2.pk, t2.version, 2, "Second hand-over slip")
        t2.refresh_from_db()
        workflow.confirm_receipt(self.w.eng_cust, t2.pk, t2.version, 2, "GOOD", True)
        m = metrics.compute(*period())["indicators"]
        self.assertEqual(m["items_reused"]["value"], 2, "the same units are counted once (SUS-09)")
        self.assertEqual(m["items_reused"]["inputs"]["repeat-transfer units excluded"], 2)
        self.assertEqual(SustainabilityImpact.objects.filter(indicator="REUSE", counted=False).count(), 1)

    def test_avoided_purchase_requires_confirmation_and_savings_keep_negative_and_unknown(self):
        req, t, a = self.complete()
        m = metrics.compute(*period())["indicators"]
        self.assertEqual(m["avoided_value"]["status"], "unavailable", "a transfer alone does not prove savings")
        with self.assertRaises(Exception):
            services.review_avoided_purchase(self.w.requester, t, True, "Monitor", 2, Decimal("10"), "Quote Q-1")
        services.review_avoided_purchase(self.w.procurement, t, True, "Monitor", 2, Decimal("10"), "Quote Q-1", repair_cost=Decimal("50"))
        with self.assertRaises(BusinessError):
            services.review_avoided_purchase(self.w.finance, t, True, "Monitor", 2, Decimal("10"), "Quote Q-1")
        m = metrics.compute(*period())["indicators"]
        self.assertEqual(m["avoided_value"]["value"], 20.0)
        self.assertEqual(m["net_savings"]["value"], -30.0, "negative savings stay visible")
        self.assertEqual(m["net_savings"]["status"], "provisional", "transfer cost unknown -> provisional")

    def test_carbon_unavailable_without_factor_and_estimated_with_cited_factor(self):
        req, t, a = self.complete()
        services.review_avoided_purchase(self.w.procurement, t, True, "Monitor", 2, Decimal("1000"), "Price list PL-1")
        self.assertEqual(metrics.compute(*period())["indicators"]["carbon"]["status"], "unavailable")
        with self.assertRaises(Exception):
            services.add_emission_factor(self.w.requester, a.category, Decimal("100"), Decimal("2"), "Some source text", "v1", True)
        services.add_emission_factor(self.w.sustain, a.category, Decimal("100"), Decimal("2"), "Illustrative factor for demo", "v1", True)
        c = metrics.compute(*period())["indicators"]["carbon"]
        self.assertEqual((c["value"], c["status"]), (198.0, "provisional"))
        self.assertIn("illustrative (unverified)", c["note"])

