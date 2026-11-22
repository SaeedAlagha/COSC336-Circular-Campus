"""Accounts and access (process 1.0, UC-07): validated account, role-grant and
department changes, each audited. Deactivation never deletes history (FR-46)."""
from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.reporting import audit

from . import policy
from .models import Department, Role, User, UserRole


def _require_admin(actor):
    policy.require(policy.can_admin_accounts(actor), "Only system administrators can manage accounts and departments.", "admin")


def _check_password(password, confirm, user=None):
    if password != confirm:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The passwords do not match.", {"password2": "Type the same password twice."})
    try:
        validate_password(password, user)
    except ValidationError as exc:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a stronger password.", {"password1": " ".join(exc.messages)})


def create_user(actor, campus_id, full_name, department, email, password, confirm):
    _require_admin(actor)
    campus_id = V.campus_id(campus_id)
    if User.objects.filter(campus_id__iexact=campus_id).exists():
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That campus ID is already registered.", {"campus_id": "Use a unique campus ID."})
    full_name = V.person_name(full_name)
    email = V.text(email, "email", label="Email", max_len=254, needs_letter=False)
    if not department.active:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose an active department.", {"department": "This department is deactivated."})
    probe = User(campus_id=campus_id, full_name=full_name, email=email or "", department=department)
    _check_password(password, confirm, probe)
    with transaction.atomic():
        try:
            probe.full_clean(exclude=["password"])
        except ValidationError as exc:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Correct the highlighted fields.", {k: " ".join(v) for k, v in exc.message_dict.items()})
        probe.set_password(password)
        probe.save()
        audit.record(actor, "Account created", "account", probe, after={"campus_id": campus_id, "department": department.code})
        return probe


def update_user(actor, user, full_name, email, department):
    _require_admin(actor)
    before = {"full_name": user.full_name, "email": user.email, "department": user.department.code}
    with transaction.atomic():
        full_name, email = V.person_name(full_name), V.text(email, "email", label="Email", max_len=254, needs_letter=False)
        if not department.active and department.pk != user.department_id:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose an active department.", {"department": "This department is deactivated."})
        user.full_name, user.email, user.department = full_name, email, department
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as exc:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Correct the highlighted fields.", {k: " ".join(v) for k, v in exc.message_dict.items()})
        user.save()
        audit.record(actor, "Account updated", "account", user, before=before, after={"full_name": user.full_name, "email": user.email, "department": department.code})
        return user


def set_active(actor, user, active, reason=""):
    _require_admin(actor)
    if user.pk == actor.pk and not active:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "You cannot deactivate your own account.")
    reason = V.reason(reason, required=False)
    with transaction.atomic():
        user.is_active = active
        user.save(update_fields=["is_active"])
        audit.record(actor, "Account reactivated" if active else "Account deactivated", "account", user, after={"is_active": active}, reason=reason)
        return user


def reset_password(actor, user, password, confirm):
    _require_admin(actor)
    _check_password(password, confirm, user)
    with transaction.atomic():
        user.set_password(password)
        user.save(update_fields=["password"])
        audit.record(actor, "Password reset by administrator", "account", user)


def grant_role(actor, user, role_code, scope_department=None):
    """Explicit grants only; SYSADMIN status never implies approver authority (BR-01)."""
    _require_admin(actor)
    role = Role.objects.filter(code=role_code).first()
    if role is None:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Unknown role.", {"role": "Choose an approved role."})
    if role_code == Role.APPROVER and user.pk == actor.pk:
        raise BusinessError(BusinessError.SELF_APPROVAL, "Administrators cannot grant themselves approval authority.")
    with transaction.atomic():
        grant, created = UserRole.objects.get_or_create(user=user, role=role, scope_department=scope_department, defaults={"granted_by": actor})
        if not created and not grant.active:
            grant.active = True
            grant.granted_by = actor
            grant.save(update_fields=["active", "granted_by"])
        audit.record(actor, "Role granted", "role_grant", grant, after={"user": user.campus_id, "role": role_code, "scope": scope_department.code if scope_department else "CAMPUS"})
        return grant


def revoke_role(actor, grant):
    _require_admin(actor)
    with transaction.atomic():
        grant.active = False
        grant.save(update_fields=["active"])
        audit.record(actor, "Role revoked", "role_grant", grant, after={"user": grant.user.campus_id, "role": grant.role.code})


def save_department(actor, dept, code, name, campus, building, default_approver=None):
    _require_admin(actor)
    creating = dept is None
    dept = dept or Department()
    before = {"code": dept.code, "name": dept.name} if not creating else {}
    code = V.text(code, "code", label="Code", required=True, max_len=12).upper()
    if not V.CODE.match(code):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Department code format is wrong.", {"code": "Use 2–12 capital letters, e.g. ENG."})
    name = V.text(name, "name", label="Name", required=True, min_len=3, max_len=120)
    campus = V.place(campus, "campus", required=True)
    building = V.place(building, "building", required=False)
    dept.code, dept.name, dept.campus, dept.building = code, name, campus, building
    if default_approver is not None and not policy.has_role(default_approver, Role.APPROVER, dept.pk if not creating else None):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The default approver must hold the Assigned Approver role for this department.", {"default_approver": "Grant the approver role first."})
    dept.default_approver = default_approver
    with transaction.atomic():
        try:
            dept.full_clean()
        except ValidationError as exc:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Correct the highlighted fields.", {k: " ".join(v) for k, v in exc.message_dict.items()})
        dept.save()
        audit.record(actor, "Department created" if creating else "Department updated", "department", dept, before=before, after={"code": dept.code, "name": dept.name})
        return dept


def set_department_active(actor, dept, active):
    """FR-46: deactivate instead of delete; referenced assets, requests and history are kept."""
    _require_admin(actor)
    with transaction.atomic():
        dept.active = active
        dept.save(update_fields=["active"])
        audit.record(actor, "Department reactivated" if active else "Department deactivated", "department", dept, after={"active": active})
        return dept
