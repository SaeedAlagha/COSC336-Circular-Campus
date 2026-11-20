from django.core.exceptions import PermissionDenied
from .models import Profile

def profile(user):
    try:
        p=user.profile
        if not user.is_active or not p.department.active: raise PermissionDenied('Account or department is inactive.')
        return p
    except Profile.DoesNotExist: raise PermissionDenied('An active campus role is required.')

def require(user,*roles,department=None):
    p=profile(user)
    if not user.is_active or not p.department.active or p.role not in roles:
        raise PermissionDenied('Your assigned role does not permit this action.')
    if department is not None and p.department_id!=department:
        raise PermissionDenied('This record belongs to another department.')
    return p

def owns_need(user,need):
    p=profile(user)
    return need.requester_id==user.pk or (p.role=='Department Representative' and need.department_id==p.department_id)

REPORT_ROLES=('Procurement Officer','Finance Officer','Sustainability Officer','Management / Auditor')
