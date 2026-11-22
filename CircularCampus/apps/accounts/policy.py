"""Identity and policy component (design 4.6, 4.6.1 permission matrix).

Deny-by-default. Every protected view and every service command calls into
this module on the server; hiding a button is never the access control
(C1, NFR-08, BR-01). Technical administration (SYSADMIN) never implies
resource-approval authority.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from .models import Role


# [UML: Inheritance — AccessDenied is-a PermissionDenied]
class AccessDenied(PermissionDenied):
    """Raised by policy checks; the exception handler logs a safe audit event (FR-04)."""

    def __init__(self, message="You do not have permission to do that.", action="", record=None):
        super().__init__(message)
        self.safe_message = message
        self.action = action
        self.record = record


def _dept_id(value):
    if value is None:
        return None
    return getattr(value, "pk", value)


def grants(user):
    """Return {role_code: set(scope_department_id or None)} for an active user (cached per request)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active or not user.department.active:
        return {}
    cache = getattr(user, "_role_cache", None)
    if cache is None:
        cache = {}
        for g in user.role_grants.filter(active=True).select_related("role"):
            cache.setdefault(g.role.code, set()).add(g.scope_department_id)
        user._role_cache = cache
    return cache


def role_codes(user):
    return set(grants(user).keys())


def has_role(user, role, department=None):
    """True if the user holds ``role`` with a scope covering ``department``.

    ``department=None`` asks "holds this role anywhere".
    """
    scopes = grants(user).get(role)
    if not scopes:
        return False
    if department is None:
        return True
    return None in scopes or _dept_id(department) in scopes


def scoped_departments(user, role):
    """Department ids the role covers, or None meaning campus-wide."""
    scopes = grants(user).get(role, set())
    if None in scopes:
        return None
    return scopes


def require(condition, message="You do not have permission to do that.", action="", record=None):
    if not condition:
        raise AccessDenied(message, action=action, record=record)


# ---------------------------------------------------------------- catalog ---
VISIBLE_CATALOG_STATUSES = {"AVAILABLE", "RESERVED", "TRANSFER_PENDING"}
OVERSIGHT_ROLES = (Role.APPROVER, Role.MAINTENANCE, Role.PROCUREMENT, Role.FINANCE, Role.SUSTAINABILITY, Role.AUDITOR, Role.DEPT_REP)


def can_view_asset(user, asset):
    if not grants(user):
        return False
    if asset.status in VISIBLE_CATALOG_STATUSES and asset.is_published:
        return True  # published listings are visible to authorised campus users
    if asset.custodian_id == user.pk:
        return True
    return any(has_role(user, r, asset.owner_department_id) for r in (Role.CUSTODIAN,) + OVERSIGHT_ROLES)


def can_view_financials(user, asset):
    """NFR-09: values and costs only for roles with a financial need."""
    if asset.custodian_id == user.pk:
        return True
    return any(has_role(user, r, asset.owner_department_id) for r in (Role.CUSTODIAN, Role.APPROVER, Role.FINANCE, Role.PROCUREMENT, Role.SUSTAINABILITY, Role.AUDITOR))


def can_manage_asset(user, asset=None, department=None):
    dept = asset.owner_department_id if asset is not None else department
    return has_role(user, Role.CUSTODIAN, dept)


def can_report_defect(user, asset):
    return can_view_asset(user, asset) and any(
        has_role(user, r, None) for r in (Role.REQUESTER, Role.DEPT_REP, Role.CUSTODIAN, Role.APPROVER, Role.MAINTENANCE)
    )


# --------------------------------------------------------------- requests ---
def can_create_request(user, department=None):
    dept = department or user.department_id
    return has_role(user, Role.REQUESTER, dept) or has_role(user, Role.DEPT_REP, dept)


def can_edit_request(user, req):
    return req.requester_id == user.pk or has_role(user, Role.DEPT_REP, req.department_id)


def can_view_request(user, req):
    if req.requester_id == user.pk or req.assigned_approver_id == user.pk or req.receiving_custodian_id == user.pk:
        return True
    if has_role(user, Role.DEPT_REP, req.department_id) or has_role(user, Role.AUDITOR, req.department_id):
        return True
    if has_role(user, Role.APPROVER, req.department_id) or has_role(user, Role.FINANCE, req.department_id):
        return True
    # A custodian releasing stock for this request may see it.
    return req.reservations.filter(asset__custodian=user).exists() or any(
        has_role(user, Role.CUSTODIAN, r.asset.owner_department_id) for r in req.reservations.select_related("asset")
    )


# --------------------------------------------------------------- workflow ---
def can_decide_stage(user, approval, proposer_id):
    """A stage may be decided only by an eligible reviewer who is not the proposer."""
    from apps.workflow.models import Approval

    if user.pk == proposer_id:
        return False
    if approval.stage == Approval.AUTHORIZE:
        return approval.reviewer_id == user.pk and has_role(user, Role.APPROVER, approval.scope_department_id)
    if approval.stage == Approval.COST_REVIEW:
        return has_role(user, Role.FINANCE, approval.scope_department_id)
    if approval.stage == Approval.RELEASE_CHECK:
        return has_role(user, Role.CUSTODIAN, approval.scope_department_id)
    return False


def can_view_transfer(user, transfer):
    if user.pk in (transfer.releasing_user_id, transfer.receiving_user_id):
        return True
    return can_view_request(user, transfer.reservation.request)


def can_resolve_exception(user, transfer):
    req = transfer.reservation.request
    if user.pk == req.requester_id:
        return False
    return has_role(user, Role.APPROVER, req.department_id) or has_role(user, Role.APPROVER, transfer.source_asset.owner_department_id)


# ------------------------------------------------------------ maintenance ---
def can_service(user, asset):
    return has_role(user, Role.MAINTENANCE, asset.owner_department_id)


def can_verify_service(user, asset):
    return has_role(user, Role.CUSTODIAN, asset.owner_department_id)


def can_view_maintenance(user, record):
    return record.reporter_id == user.pk or can_service(user, record.asset) or can_view_asset(user, record.asset)


# -------------------------------------------------------------- reporting ---
def can_view_reports(user, department=None):
    return any(has_role(user, r, department) for r in (Role.PROCUREMENT, Role.FINANCE, Role.SUSTAINABILITY, Role.AUDITOR, Role.APPROVER, Role.DEPT_REP, Role.CUSTODIAN, Role.MAINTENANCE))


def report_scope(user):
    """Department ids the user may report on, or None for campus-wide."""
    result = set()
    for r in (Role.PROCUREMENT, Role.FINANCE, Role.SUSTAINABILITY, Role.AUDITOR, Role.APPROVER, Role.DEPT_REP, Role.CUSTODIAN, Role.MAINTENANCE):
        scopes = scoped_departments(user, r)
        if scopes is None and r in grants(user):
            return None
        result |= scopes or set()
    return result


def can_author_report(user):
    """Reporting officers draft, edit, review and publish narratives (UC-06)."""
    return any(has_role(user, r) for r in Role.REPORTING)


def can_review_avoided_purchase(user, department):
    return has_role(user, Role.PROCUREMENT, department) or has_role(user, Role.FINANCE, department)


def can_manage_factors(user):
    return has_role(user, Role.SUSTAINABILITY)


def can_search_audit(user):
    return has_role(user, Role.AUDITOR) or has_role(user, Role.SYSADMIN)


def can_admin_accounts(user):
    return has_role(user, Role.SYSADMIN)


def is_requester_like(user):
    return has_role(user, Role.REQUESTER) or has_role(user, Role.DEPT_REP)
