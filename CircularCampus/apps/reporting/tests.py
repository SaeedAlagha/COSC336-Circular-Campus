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


class NotificationTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_events_notify_the_right_people_and_links_are_rechecked(self):
        a = self.w.asset()
        req, version, hold = self.w.submit(self.w.request(), a, 2)
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="REQUEST_SUBMITTED").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.w.eng_cust, event_code="APPROVAL_NEEDED").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.w.approver, event_code="APPROVAL_NEEDED").exists(), "approver is notified only when their stage opens")
        self.w.approve_all(version)
        self.assertTrue(Notification.objects.filter(recipient=self.w.approver, event_code="APPROVAL_NEEDED").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.w.requester, event_code="REQUEST_APPROVED").exists())
        n = Notification.objects.filter(recipient=self.w.requester).first()
        self.client.force_login(self.w.rep)
        r = self.client.post(reverse("reporting:notifications"), {"id": n.pk, "action": "open"})
        self.assertEqual(r.status_code, 404, "cannot open someone else's notice")
        self.client.force_login(self.w.requester)
        self.client.post(reverse("reporting:notifications"), {"id": n.pk, "action": "read"})
        n.refresh_from_db()
        self.assertIsNotNone(n.read_at)


class CsvAndReportPermissionTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_csv_respects_permission_filters_and_neutralises_formulas(self):
        self.w.request(title="=HYPERLINK(\"http://x\")")
        self.w.request(title="Normal")
        self.client.force_login(self.w.requester)
        self.assertEqual(self.client.get(reverse("reporting:operational"), {"export": "requests"}).status_code, 403)
        self.client.force_login(self.w.auditor)
        r = self.client.get(reverse("reporting:operational"), {"export": "requests", "status": "DRAFT"})
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        body = r.content.decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", body)
        self.assertEqual(body.strip().count("\n"), 2, "header + 2 draft requests")
        r = self.client.get(reverse("reporting:operational"), {"export": "requests", "status": "FULFILLED"})
        self.assertEqual(r.content.decode("utf-8-sig").strip().count("\n"), 0)
        self.assertEqual(services.csv_safe("+1"), "'+1")
        self.assertEqual(services.csv_safe("@SUM"), "'@SUM")

    def test_department_scoped_officer_cannot_report_on_other_departments(self):
        scoped = self.w.user("100060600", "Scoped Fin", self.w.cs, [("FINANCE", self.w.cs)])
        self.client.force_login(scoped)
        self.assertEqual(self.client.get(reverse("reporting:operational"), {"department": self.w.eng.pk}).status_code, 403)


class NarrativeTests(TestCase):
    """AI feature 5: totals from the database; draft never publishes; edit/review/publish are explicit."""

    def setUp(self):
        self.w = World()
        start, end = period()
        self.report = services.create_report(self.w.sustain, Report.MONTHLY, start, end)

    def fake_gateway(self, text):
        from apps.intelligence.gateway import AIGateway

        client = mock.Mock(configured=True, model="gemini-test", embedding_model="emb", timeout=5)
        client.generate_json.return_value = ({"narrative": text, "cited_metrics": ["items_reused", "not_a_metric"]}, "gemini-test-001")
        return AIGateway(client=client)

    def test_requester_cannot_create_reports(self):
        start, end = period()
        with self.assertRaises(Exception):
            services.create_report(self.w.requester, Report.MONTHLY, start, end)

    def test_generated_draft_is_not_published_and_invented_numbers_block_review(self):
        r, outcome = services.generate_narrative(self.w.sustain, self.report.pk, gateway=self.fake_gateway(
            "This period 38 items were reused, saving AED 41,200 for campus departments overall."))
        self.assertFalse(outcome.fallback)
        self.assertEqual((r.status, r.narrative_source), (Report.DRAFT, Report.AI))
        self.assertIn("38", r.validation["unsupported_numbers"])
        self.assertEqual(outcome.result.model_version.revision, "gemini-test-001")
        with self.assertRaises(BusinessError):
            services.mark_reviewed(self.w.sustain, r.pk, r.version, True)
        with self.assertRaises(BusinessError):
            services.publish(self.w.sustain, r.pk, r.version)
        r = services.edit_narrative(self.w.sustain, r.pk, r.version, "No completed transfers were recorded this period; savings are unavailable.")
        self.assertTrue(r.validation["ok"])
        self.assertNotEqual(r.narrative_text, r.narrative_original, "original draft preserved")
        r = services.mark_reviewed(self.w.sustain, r.pk, r.version, True)
        r = services.publish(self.w.sustain, r.pk, r.version)
        self.assertEqual(r.status, Report.PUBLISHED)
        with self.assertRaises(BusinessError):
            services.edit_narrative(self.w.sustain, r.pk, r.version, "Changing a published report should fail.")

    def test_edit_after_review_requires_fresh_review(self):
        r = services.edit_narrative(self.w.sustain, self.report.pk, self.report.version, "No transfers were completed in this period.")
        r = services.mark_reviewed(self.w.sustain, r.pk, r.version, True)
        r = services.edit_narrative(self.w.sustain, r.pk, r.version, "No transfers were completed in this period at all.")
        self.assertEqual(r.status, Report.DRAFT)
        with self.assertRaises(BusinessError):
            services.publish(self.w.sustain, r.pk, r.version)

    def test_gemini_unavailable_gives_labelled_template_draft(self):
        r, outcome = services.generate_narrative(self.w.sustain, self.report.pk)  # no key configured in tests
        self.assertTrue(outcome.fallback)
        self.assertEqual(r.narrative_source, Report.TEMPLATE)
        self.assertTrue(r.validation["ok"], "template uses only snapshot figures")

    def test_only_minimal_snapshot_is_sent(self):
        gw = self.fake_gateway("Nothing notable happened this period across the campus departments.")
        services.generate_narrative(self.w.sustain, self.report.pk, gateway=gw)
        system, prompt, schema = gw.client.generate_json.call_args[0][:3]
        self.assertNotIn("password", prompt.lower())
        self.assertNotIn(self.w.requester.full_name, prompt)
        self.assertIn("indicators", prompt)
