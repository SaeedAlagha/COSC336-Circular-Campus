"""Bonus features: leaderboard, availability alerts, QR labels / scan-to-confirm, photo classification, Arabic UI."""
import json

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from apps.catalog import services as catalog
from apps.core.errors import BusinessError
from apps.core.testing import PW, World
from apps.exchange import services as exchange
from apps.intelligence import classification
from apps.intelligence.gateway import AIGateway
from apps.intelligence.gemini import GeminiClient
from apps.reporting.models import Notification
from apps.workflow import services as workflow
from apps.workflow.models import TransferStatus

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"\x00" * 64


class QRTests(TestCase):
    def setUp(self):
        self.w = World()
        self.c = Client()

    def test_labels_lookup_and_permissions(self):
        a = self.w.asset(tag="A-610")
        self.c.force_login(self.w.eng_cust)
        r = self.c.get(f"/catalog/labels/?asset={a.pk}")
        self.assertContains(r, "<svg")
        self.assertContains(r, "A-610")
        self.assertEqual(self.c.get("/catalog/q/a-610/").status_code, 302)
        self.assertEqual(self.c.get("/catalog/q/not a tag/").status_code, 404)
        self.c.force_login(self.w.requester)
        self.assertEqual(self.c.get(f"/catalog/labels/?asset={a.pk}").status_code, 403)
        self.assertEqual(self.c.post("/catalog/scan/", {"code": "http://x/catalog/q/A-610/"}).status_code, 302)

    def test_scan_to_confirm_receipt(self):
        req, t, asset = self.w.approved_transfer(qty=2)
        self.w.release(t)
        t.refresh_from_db()
        with self.assertRaises(BusinessError):
            workflow.confirm_receipt(self.w.cs_cust, t.pk, t.version, 2, asset.condition, True, "", scanned_code="A-999")
        t.refresh_from_db()
        self.assertEqual(t.status, TransferStatus.RELEASED)  # wrong label changes nothing
        t = workflow.confirm_receipt(self.w.cs_cust, t.pk, t.version, 2, asset.condition, True, "", scanned_code=f"http://campus/catalog/q/{asset.asset_tag}/")
        self.assertEqual(t.status, TransferStatus.COMPLETED)
        self.assertTrue(t.receipt_scan_verified)

