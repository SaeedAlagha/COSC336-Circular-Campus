"""Load clearly SYNTHETIC demonstration data.

Safe by design: it refuses to run when any business data already exists (it
never deletes or overwrites records). Every historical scenario is executed
through the real workflow services (submit → stage reviews → release →
receipt, defects → inspection → approved repair → verification, disposition →
approval → completion evidence), then its timestamps are shifted into the past
so dashboards have a time axis. All people, assets and amounts are fictional.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.accounts.models import Department, Role, User, UserRole
from apps.catalog import services as catalog
from apps.catalog.models import Asset, Category
from apps.exchange import services as exchange
from apps.maintenance import services as maint
from apps.workflow import services as workflow
from apps.workflow.models import Approval

DEFAULT_PASSWORD = "CircularCampus2026!"

ROLES = {code: name for code, name in Role.CHOICES}
DEPARTMENTS = [
    ("ENG", "Engineering", "Main Campus", "E2"),
    ("CS", "Computer Science", "Main Campus", "B3"),
    ("SCI", "Science Laboratories", "Main Campus", "L1"),
    ("LIB", "Library & Learning", "Main Campus", "LC"),
    ("ADM", "Administration", "Main Campus", "A1"),
    ("FAC", "Facilities & Estates", "SAN Campus", "F1"),
]
USERS = [
    # campus_id, name, dept, [(role, scope or None=campus)]
    ("100061001", "Layla Haddad", "CS", [("REQUESTER", "CS")]),
    ("100061002", "Omar Farouk", "CS", [("DEPT_REP", "CS"), ("REQUESTER", "CS")]),
    ("100061003", "Reem Al Suwaidi", "ENG", [("REQUESTER", "ENG")]),
    ("100062001", "Sara Al Mansoori", "ENG", [("CUSTODIAN", "ENG")]),
    ("100062002", "Yousef Nasser", "CS", [("CUSTODIAN", "CS")]),
    ("100062003", "Mariam Saeed", "SCI", [("CUSTODIAN", "SCI")]),
    ("100062004", "Hessa Al Marri", "LIB", [("CUSTODIAN", "LIB"), ("REQUESTER", "LIB")]),
    ("100063001", "Dr. Hamad Al Ketbi", "ENG", [("APPROVER", None)]),
    ("100063002", "Dr. Aisha Rahman", "CS", [("APPROVER", "CS")]),
    ("100064001", "Khalid Ibrahim", "FAC", [("MAINTENANCE", None)]),
    ("100065001", "Noura Al Hammadi", "ADM", [("PROCUREMENT", None)]),
    ("100065002", "Rashid Al Zaabi", "ADM", [("FINANCE", None)]),
    ("100065003", "Fatima Khoury", "ADM", [("SUSTAINABILITY", None)]),
    ("100069001", "Ahmed Saleh (IT)", "ADM", [("SYSADMIN", None)]),
    ("100069002", "Huda Al Nuaimi", "ADM", [("AUDITOR", None)]),
]
CATEGORIES = {
    ("IT Equipment", "IT"): [("Monitor", "monitor, screen, display, lcd"), ("Laptop", "laptop, notebook, ultrabook"),
                             ("Desktop computer", "desktop, pc, computer, workstation, tower"), ("Printer & scanner", "printer, scanner, copier"),
                             ("Networking", "router, switch, access point, ethernet")],
    ("Audio-Visual", "GENERAL"): [("Projector", "projector, beamer"), ("Conference & audio", "speaker, webcam, microphone, conference, headset")],
    ("Furniture", "FURNITURE"): [("Chair", "chair, seat, stool, seating"), ("Desk & table", "desk, table, workstation desk"),
                                 ("Storage", "cabinet, cupboard, locker, shelf, shelving, bookcase"), ("Whiteboard", "whiteboard, board, flipchart")],
    ("Laboratory Equipment", "GENERAL"): [("Microscope", "microscope, optical"), ("Test & measurement", "oscilloscope, multimeter, signal generator, power supply"),
                                          ("Lab apparatus", "centrifuge, hotplate, stirrer, balance, fume")],
    ("Books & Media", "GENERAL"): [("Textbooks", "book, textbook, manual")],
}


class Command(BaseCommand):
    help = "Load synthetic demo data (refuses if data already exists)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default=os.environ.get("CAMPUS_DEMO_PASSWORD") or DEFAULT_PASSWORD)

    def handle(self, *args, **opts):
        if User.objects.exists() or Asset.objects.exists():
            self.stdout.write(self.style.WARNING("Demo data NOT loaded: the database already contains records. Nothing was changed."))
            return
        if len(opts["password"]) < 12:
            raise CommandError("The demo password must be at least 12 characters.")
        self.password = opts["password"]
        with transaction.atomic():
            self.reference_data()
        self.history()
        self.current_state()
        self.stdout.write(self.style.SUCCESS(f"Synthetic demo data loaded: {User.objects.count()} accounts, {Asset.objects.count()} asset batches."))
        self.stdout.write("Sign in with any campus ID below (password from CAMPUS_DEMO_PASSWORD or the documented default).")
        for cid, name, dept, roles in USERS:
            self.stdout.write(f"  {cid:7} {name:24} {', '.join(ROLES[r] for r, _ in roles)}")

    # ------------------------------------------------------------- reference ---
    def reference_data(self):
        for code, name in Role.CHOICES:
            Role.objects.create(code=code, name=name)
        self.dept = {}
        for code, name, campus, building in DEPARTMENTS:
            self.dept[code] = Department.objects.create(code=code, name=name, campus=campus, building=building)
        self.u = {}
        for cid, name, dept, roles in USERS:
            user = User.objects.create_user(cid, name, self.dept[dept], self.password, email=f"{cid.lower()}@example.edu")
            for role, scope in roles:
                UserRole.objects.create(user=user, role=Role.objects.get(code=role), scope_department=self.dept[scope] if scope else None)
            self.u[cid] = user
        for code, approver in (("CS", "100063002"), ("ENG", "100063001"), ("SCI", "100063001"), ("LIB", "100063001")):
            d = self.dept[code]
            d.default_approver = self.u[approver]
            d.save()
        self.cat = {}
        for (root, kind), children in CATEGORIES.items():
            parent = Category.objects.create(name=root, kind=kind)
            for name, kw in children:
                self.cat[name] = Category.objects.create(name=name, parent=parent, keywords=kw)

    # ---------------------------------------------------------------- helpers ---
    def asset(self, tag, name, cat, qty, dept, custodian, building, room, condition, desc, specs="", tags="", **extra):
        data = dict(asset_tag=tag, name=name, description=desc, specifications=specs, tags=tags, category=self.cat[cat], quantity=qty,
                    owner_department=self.dept[dept], custodian=self.u[custodian], campus=extra.pop("campus", "Main Campus"),
                    building=building, room=room, condition=condition, **extra)
        return catalog.register_asset(self.u[custodian], data)

    def publish(self, a, custodian):
        a.refresh_from_db()
        return catalog.publish(self.u[custodian], a.pk, a.version)

    def request(self, requester, title, cat, qty, purpose, specs="", mandatory="", condition="GOOD", urgency="NORMAL", building="B3", room="101", days=14, receiver="100062002", partial=False):
        return exchange.create_draft(self.u[requester], dict(
            title=title, category=self.cat[cat], purpose=purpose, specifications=specs, mandatory_specs=mandatory, quantity=qty,
            preferred_condition=condition, urgency=urgency, destination_campus="Main Campus", destination_building=building, destination_room=room,
            required_date=timezone.localdate() + timedelta(days=days), allow_partial=partial, receiving_custodian=self.u[receiver]))

    def run_stages(self, version, release_by, finance_by="100065002"):
        for appr in version.approvals.filter(decision__isnull=True).order_by("stage_order"):
            if appr.stage == Approval.RELEASE_CHECK:
                who = self.u[release_by]
            elif appr.stage == Approval.COST_REVIEW:
                who = self.u[finance_by]
            else:
                who = appr.reviewer
            workflow.decide(who, appr.pk, version.pk, Approval.APPROVED, "Checked: eligible, safe and within budget.")

    def deliver(self, req, release_by, receive_by, cost=None, received=None, condition=None):
        t = req.reservations.order_by("-created_at").first().transfers.order_by("-created_at").first()
        workflow.schedule(self.u[release_by], t.pk, t.version, "DELIVERY", timezone.localdate() + timedelta(days=1), "Facilities trolley, loading bay")
        t.refresh_from_db()
        workflow.release(self.u[release_by], t.pk, t.version, t.quantity, "Handed to facilities porter; release slip RS-" + t.reference[-4:], cost)
        t.refresh_from_db()
        workflow.confirm_receipt(self.u[receive_by], t.pk, t.version, received if received is not None else t.quantity, condition or t.source_asset.condition, True, "Counted on arrival")
        t.refresh_from_db()
        return t

    def full_exchange(self, requester, asset, qty, release_by, receive_by, title, cat, purpose, cost=None, **kw):
        req = self.request(requester, title, cat, qty, purpose, receiver=receive_by, **kw)
        req, version, hold = exchange.submit(self.u[requester], req.pk, req.version, asset.pk, qty)
        self.run_stages(version, release_by)
        req.refresh_from_db()
        return req, self.deliver(req, release_by, receive_by, cost)

    def backdate(self, start, days):
        """Shift every row created during a scenario ``days`` into the past (synthetic history only)."""
        from apps.exchange.models import Reservation, ResourceRequest
        from apps.intelligence.models import AITask
        from apps.maintenance.models import Disposition, LifeCycleEvent, MaintenanceRecord
        from apps.reporting.models import AuditEvent, AvoidedPurchaseReview, Notification, SustainabilityImpact
        from apps.workflow.models import ProposalVersion, Transfer

        delta = timedelta(days=days)
        fields = {
            LifeCycleEvent: ["occurred_at"], SustainabilityImpact: ["occurred_at", "cohort_entered_at"], AuditEvent: ["created_at"], Notification: ["created_at"],
            Transfer: ["created_at", "released_at", "received_at"], Reservation: ["created_at", "decided_at", "released_at"], ResourceRequest: ["created_at", "submitted_at"],
            ProposalVersion: ["created_at", "decided_at"], Approval: ["decided_at"], MaintenanceRecord: ["reported_at", "work_started_at", "work_completed_at", "verified_at"],
            Disposition: ["created_at", "completed_at"], AvoidedPurchaseReview: ["reviewed_at"], Asset: ["created_at", "cohort_entered_at"], AITask: ["queued_at"],
        }
        for model, names in fields.items():
            for name in names:
                model.objects.filter(**{f"{name}__gte": start}).update(**{name: F(name) - delta})
        Notification.objects.filter(created_at__lt=timezone.now() - timedelta(days=5)).update(read_at=F("created_at"))

    # ---------------------------------------------------------------- history ---
    def history(self):
        from apps.reporting import services as reporting

        # 1 — Monitors from Engineering reused by Computer Science (~11 weeks ago)
        t0 = timezone.now()
        mon = self.asset("A-101", "Dell P2422H 24-inch monitor", "Monitor", 8, "ENG", "100062001", "E2", "214", "GOOD",
                         "24-inch IPS monitors from the retired CAD lab. Small scratches on two bezels, all tested working.",
                         "24-inch; 1920x1080; HDMI; DisplayPort; height-adjustable stand", "hdmi, 24-inch, ips",
                         purchase_date=date(2021, 9, 1), unit_value_aed=Decimal("450"), replacement_value_aed=Decimal("1050"),
                         estimated_transfer_cost_aed=Decimal("40"), unit_weight_kg=Decimal("5.4"), expected_retirement_date=date(2027, 9, 1),
                         type_metadata={"ports": "HDMI, DisplayPort"})
        self.publish(mon, "100062001")
        req, t = self.full_exchange("100061001", mon, 4, "100062001", "100062002", "Monitors for the robotics teaching lab", "Monitor",
                                    "Dual-screen stations for the robotics teaching lab", specs="24-inch, HDMI", mandatory="HDMI", cost=Decimal("40"))
        reporting.review_avoided_purchase(self.u["100065001"], t, True, "24-inch monitor (framework agreement item IT-MON-24)", 4, Decimal("1050"), "Framework price list FY26 item IT-MON-24")
        self.backdate(t0, 78)

        # 2 — Lab stools from Science reused by Engineering (~9 weeks ago)
        t0 = timezone.now()
        stools = self.asset("F-210", "Adjustable lab stools", "Chair", 12, "SCI", "100062003", "L1", "012", "EXCELLENT",
                            "Height-adjustable lab stools with foot ring, polypropylene seat. Surplus after lab refit.", "height-adjustable; chemical-resistant seat",
                            "stool, adjustable", purchase_date=date(2023, 1, 15), unit_value_aed=Decimal("210"), replacement_value_aed=Decimal("320"),
                            estimated_transfer_cost_aed=Decimal("0"), unit_weight_kg=Decimal("6.0"), type_metadata={"material": "Steel, polypropylene"})
        self.publish(stools, "100062003")
        req, t = self.full_exchange("100061003", stools, 6, "100062003", "100062001", "Stools for the materials testing bench", "Chair",
                                    "Seating for the materials testing practical sessions", building="E2", room="118", cost=Decimal("0"))
        reporting.review_avoided_purchase(self.u["100065002"], t, True, "Lab stool (catalogue LS-200)", 6, Decimal("320"), "Supplier quote Q-2026-114")
        self.backdate(t0, 63)

        # 3 — Projector repaired instead of replaced (~6 weeks ago)
        t0 = timezone.now()
        proj = self.asset("AV-301", "Epson EB-W51 projector", "Projector", 1, "LIB", "100062004", "LC", "Seminar 2", "FAIR",
                          "Classroom projector; lamp failed, otherwise working.", "WXGA; 4000 lumens; HDMI; VGA", "projector, hdmi",
                          purchase_date=date(2020, 2, 10), unit_value_aed=Decimal("900"), replacement_value_aed=Decimal("1800"),
                          expected_retirement_date=date(2026, 12, 31), unit_weight_kg=Decimal("2.5"))
        rec = maint.report_defect(self.u["100062004"], proj.pk, "Lamp failed during a seminar; projector powers on but shows no image.", "Photo of lamp warning LED")
        rec = maint.start_inspection(self.u["100064001"], rec.pk, rec.version)
        rec = maint.record_inspection(self.u["100064001"], rec.pk, rec.version, "Lamp at end of life; optics and board fine.", "NEEDS_REPAIR", "REPAIR", "Replace lamp module ELPLP96", Decimal("120"))
        version = rec.proposal_versions.first()
        self.run_stages(version, "100062004")
        rec.refresh_from_db()
        rec = maint.record_work(self.u["100064001"], rec.pk, rec.version, "Lamp module replaced; 2-hour burn-in test passed.", Decimal("120"), "GOOD")
        maint.verify(self.u["100062004"], rec.pk, rec.version, True, "Image test and focus check passed in Seminar 2.", "GOOD", date(2028, 6, 30))
        self.backdate(t0, 44)

        # 4 — Unrepairable printers donated / recycled (~4 weeks ago)
        t0 = timezone.now()
        prn = self.asset("IT-415", "HP LaserJet M402 printers", "Printer & scanner", 3, "CS", "100062002", "B3", "Store 1", "FAIR",
                         "Mono laser printers replaced by the managed print service. Working but no internal demand.", "USB; Ethernet; duplex",
                         "printer, laser", purchase_date=date(2018, 5, 1), unit_weight_kg=Decimal("8.6"))
        catalog.publish(self.u["100062002"], prn.pk, prn.version)
        prn.refresh_from_db()
        catalog.unpublish(self.u["100062002"], prn.pk, prn.version, "No internal takers after 3 weeks")
        prn.refresh_from_db()
        d = maint.propose_disposition(self.u["100062002"], prn.pk, prn.version, "DONATE", 3, "No internal demand after publication; printers are serviceable.",
                                      "Internal reuse (published 3 weeks, no requests); repair not needed; recycling would waste working units.", "Asset list and test prints",
                                      None, "Al Noor Community School")
        self.run_stages(d.proposal_versions.first(), "100062002")
        maint.complete_disposition(self.u["100062002"], d.pk, timezone.localdate(), 3, "Al Noor Community School", "Donation receipt DN-2026-041 signed by school principal")
        self.backdate(t0, 30)

        # 5 — Partial batch with a receipt exception resolved (~2 weeks ago)
        t0 = timezone.now()
        desks = self.asset("F-118", "Sit-stand desks 140 cm", "Desk & table", 6, "ENG", "100062001", "E2", "305", "GOOD",
                           "Electric sit-stand desks from the closed design studio.", "140x70 cm; electric height adjust", "desk, adjustable",
                           purchase_date=date(2022, 8, 20), unit_value_aed=Decimal("1100"), replacement_value_aed=Decimal("1650"),
                           estimated_transfer_cost_aed=Decimal("60"), unit_weight_kg=Decimal("38"), type_metadata={"material": "Laminate, steel", "dimensions": "140 x 70 cm"})
        self.publish(desks, "100062001")
        req = self.request("100061002", "Sit-stand desks for the AI lab", "Desk & table", 3, "Accessible workstations in the AI research lab", partial=True, room="204")
        req, version, hold = exchange.submit(self.u["100061002"], req.pk, req.version, desks.pk, 3)
        self.run_stages(version, "100062001")
        req.refresh_from_db()
        t = req.reservations.first().transfers.first()
        workflow.schedule(self.u["100062001"], t.pk, t.version, "DELIVERY", timezone.localdate() + timedelta(days=1), "Two-person lift; goods lift 2")
        t.refresh_from_db()
        workflow.release(self.u["100062001"], t.pk, t.version, 3, "Loaded on facilities van, slip RS-0412", Decimal("60"))
        t.refresh_from_db()
        t = workflow.confirm_receipt(self.u["100062002"], t.pk, t.version, 2, "GOOD", True, "Only 2 desks arrived; third left at loading bay")
        workflow.resolve_exception(self.u["100063002"], t.pk, t.version, "ACCEPT_RECEIVED", "Accept the 2 desks received; third desk to follow on a new delivery.")
        self.backdate(t0, 15)

    # ------------------------------------------------------- live demo state ---
    def current_state(self):
        a = self.asset
        laptops = a("IT-220", "Lenovo ThinkPad T14 laptops", "Laptop", 5, "ENG", "100062001", "E2", "110", "GOOD",
                    "Core i5 laptops returned after the staff refresh; batteries above 80%.", "Core i5; 16GB RAM; 512GB SSD; USB-C; HDMI", "laptop, i5, 16gb, ssd",
                    purchase_date=date(2022, 3, 1), unit_value_aed=Decimal("1800"), replacement_value_aed=Decimal("4200"), estimated_transfer_cost_aed=Decimal("0"),
                    unit_weight_kg=Decimal("1.5"), expected_retirement_date=date(2027, 3, 1), type_metadata={"serial_number": "PF3-batch", "ports": "USB-C, HDMI"})
        screens = a("A-130", "27-inch 4K display screens", "Monitor", 3, "SCI", "100062003", "L1", "Office 4", "EXCELLENT",
                    "Large 4K displays from the imaging suite, like new.", "27-inch; 3840x2160; HDMI; USB-C", "display, 4k, 27-inch",
                    purchase_date=date(2024, 5, 5), replacement_value_aed=Decimal("1900"), estimated_transfer_cost_aed=Decimal("40"), unit_weight_kg=Decimal("6.2"))
        a("A-145", "LG 22-inch monitors (VGA)", "Monitor", 6, "LIB", "100062004", "LC", "Store", "FAIR", "Older 22-inch monitors, VGA input only.",
          "22-inch; 1680x1050; VGA", "vga, 22-inch", unit_weight_kg=Decimal("3.9"), estimated_transfer_cost_aed=Decimal("40"), replacement_value_aed=Decimal("600"))
        chairs = a("F-220", "Ergonomic mesh office chairs", "Chair", 10, "LIB", "100062004", "LC", "Level 2", "GOOD",
                   "Mesh-back task chairs with lumbar support from the old reading room.", "adjustable height; lumbar support; armrests", "ergonomic, mesh",
                   purchase_date=date(2021, 1, 10), replacement_value_aed=Decimal("750"), estimated_transfer_cost_aed=Decimal("25"), unit_weight_kg=Decimal("14"),
                   type_metadata={"material": "Mesh, aluminium"})
        a("L-501", "Olympus CX23 student microscopes", "Microscope", 4, "SCI", "100062003", "L1", "Lab 3", "GOOD", "Binocular student microscopes, serviced last year.",
          "40x-1000x; LED illumination", "microscope, optical", purchase_date=date(2019, 9, 1), replacement_value_aed=Decimal("2600"), estimated_transfer_cost_aed=Decimal("30"))
        a("L-520", "Rigol DS1054Z oscilloscopes", "Test & measurement", 2, "ENG", "100062001", "E2", "Lab 12", "GOOD", "4-channel digital oscilloscopes, probes included.",
          "50 MHz; 4 channels; USB", "oscilloscope", replacement_value_aed=Decimal("1500"))
        a("F-330", "Steel storage cabinets", "Storage", 4, "CS", "100062002", "B3", "Store 2", "FAIR", "Lockable two-door steel cupboards, some dents.",
          "180x90x45 cm; lockable", "cabinet, lockable", unit_weight_kg=Decimal("45"))
        board = a("F-340", "Mobile whiteboards", "Whiteboard", 3, "ENG", "100062001", "E2", "Studio", "GOOD", "Double-sided whiteboards on castors.", "120x90 cm; mobile", "whiteboard, mobile")
        av = a("AV-310", "Logitech conference cameras", "Conference & audio", 2, "CS", "100062002", "B3", "Meeting 1", "EXCELLENT", "4K conference cameras with speakerphone.",
               "4K; USB; speakerphone", "webcam, conference")
        a("IT-230", "Dell OptiPlex desktop PCs", "Desktop computer", 6, "CS", "100062002", "B3", "Lab 5", "GOOD", "Small-form-factor desktops from the teaching lab refresh.",
          "Core i5; 8GB RAM; 256GB SSD; HDMI", "desktop, i5", purchase_date=date(2021, 6, 1), replacement_value_aed=Decimal("2800"), estimated_transfer_cost_aed=Decimal("30"),
          expected_retirement_date=date(2026, 6, 1))
        draft = a("B-610", "Engineering mathematics textbooks", "Textbooks", 25, "LIB", "100062004", "LC", "Stacks", "GOOD", "Kreyszig 10th edition, light pencil marks.", "Hardcover; 10th edition; English", "book")
        for asset, who in ((laptops, "100062001"), (screens, "100062003"), (chairs, "100062004"), (board, "100062001"), (av, "100062002")):
            self.publish(asset, who)
        for tag, who in (("A-145", "100062004"), ("L-501", "100062003"), ("L-520", "100062001"), ("F-330", "100062002"), ("IT-230", "100062002")):
            self.publish(Asset.objects.get(asset_tag=tag), who)

        # Pending approval: CS requester wants 2 laptops (release check waiting for ENG custodian)
        req = self.request("100061001", "Laptops for visiting researchers", "Laptop", 2, "Short-term laptops for two visiting researchers", specs="16GB RAM, SSD", mandatory="SSD", room="210", days=10)
        exchange.submit(self.u["100061001"], req.pk, req.version, laptops.pk, 2)
        # Approved, awaiting release: ENG requester takes 4 chairs from the library
        req = self.request("100061003", "Ergonomic chairs for the design studio", "Chair", 4, "Replace broken studio chairs", building="E2", room="305", receiver="100062001", condition="GOOD")
        req, version, hold = exchange.submit(self.u["100061003"], req.pk, req.version, chairs.pk, 4)
        self.run_stages(version, "100062004")
        # Released, awaiting receipt: CS takes 1 conference camera? (own dept) -> instead ENG takes 2 4K screens from SCI
        req = self.request("100061003", "4K screens for the visualisation wall", "Monitor", 2, "Visualisation wall in the E2 lobby", building="E2", room="Lobby", receiver="100062001", condition="GOOD", mandatory="HDMI")
        req, version, hold = exchange.submit(self.u["100061003"], req.pk, req.version, screens.pk, 2)
        self.run_stages(version, "100062003")
        t = hold.transfers.first()
        workflow.release(self.u["100062003"], t.pk, t.version, 2, "Collected by E2 technician with trolley, slip RS-0520", Decimal("40"))
        # A draft need with no listing yet (for AI matching demo)
        self.request("100061002", "Display screens for the student project room", "Monitor", 3, "Second screens so student teams can present and code together",
                     specs="HDMI, at least 24 inch", mandatory="HDMI", room="120", days=21)
        # Open maintenance item
        maint.report_defect(self.u["100062002"], Asset.objects.get(asset_tag="IT-230").pk, "One desktop does not power on after the lab move; check PSU.", "Lab technician note")
        self.stdout.write(f"Draft asset left unpublished for the registration demo: {draft.asset_tag}")
