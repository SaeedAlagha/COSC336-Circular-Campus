"""C2/C3/C6: registration validation, attachments, publication, search filters, state machine."""
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.catalog import services
from apps.catalog.models import Asset, AssetStatus
from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.maintenance.models import LifeCycleEvent
import tempfile

TMP = tempfile.mkdtemp()


class RegistrationTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_draft_until_published_and_unknowns_stay_null(self):
        a = self.w.asset(publish=False)
        self.assertEqual(a.status, AssetStatus.DRAFT)
        self.assertIsNone(a.purchase_date)
        self.assertIsNone(a.unit_value_aed)
        self.assertIsNone(a.age_years())
        ev = LifeCycleEvent.objects.get(asset=a, event_type="REGISTERED")
        self.assertEqual(ev.actor, self.w.eng_cust)

    def test_duplicate_id_and_invalid_values_rejected_without_partial_record(self):
        self.w.asset(tag="A-200")
        with self.assertRaises(BusinessError):
            self.w.asset(tag="A-200")
        from django.core.exceptions import ValidationError
        with self.assertRaises((BusinessError, ValidationError)):
            self.w.asset(tag="A-201", unit_value_aed=Decimal("-5"))
        self.assertFalse(Asset.objects.filter(asset_tag="A-201").exists())

    def test_form_level_validation_via_http(self):
        self.client.force_login(self.w.eng_cust)
        r = self.client.post(reverse("catalog:new"), {"asset_tag": "bad id", "name": "", "description": "", "quantity": 0, "category": self.w.monitor_cat.pk,
                                                       "owner_department": self.w.eng.pk, "campus": "Main", "building": "E2", "room": "1", "condition": "GOOD", "action": "save"})
        self.assertEqual(r.status_code, 200)
        form = r.context["form"]
        for field in ("asset_tag", "name", "description", "quantity"):
            self.assertIn(field, form.errors)
        self.assertFalse(Asset.objects.exists())

    def test_edit_cannot_change_custody_and_corrections_need_reason(self):
        a = self.w.asset()
        data = {"name": "Renamed", "description": a.description, "category": a.category, "custodian": self.w.cs_cust, "owner_department": self.w.cs}
        a = services.update_asset(self.w.eng_cust, a.pk, a.version, data)
        self.assertEqual((a.custodian, a.owner_department, a.name), (self.w.eng_cust, self.w.eng, "Renamed"))
        with self.assertRaises(BusinessError):
            services.update_asset(self.w.eng_cust, a.pk, a.version, {"condition": "FAIR"})
        a = services.update_asset(self.w.eng_cust, a.pk, a.version, {"condition": "FAIR"}, "Scratches found on audit")
        ev = LifeCycleEvent.objects.filter(asset=a, event_type="CORRECTION").latest("occurred_at")
        self.assertEqual((ev.before["condition"], ev.after["condition"]), ("GOOD", "FAIR"))

    def test_stale_edit_rejected(self):
        a = self.w.asset()
        services.update_asset(self.w.eng_cust, a.pk, a.version, {"name": "One"})
        with self.assertRaises(BusinessError) as ctx:
            services.update_asset(self.w.eng_cust, a.pk, a.version, {"name": "Two"})
        self.assertEqual(ctx.exception.code, BusinessError.STALE_VERSION)

    def test_location_change_blocked_while_stock_committed(self):
        a = self.w.asset()
        self.w.submit(self.w.request(), a, 2)
        a.refresh_from_db()
        with self.assertRaises(BusinessError):
            services.update_asset(self.w.eng_cust, a.pk, a.version, {"building": "Z9"}, "Moved to storage")


