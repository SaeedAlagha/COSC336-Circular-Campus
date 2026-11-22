"""The class-diagram operations (Phase 4-5, Figures 16-18) exist as methods and drive the real workflow."""
from datetime import date

from django.test import TestCase

from apps.core.testing import World
from apps.workflow.models import Approval, TransferStatus


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
