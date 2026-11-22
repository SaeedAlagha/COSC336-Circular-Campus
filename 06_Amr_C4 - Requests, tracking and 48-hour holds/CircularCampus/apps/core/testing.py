"""Test fixture builder shared by all app test suites (no demo seed dependency)."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Department, Role, User, UserRole
from apps.catalog import services as catalog
from apps.catalog.models import Category
from apps.exchange import services as exchange
from apps.workflow import services as workflow
from apps.workflow.models import Approval

PW = "Correct horse battery 12"


class World:
    """Two departments (CS requests from ENG), one user per role, a category tree."""

    def __init__(self):
        for code, name in Role.CHOICES:
            Role.objects.get_or_create(code=code, defaults={"name": name})
        self.eng = Department.objects.create(code="ENG", name="Engineering", building="E2")
        self.cs = Department.objects.create(code="CS", name="Computer Science", building="B3")
        self.requester = self.user("100060101", "Req User", self.cs, [("REQUESTER", self.cs)])
        self.rep = self.user("100060102", "Dept Rep", self.cs, [("DEPT_REP", self.cs)])
        self.eng_cust = self.user("100060201", "Eng Custodian", self.eng, [("CUSTODIAN", self.eng)])
        self.cs_cust = self.user("100060202", "Cs Custodian", self.cs, [("CUSTODIAN", self.cs)])
        self.approver = self.user("100060301", "Cs Approver", self.cs, [("APPROVER", self.cs)])
        self.eng_approver = self.user("100060302", "Eng Approver", self.eng, [("APPROVER", self.eng)])
        self.tech = self.user("100060401", "Tech Staff", self.eng, [("MAINTENANCE", None)])
        self.finance = self.user("100060501", "Fin Officer", self.eng, [("FINANCE", None)])
        self.procurement = self.user("100060502", "Proc Officer", self.eng, [("PROCUREMENT", None)])
        self.sustain = self.user("100060503", "Sus Officer", self.eng, [("SUSTAINABILITY", None)])
        self.admin = self.user("100060901", "Sys Admin", self.eng, [("SYSADMIN", None)])
        self.auditor = self.user("100060902", "Audit User", self.eng, [("AUDITOR", None)])
        self.cs.default_approver = self.approver
        self.cs.save()
        self.eng.default_approver = self.eng_approver
        self.eng.save()
        it = Category.objects.create(name="IT Equipment", kind="IT")
        furn = Category.objects.create(name="Furniture", kind="FURNITURE")
        self.monitor_cat = Category.objects.create(name="Monitor", parent=it, keywords="monitor, screen, display")
        self.laptop_cat = Category.objects.create(name="Laptop", parent=it, keywords="laptop, notebook")
        self.chair_cat = Category.objects.create(name="Chair", parent=furn, keywords="chair, seat, stool")

    def user(self, cid, name, dept, roles):
        u = User.objects.create_user(cid, name, dept, PW)
        for code, scope in roles:
            UserRole.objects.create(user=u, role=Role.objects.get(code=code), scope_department=scope)
        return u

    def asset(self, tag="A-100", qty=4, publish=True, cost=None, **extra):
        data = dict(asset_tag=tag, name=extra.pop("name", "Dell 24-inch HDMI monitor"), description=extra.pop("description", "24-inch monitor with HDMI, tested"),
                    specifications=extra.pop("specifications", "24-inch; HDMI"), category=extra.pop("category", self.monitor_cat), quantity=qty,
                    owner_department=self.eng, custodian=self.eng_cust, campus="Main Campus", building="E2", room="101",
                    condition=extra.pop("condition", "GOOD"), estimated_transfer_cost_aed=cost, replacement_value_aed=extra.pop("replacement", Decimal("1000")), **extra)
        a = catalog.register_asset(self.eng_cust, data)
        if publish:
            a = catalog.publish(self.eng_cust, a.pk, a.version)
        a.refresh_from_db()
        return a

    def request(self, qty=2, partial=False, user=None, **extra):
        user = user or self.requester
        data = dict(title=extra.pop("title", "Monitors for lab"), category=self.monitor_cat, purpose=extra.pop("purpose", "Teaching lab screens"),
                    specifications="", mandatory_specs=extra.pop("mandatory_specs", ""), quantity=qty, preferred_condition=extra.pop("preferred_condition", "GOOD"),
                    urgency="NORMAL", destination_campus="Main Campus", destination_building="B3", destination_room="204",
                    required_date=timezone.localdate() + timedelta(days=14), allow_partial=partial, receiving_custodian=self.cs_cust)
        data.update(extra)
        return exchange.create_draft(user, data)

    def submit(self, req, asset=None, qty=None, user=None):
        req.refresh_from_db()
        return exchange.submit(user or self.requester, req.pk, req.version, asset.pk if asset else None, qty)

    def approve_all(self, version, release_by=None):
        for a in version.approvals.filter(decision__isnull=True).order_by("stage_order"):
            who = {Approval.RELEASE_CHECK: release_by or self.eng_cust, Approval.COST_REVIEW: self.finance}.get(a.stage, a.reviewer)
            workflow.decide(who, a.pk, version.pk, Approval.APPROVED, "ok")

    def approved_transfer(self, qty=2, asset=None, partial=False):
        asset = asset or self.asset()
        req = self.request(qty=qty, partial=partial)
        req, version, hold = self.submit(req, asset, qty)
        self.approve_all(version)
        return req, hold.transfers.get(), asset

    def release(self, t):
        t.refresh_from_db()
        return workflow.release(self.eng_cust, t.pk, t.version, t.quantity, "Handed over with slip RS-1")

    def receive(self, t, qty=None, condition=None, accepted=True):
        t.refresh_from_db()
        return workflow.confirm_receipt(self.cs_cust, t.pk, t.version, qty if qty is not None else t.quantity, condition or t.source_asset.condition, accepted, "")
