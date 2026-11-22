"""Bonus features: leaderboard, availability alerts, QR labels / scan-to-confirm, photo classification, Arabic UI."""
import json

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from apps.catalog import services as catalog
from apps.core.errors import BusinessError
from apps.core.testing import PW, World
from apps.exchange import services as exchange
from apps.exchange.models import SavedSearch, WishlistHit
from apps.intelligence import classification
from apps.intelligence.gateway import AIGateway
from apps.intelligence.gemini import GeminiClient
from apps.intelligence.tests import CFG, gen_payload, opener_returning
from apps.reporting import leaderboard
from apps.reporting.models import Notification
from apps.workflow import services as workflow
from apps.workflow.models import TransferStatus

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"\x00" * 64


class LeaderboardTests(TestCase):
    def test_points_follow_published_formula_and_badges(self):
        w = World()
        rows, _ = leaderboard.build("all")
        self.assertTrue(all(r.points == 0 for r in rows))
        req, t, asset = w.approved_transfer(qty=2)
        w.release(t)
        w.receive(t)
        rows = {r.department.code: r for r in leaderboard.build("all")[0]}
        self.assertEqual((rows["ENG"].given, rows["CS"].received), (2, 2))
        self.assertEqual(rows["ENG"].points, 2 * leaderboard.POINTS["given"])
        self.assertEqual(rows["CS"].points, 2 * leaderboard.POINTS["received"])
        self.assertEqual([b.code for b in rows["ENG"].badges], ["FIRST_SHARE"])
        self.assertEqual(rows["ENG"].rank, 1)

    def test_page_visible_to_staff_and_money_hidden_from_requesters(self):
        w = World()
        c = Client()
        c.force_login(w.requester)
        r = c.get("/reports/leaderboard/?period=nonsense")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Avoided (AED)")
        c.force_login(w.finance)
        self.assertContains(c.get("/reports/leaderboard/"), "Avoided (AED)")


class AvailabilityAlertTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_validation(self):
        with self.assertRaises(BusinessError) as ctx:
            exchange.save_search(self.w.requester, "Anything", "", None)
        self.assertIn("keywords", ctx.exception.field_errors)
        with self.assertRaises(BusinessError):
            exchange.save_search(self.w.requester, "<b>x</b>", "monitor")
        with self.assertRaises(BusinessError):
            exchange.save_search(self.w.requester, "Monitors", "monitor", min_quantity="0")
        with self.assertRaises(PermissionDenied):
            exchange.save_search(self.w.eng_cust, "Monitors", "monitor")  # custodian is not a requester
        for i in range(SavedSearch.MAX_PER_USER):
            exchange.save_search(self.w.requester, f"Alert {i}", "monitor")
        with self.assertRaises(BusinessError):
            exchange.save_search(self.w.requester, "One too many", "monitor")

    def test_publish_notifies_once_and_respects_rules(self):
        s, existing = exchange.save_search(self.w.requester, "HDMI monitors", "hdmi monitor", self.w.monitor_cat.parent, "GOOD", 2)
        self.assertEqual(existing, [])
        asset = self.w.asset(tag="A-501", qty=3)  # publishes
        self.assertEqual(WishlistHit.objects.filter(search=s, asset=asset).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.w.requester, event_code="WISHLIST_MATCH").count(), 1)
        asset.refresh_from_db()
        a, _ = catalog.unpublish(self.w.eng_cust, asset.pk, asset.version)
        catalog.publish(self.w.eng_cust, a.pk, a.version)
        self.assertEqual(Notification.objects.filter(recipient=self.w.requester, event_code="WISHLIST_MATCH").count(), 1)  # no repeat
        self.w.asset(tag="A-502", qty=3, condition="FAIR")      # below minimum condition
        self.w.asset(tag="A-503", qty=1)                        # fewer than 2 free
        self.w.asset(tag="A-504", qty=3, name="Office chair", description="Mesh chair", specifications="Mesh back", category=self.w.chair_cat)
        self.assertEqual(WishlistHit.objects.filter(search=s).count(), 1)

    def test_own_department_stock_does_not_alert(self):
        exchange.save_search(self.w.requester, "Monitors", "monitor")
        a = catalog.register_asset(self.w.cs_cust, dict(asset_tag="A-510", name="CS monitor", description="Spare monitor here", category=self.w.monitor_cat,
                                                        quantity=2, owner_department=self.w.cs, custodian=self.w.cs_cust, campus="Main Campus",
                                                        building="B3", room="1", condition="GOOD"))
        catalog.publish(self.w.cs_cust, a.pk, a.version)
        self.assertFalse(WishlistHit.objects.exists())


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


class PhotoClassificationTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_rejects_non_images(self):
        with self.assertRaises(ValueError):
            classification.suggest_from_photo(self.w.eng_cust, SimpleUploadedFile("a.pdf", PDF, "application/pdf"))
        with self.assertRaises(ValueError):
            classification.suggest_from_photo(self.w.eng_cust, SimpleUploadedFile("a.png", b"not an image", "image/png"))

    def test_without_gemini_says_photo_not_analysed(self):
        out = classification.suggest_from_photo(self.w.eng_cust, SimpleUploadedFile("a.png", PNG, "image/png"),
                                                gateway=AIGateway(GeminiClient({**CFG, "API_KEY": ""})))
        self.assertTrue(out.fallback)
        self.assertIsNone(out.output["category_id"])
        self.assertIn("Photo NOT analysed", out.output["reasons"][0])

    def test_gemini_receives_inline_image(self):
        op = opener_returning(gen_payload({"category": "IT Equipment / Monitor", "condition": "FAIR", "tags": ["monitor"],
                                           "standardized_description": "Monitor with scuffed stand", "reasons": ["screen and stand visible"]}))
        out = classification.suggest_from_photo(self.w.eng_cust, SimpleUploadedFile("a.png", PNG, "image/png"), "Old monitor",
                                                gateway=AIGateway(GeminiClient(CFG, opener=op)))
        self.assertFalse(out.fallback)
        self.assertEqual(out.output["condition"], "FAIR")
        body = json.loads(op.calls[0][0].data)
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "image/png")
        self.assertIn("<staff_text>", parts[0]["text"])


class ArabicInterfaceTests(TestCase):
    def test_arabic_is_rtl_and_translated(self):
        w = World()
        c = Client(HTTP_ACCEPT_LANGUAGE="ar")
        c.force_login(w.requester)
        r = c.get("/catalog/")
        self.assertContains(r, 'dir="rtl"')
        self.assertContains(r, "الكتالوج")
        c = Client()
        c.force_login(w.requester)
        self.assertContains(c.get("/catalog/"), 'dir="ltr"')
        c.post("/i18n/setlang/", {"language": "ar", "next": "/catalog/"})
        self.assertContains(c.get("/catalog/"), "طلباتي")

    def test_every_string_has_a_translation(self):
        from pathlib import Path

        from django.conf import settings

        loc = Path(settings.BASE_DIR) / "locale"
        ids = json.loads((loc / "msgids_v1.json").read_text(encoding="utf-8"))
        ar = []
        for part in sorted((loc / "ar").glob("part*.json")):
            ar += json.loads(part.read_text(encoding="utf-8"))
        self.assertEqual(len(ids), len(ar))
        self.assertTrue(all(isinstance(x, str) and x.strip() for x in ar))
        self.assertTrue((loc / "ar" / "LC_MESSAGES" / "django.mo").exists())

    def test_sign_in_page_works_in_arabic(self):
        World()
        c = Client(HTTP_ACCEPT_LANGUAGE="ar")
        r = c.post("/sign-in/", {"campus_id": "100060101", "password": PW})
        self.assertEqual(r.status_code, 302)
