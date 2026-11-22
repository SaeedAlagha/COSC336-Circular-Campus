"""NFR-10 — every input is validated on the server; malformed input never causes a server error."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from apps.catalog import services as catalog
from apps.catalog.forms import AssetForm
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.testing import World
from apps.exchange.forms import RequestForm
from apps.intelligence import assistant


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


class ServiceAndFormTests(TestCase):
    def setUp(self):
        self.w = World()

    def asset_data(self, **kw):
        d = dict(asset_tag="A-900", name="Dell monitor", description="24-inch monitor, tested", specifications="HDMI", tags="monitor",
                 category=self.w.monitor_cat, quantity=2, owner_department=self.w.eng, custodian=self.w.eng_cust, campus="Main Campus",
                 building="E2", room="101", condition="GOOD", type_metadata={"ports": "HDMI"})
        d.update(kw)
        return d

    def test_register_rejects_bad_ports_with_field_error(self):
        with self.assertRaises(BusinessError) as ctx:
            catalog.register_asset(self.w.eng_cust, self.asset_data(type_metadata={"ports": "hello there"}))
        self.assertIn("ports", ctx.exception.field_errors)
        a = catalog.register_asset(self.w.eng_cust, self.asset_data(type_metadata={"ports": "hdmi, usb-c", "material": "ignored for IT"}))
        self.assertEqual(a.type_metadata, {"ports": "HDMI, USB-C"})

    def test_register_rejects_inconsistent_values_and_dates(self):
        today = timezone.localdate()
        cases = [
            dict(unit_value_aed=Decimal("900"), replacement_value_aed=Decimal("500")),
            dict(quantity=20000),
            dict(unit_weight_kg=Decimal("0")),
            dict(purchase_date=today + timedelta(days=1)),
            dict(purchase_date=today - timedelta(days=400), available_from=today - timedelta(days=500)),
            dict(available_from=today + timedelta(days=365 * 3)),
            dict(name="<img src=x onerror=alert(1)>"),
            dict(room="1\x002"),
            dict(specifications=""),  # FR-05: specifications are required
        ]
        for i, extra in enumerate(cases):
            with self.subTest(extra=extra), self.assertRaises(BusinessError):
                catalog.register_asset(self.w.eng_cust, self.asset_data(asset_tag=f"A-9{i:02d}", **extra))

    def test_asset_form_shows_field_errors(self):
        form = AssetForm(data={"asset_tag": "A-1", "name": "<b>x</b>", "description": "ok desc here", "category": self.w.monitor_cat.pk, "quantity": "0",
                               "owner_department": self.w.eng.pk, "campus": "Main", "building": "E2", "room": "1", "condition": "GOOD",
                               "ports": "hello there", "dimensions": "huge", "unit_value_aed": "50", "replacement_value_aed": "10"}, user=self.w.eng_cust)
        self.assertFalse(form.is_valid())
        for f in ("name", "quantity", "ports", "dimensions", "unit_value_aed"):
            self.assertIn(f, form.errors, f)

    def test_request_rules(self):
        base = dict(title="Monitors", category=self.w.monitor_cat.pk, purpose="Teaching lab", quantity="2", preferred_condition="GOOD", urgency="NORMAL",
                    required_date=(timezone.localdate() + timedelta(days=7)).isoformat(), destination_campus="Main Campus", destination_building="B3",
                    destination_room="204", receiving_custodian=self.w.cs_cust.pk)
        self.assertTrue(RequestForm(data=base, department=self.w.cs).is_valid())
        for field, value in (("quantity", "5000"), ("required_date", (timezone.localdate() + timedelta(days=1000)).isoformat()), ("destination_room", "<x>"),
                             ("urgency", "SUPER"), ("title", "12345"), ("mandatory_specs", "a" * 40)):
            with self.subTest(field=field):
                form = RequestForm(data={**base, field: value}, department=self.w.cs)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
        with self.assertRaises(BusinessError) as ctx:
            self.w.request(qty=5000, destination_room="")
        self.assertTrue({"quantity", "destination_room"} <= set(ctx.exception.field_errors))

    def test_stored_text_guard_catches_any_code_path(self):
        a = self.w.asset(tag="A-950")
        a.description = "safe\x07text"
        with self.assertRaises(BusinessError):
            a.save()

    def test_assistant_question_limits(self):
        with self.assertRaises(ValueError):
            assistant.ask(self.w.requester, "x " * 300)
        with self.assertRaises(ValueError):
            assistant.ask(self.w.requester, "\x00\x01 ?? !!")


class NoServerErrorTests(TestCase):
    """Tampered and garbage form posts get a message, 400 or 404 — never a 500."""

    GARBAGE = ["", "abc", "-5", "1.5e400", "<script>x</script>", "x" * 5000, "'; DROP TABLE x;--", str(uuid.uuid4())]

    def test_garbage_posts(self):
        w = World()
        a = w.asset(tag="A-960")
        req = w.request()
        targets = [
            (w.eng_cust, f"/catalog/{a.pk}/action/", ["publish", "unpublish"]),
            (w.eng_cust, "/catalog/new/", ["save", "apply_suggestion"]),
            (w.eng_cust, f"/maintenance/dispositions/propose/{a.pk}/", [""]),
            (w.requester, f"/requests/{req.pk}/action/", ["submit", "reserve", "withdraw", "cancel"]),
            (w.requester, "/requests/new/", [""]),
            (w.admin, f"/admin/accounts/{w.requester.pk}/", ["edit", "revoke", "password"]),
            (w.admin, "/admin/departments/", ["create", "update", "deactivate"]),
            (w.requester, "/reports/notifications/", ["open"]),
            (w.sustain, "/reports/sustainability/", [""]),
            (w.sustain, f"/reports/ai-reports/{uuid.uuid4()}/", ["save", "publish"]),
        ]
        fields = ["version", "version_id", "quantity", "asset", "match", "id", "grant", "dept", "category", "advice", "reason", "evidence",
                  "name", "description", "campus", "building", "room", "code", "full_name", "email", "title", "purpose", "required_date",
                  "replacement", "transport", "source", "estimated_cost", "justification", "alternatives", "ai_result", "prefill_asset"]
        for user, path, actions in targets:
            c = Client(raise_request_exception=False)
            c.force_login(user)
            for action in actions:
                for g in self.GARBAGE:
                    data = {f: g for f in fields}
                    if action:
                        data["action"] = action
                    r = c.post(path, data)
                    self.assertLess(r.status_code, 500, f"{path} {action} {g[:20]!r}")
        c = Client(raise_request_exception=False)
        c.force_login(w.requester)
        self.assertLess(c.get("/requests/new/?asset=notauuid&quantity=abc&category=zz").status_code, 500)
