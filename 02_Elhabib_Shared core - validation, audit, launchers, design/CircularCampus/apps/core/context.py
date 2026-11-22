"""Shell context: role-aware navigation, role/department chips, unread notices."""
from django.conf import settings

from apps.accounts import policy
from apps.accounts.models import Role


def _installed(urlname):
    """A menu entry appears only when its page is installed (keeps every build step runnable)."""
    from django.urls import NoReverseMatch, reverse

    try:
        reverse(urlname)
        return True
    except NoReverseMatch:
        return False


def shell(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"demo_mode": settings.CAMPUS["DEMO_MODE"]}
    roles = policy.role_codes(user)
    labels = dict(Role.CHOICES)
    grants = []
    for g in user.role_grants.filter(active=True).select_related("role", "scope_department"):
        grants.append({"role": g.role.name, "scope": g.scope_department.code if g.scope_department_id else "Campus-wide"})
    nav = [("Workspace", [("dashboard", "Dashboard", "layout-dashboard", "core:dashboard")])]
    exchange = [("catalog", "Catalog", "package", "catalog:list")]
    if policy.is_requester_like(user):
        exchange.append(("requests", "My requests", "clipboard-list", "exchange:list"))
    if Role.CUSTODIAN in roles:
        exchange.append(("assets", "My assets", "box", "catalog:mine"))
    nav.append(("Exchange", exchange))
    flow = []
    if roles & {Role.APPROVER, Role.CUSTODIAN, Role.FINANCE}:
        flow.append(("approvals", "Approvals", "shield-check", "workflow:queue"))
    if roles & {Role.CUSTODIAN, Role.APPROVER, Role.REQUESTER, Role.DEPT_REP, Role.AUDITOR}:
        flow.append(("transfers", "Transfers", "truck", "workflow:transfers"))
    if roles & {Role.MAINTENANCE, Role.CUSTODIAN, Role.APPROVER, Role.REQUESTER, Role.DEPT_REP}:
        flow.append(("maintenance", "Maintenance", "wrench", "maintenance:list"))
    if flow:
        nav.append(("Workflow", flow))
    intel = [("assistant", "Assistant", "sparkles", "intelligence:assistant")]
    if policy.can_view_reports(user):
        intel += [("sustainability", "Sustainability", "leaf", "reporting:sustainability"), ("reports", "Reports", "chart-column", "reporting:operational")]
    if policy.can_author_report(user) or Role.AUDITOR in roles:
        intel.append(("narratives", "AI reports", "file-text", "reporting:reports"))
    nav.append(("Insight", intel))
    admin = []
    if policy.can_admin_accounts(user):
        admin += [("users", "Accounts", "users", "accounts:users"), ("departments", "Departments", "building-2", "accounts:departments")]
    if policy.can_search_audit(user):
        admin.append(("audit", "Audit trail", "history", "reporting:audit"))
    if admin:
        nav.append(("Administration", admin))
    nav = [(label, [i for i in items if _installed(i[3])]) for label, items in nav]
    nav = [(label, items) for label, items in nav if items]
    primary_role = next((labels[r] for r, _ in Role.CHOICES if r in roles), "No role assigned")
    return {
        "demo_mode": settings.CAMPUS["DEMO_MODE"],
        "nav_groups": nav,
        "role_grants": grants,
        "primary_role": primary_role,
        "unread_count": user.notifications.filter(read_at__isnull=True).count(),
        "ai_live": bool(settings.GEMINI.get("API_KEY")) and settings.GEMINI.get("ENABLED", True),
    }
