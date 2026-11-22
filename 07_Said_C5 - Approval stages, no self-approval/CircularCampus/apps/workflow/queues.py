"""Read-side queries for approval queues (no business writes)."""
from apps.accounts import policy

from .models import Approval, ProposalVersion


def my_pending_stages(user):
    """Stages the user may decide now: version pending/current, earlier stages complete, not own proposal."""
    result = []
    qs = (Approval.objects.filter(decision__isnull=True, version__status=ProposalVersion.PENDING)
          .select_related("version__proposer", "version__request", "version__maintenance__asset", "version__disposition__asset", "scope_department", "reviewer")
          .order_by("version__created_at", "stage_order"))
    for a in qs:
        if a.version.approvals.filter(stage_order__lt=a.stage_order, decision__isnull=True).exists():
            continue
        if policy.can_decide_stage(user, a, a.version.proposer_id):
            result.append(a)
    return result