@override_settings(PROTECTED_MEDIA_ROOT=__import__("pathlib").Path(TMP))
class AttachmentTests(TestCase):
    def setUp(self):
        self.w = World()
        self.a = self.w.asset()

    def test_accepts_real_png_rejects_spoofed_and_oversized(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        att = services.add_attachment(self.w.eng_cust, self.a.pk, SimpleUploadedFile("p.png", png, "image/png"))
        self.assertEqual((att.attachment_no, att.mime_type, att.size_bytes), (1, "image/png", len(png)))
        with self.assertRaises(BusinessError):
            services.add_attachment(self.w.eng_cust, self.a.pk, SimpleUploadedFile("evil.pdf", b"MZ not a pdf", "application/pdf"))
        with self.assertRaises(BusinessError):
            services.add_attachment(self.w.eng_cust, self.a.pk, SimpleUploadedFile("x.exe", b"MZ", "application/octet-stream"))
        with self.settings(ATTACHMENT_MAX_BYTES=50):
            with self.assertRaises(BusinessError):
                services.add_attachment(self.w.eng_cust, self.a.pk, SimpleUploadedFile("big.png", png, "image/png"))

    def test_download_follows_asset_permission(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 10
        services.add_attachment(self.w.eng_cust, self.a.pk, SimpleUploadedFile("p.png", png, "image/png"))
        draft = self.w.asset(tag="A-300", publish=False)
        services.add_attachment(self.w.eng_cust, draft.pk, SimpleUploadedFile("d.png", png, "image/png"))
        self.client.force_login(self.w.requester)
        self.assertEqual(self.client.get(reverse("catalog:attachment", args=[self.a.pk, 1])).status_code, 200)
        self.assertEqual(self.client.get(reverse("catalog:attachment", args=[draft.pk, 1])).status_code, 403)


class PublicationAndSearchTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_unpublish_preserves_existing_holds(self):
        a = self.w.asset()
        req, version, hold = self.w.submit(self.w.request(), a, 2)
        a.refresh_from_db()
        a, committed = services.unpublish(self.w.eng_cust, a.pk, a.version, "No longer surplus")
        hold.refresh_from_db()
        self.assertEqual((committed, hold.status), (2, "PENDING_APPROVAL"))
        self.assertEqual(services.free_listed_quantity(a), 0)
        self.w.approve_all(version)
        hold.refresh_from_db()
        self.assertEqual(hold.status, "ALLOCATED", "existing commitment still resolves through its workflow")

    def test_poor_or_restricted_stock_cannot_be_published(self):
        a = self.w.asset(tag="A-400", publish=False, condition="POOR")
        with self.assertRaises(BusinessError):
            services.publish(self.w.eng_cust, a.pk, a.version)

    def test_filters_keyword_category_condition_department_and_unknown_age(self):
        from datetime import date
        self.w.asset(tag="A-500", name="Dell monitor", purchase_date=date(2020, 1, 1))
        self.w.asset(tag="A-501", name="Office chair", category=self.w.chair_cat, condition="FAIR", description="Mesh chair", specifications="Mesh back; fixed arms")
        self.client.force_login(self.w.requester)
        tags = lambda r: [row["asset"].asset_tag for row in r.context["page"].object_list]  # noqa: E731
        self.assertEqual(tags(self.client.get(reverse("catalog:list"), {"q": "chair"})), ["A-501"])
        self.assertEqual(tags(self.client.get(reverse("catalog:list"), {"category": self.w.monitor_cat.root.pk})), ["A-500"])
        self.assertEqual(tags(self.client.get(reverse("catalog:list"), {"condition": "GOOD"})), ["A-500"])
        self.assertEqual(len(tags(self.client.get(reverse("catalog:list"), {"department": self.w.eng.pk}))), 2)
        self.assertEqual(tags(self.client.get(reverse("catalog:list"), {"min_age": "1"})), ["A-500"], "unknown age never matches a range")
        r = self.client.get(reverse("catalog:list"), {"min_age": "5", "max_age": "1"})
        self.assertIn("max_age", r.context["form"].errors)


class StateMachineTests(TestCase):
    def setUp(self):
        self.w = World()

    def test_invalid_transition_refused(self):
        a = self.w.asset(publish=False)
        with self.assertRaises(BusinessError) as ctx:
            services.transition(a, AssetStatus.DONATED, self.w.eng_cust)
        self.assertEqual(ctx.exception.code, BusinessError.INVALID_TRANSITION)

    def test_transition_table_matches_design_examples(self):
        T = AssetStatus.can_move
        self.assertTrue(T("DRAFT", "AVAILABLE"))
        self.assertTrue(T("RESERVED", "TRANSFER_PENDING"))
        self.assertTrue(T("TRANSFER_PENDING", "IN_USE"))
        self.assertTrue(T("UNDER_INSPECTION", "IN_MAINTENANCE"))
        self.assertTrue(T("UNAVAILABLE", "DISPOSITION_PENDING"))
        self.assertFalse(T("AVAILABLE", "DISPOSED"))
        self.assertFalse(T("DRAFT", "TRANSFER_PENDING"))
        self.assertFalse(T("DISPOSED", "AVAILABLE"))

    def test_full_hold_moves_listing_to_reserved_then_transfer_pending(self):
        a = self.w.asset(qty=2)
        req, version, hold = self.w.submit(self.w.request(qty=2), a, 2)
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.RESERVED)
        self.w.approve_all(version)
        a.refresh_from_db()
        self.assertEqual(a.status, AssetStatus.TRANSFER_PENDING)

    def test_events_are_append_only(self):
        a = self.w.asset()
        ev = LifeCycleEvent.objects.filter(asset=a).first()
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            ev.reason = "rewrite"; ev.save()
        with self.assertRaises(ValidationError):
            ev.delete()
