"""The class-diagram operations (Phase 4-5, Figures 16-18) exist as methods and drive the real workflow."""
from datetime import date

from django.test import TestCase

from apps.core.testing import World
from apps.intelligence.gateway import AIGateway
from apps.workflow.models import Approval, TransferStatus


class ClassDiagramOperationTests(TestCase):
    def test_full_exchange_through_class_methods(self):
        w = World()
        asset = w.asset(qty=4)
        self.assertEqual(asset.free_quantity(), 4)
        self.assertTrue(w.requester.may("view_asset", asset))
        self.assertFalse(w.requester.may("manage_asset", asset))
        req = w.request(qty=2)
        req, version, hold = req.submit(w.requester, asset, 2)
        self.assertTrue(version.matches_current())
        with self.assertRaises(PermissionError):
            hold.allocate()
        for stage in version.approvals.filter(decision__isnull=True).order_by("stage_order"):
            who = w.eng_cust if stage.stage == Approval.RELEASE_CHECK else w.finance if stage.stage == Approval.COST_REVIEW else stage.reviewer
            stage.decide(who, "APPROVED", "ok")
        t = hold.transfers.get()
        t = t.record_release(w.eng_cust, 2, "Handed over with slip RS-9")
        t.refresh_from_db()
        t = t.confirm_receipt(w.cs_cust, 2, "GOOD")
        self.assertEqual(t.status, TransferStatus.COMPLETED)
        self.assertEqual(t.received_asset.owner_department, w.cs)

    def test_maintenance_disposition_and_ai_methods(self):
        w = World()
        asset = w.asset(tag="A-950", publish=False)
        rec = asset.restrict(w.eng_cust, "Display flickers after warm-up")
        from apps.maintenance import services
        rec = services.start_inspection(w.tech, rec.pk, rec.version)
        rec = rec.record_inspection(w.tech, "Loose cable reseated and tested", "SAFE_NO_REPAIR")
        rec = rec.verify_safe_return(w.eng_cust, True, "Ran for an hour with no flicker")
        self.assertEqual(rec.status, "COMPLETED")
        self.assertTrue(AIGateway.validate({"a": 1}, ["a"]))
        self.assertFalse(hasattr(AIGateway, "approve"))


class Figure19RelationshipTests(TestCase):
    """Inheritance, aggregation and composition from design Figure 19 exist as real code."""

    def test_inheritance_ITAsset_and_FurnitureAsset(self):
        from apps.catalog.models import Asset, FurnitureAsset, ITAsset, specialise
        w = World()
        it = w.asset(tag="A-960", type_metadata={"serial_number": "SN-1", "ports": "HDMI, USB-C"})
        chair = w.asset(tag="F-961", category=w.chair_cat, type_metadata={"material": "Mesh", "serial_number": "ignored"})
        self.assertTrue(issubclass(ITAsset, Asset) and issubclass(FurnitureAsset, Asset))
        self.assertEqual(list(ITAsset.objects.values_list("asset_tag", flat=True)), ["A-960"])
        self.assertEqual(list(FurnitureAsset.objects.values_list("asset_tag", flat=True)), ["F-961"])
        s = specialise(Asset.objects.get(pk=it.pk))
        self.assertIsInstance(s, ITAsset)
        self.assertEqual((s.serial_number, s.ports), ("SN-1", ["HDMI", "USB-C"]))
        f = specialise(Asset.objects.get(pk=chair.pk))
        self.assertIsInstance(f, FurnitureAsset)
        self.assertEqual(f.material, "Mesh")
        self.assertNotIn("serial_number", chair.type_metadata, "furniture does not accept IT metadata")

    def test_aggregation_AssetCollection_does_not_own_assets(self):
        from apps.catalog.models import Asset, AssetCollection
        w = World()
        a = w.asset(tag="A-962")
        c = AssetCollection([a])
        self.assertEqual(len(c), 1)
        del c
        self.assertTrue(Asset.objects.filter(pk=a.pk).exists())

    def test_composition_RequestSpecification_owned_by_request(self):
        from apps.exchange.models import RequestSpecification
        w = World()
        req = w.request(qty=3, mandatory_specs="HDMI, 24-inch")
        spec = req.specification
        self.assertIsInstance(spec, RequestSpecification)
        self.assertEqual((spec.quantity, spec.mandatory_terms), (3, ["HDMI", "24-inch"]))
        self.assertEqual(req.mandatory_list, spec.mandatory_terms)
