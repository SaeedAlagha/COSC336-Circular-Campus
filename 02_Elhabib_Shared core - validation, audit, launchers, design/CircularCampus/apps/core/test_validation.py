"""NFR-10 — every input is validated on the server; malformed input never causes a server error."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from apps.core import validators as V
from apps.core.errors import BusinessError


class FieldRuleTests(TestCase):
    def test_ports_allow_list(self):
        self.assertEqual(V.ports("hdmi, 2x usb-c; Ethernet"), "HDMI, 2× USB-C, Ethernet")
        self.assertEqual(V.ports(""), "")
        for bad in ("hello there", "HDMI, banana", "99x USB-C", "<b>HDMI</b>"):
            with self.subTest(bad=bad), self.assertRaises(BusinessError) as ctx:
                V.ports(bad)
            self.assertIn("ports", ctx.exception.field_errors)

    def test_serial_dimensions_material(self):
        self.assertEqual(V.serial_number("sn-48213-b"), "SN-48213-B")
        for bad in ("hello there", "ab", "NODIGITS", "x" * 80):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.serial_number(bad)
        self.assertEqual(V.dimensions("140 x 70 cm"), "140 x 70 cm")
        V.dimensions("120x60x75cm")
        for bad in ("big", "140 cm", "0 x 70 cm", "140 x 70"):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.dimensions(bad)
        V.material("Laminated wood")
        with self.assertRaises(BusinessError):
            V.material("1234")

    def test_numbers(self):
        self.assertEqual(V.integer("12", "q"), 12)
        for bad in ("", "abc", "1.5", "-1", "0", "99999999999999", "1e3", True):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.integer(bad, "q", max_value=1000)
        self.assertEqual(V.money("1,250.50", "m"), Decimal("1250.50"))
        for bad in ("NaN", "inf", "1.5e400", "-5", "12.345", "abc", "20000000"):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.money(bad, "m")
        with self.assertRaises(BusinessError) as ctx:
            V.version("not-a-number")
        self.assertEqual(ctx.exception.code, BusinessError.STALE_VERSION)

    def test_text_rules(self):
        self.assertEqual(V.text("  two   spaces ", "t"), "two spaces")
        for bad in ("<script>alert(1)</script>", "a\x00b", "click javascript:x", "img onerror=x", "12345 !!!"):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.text(bad, "t")
        with self.assertRaises(BusinessError):
            V.text("x" * 301, "t")
        V.text("Fits < 5 kg and costs > 100", "t")  # comparison signs are not markup
        self.assertEqual(V.keywords("monitor, HDMI, monitor, 24-inch"), ["monitor", "HDMI", "24-inch"])
        with self.assertRaises(BusinessError):
            V.keywords("ok, " + "x" * 40)
        with self.assertRaises(BusinessError):
            V.place("Room <b>", "room")
        V.place("Lab B / 2.14", "room")
        V.person_name("Mary-Jane O'Neil")
        for bad in ("R2D2", "John <b>", "!!"):
            with self.subTest(bad=bad), self.assertRaises(BusinessError):
                V.person_name(bad)

    def test_uuid_parsing(self):
        self.assertIsNone(V.uuid_or_none("abc"))
        u = uuid.uuid4()
        self.assertEqual(V.uuid_or_none(str(u)), u)
        with self.assertRaises(BusinessError):
            V.parse_uuid("'; DROP TABLE x;--")

