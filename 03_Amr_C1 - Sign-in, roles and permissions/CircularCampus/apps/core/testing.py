"""Test fixture builder shared by all app test suites (no demo seed dependency)."""
from apps.accounts.models import Department, Role, User, UserRole
from apps.catalog.models import Category

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
