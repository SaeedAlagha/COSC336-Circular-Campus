from datetime import date

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect

from apps.accounts import policy
from apps.accounts.models import Role
from apps.catalog.models import Condition
from apps.core.errors import BusinessError
from apps.core.views import AppView

from . import services
from .models import Approval, ProposalVersion, Transfer, TransferStatus
from .queues import my_pending_stages


class QueueView(AppView):
    template_name = "workflow/queue.html"
    nav = "approvals"

    def get(self, request):
        user = request.user
        policy.require(policy.role_codes(user) & {Role.APPROVER, Role.CUSTODIAN, Role.FINANCE}, "Only reviewers have an approval queue.", "queue")
        pending = my_pending_stages(user)
        decided = Approval.objects.filter(Q(reviewer=user), decision__in=[Approval.APPROVED, Approval.REJECTED, Approval.RETURNED]).select_related("version__request", "version__maintenance", "version__disposition").order_by("-decided_at")[:15]
        waiting_on_others = (Approval.objects.filter(decision__isnull=True, version__status=ProposalVersion.PENDING, version__proposer=user)
                             .select_related("version__request", "version__maintenance", "version__disposition").order_by("stage_order"))
        return self.render(request, {"pending": pending, "decided": decided, "mine_waiting": waiting_on_others})


class ApprovalView(AppView):
    """UC-03: the reviewer sees the exact immutable version, prior stages and evidence."""

    template_name = "workflow/approval.html"
    nav = "approvals"

    def get(self, request, pk):
        a = get_object_or_404(Approval.objects.select_related("version__proposer", "version__request__category", "version__maintenance__asset", "version__disposition__asset", "scope_department", "reviewer"), pk=pk)
        v = a.version
        target = v.target
        user = request.user
        allowed = policy.can_decide_stage(user, a, v.proposer_id) or user.pk == v.proposer_id or policy.has_role(user, Role.AUDITOR) or (v.request and policy.can_view_request(user, v.request))
        policy.require(allowed, "You are not a reviewer for this proposal.", "view_approval", a)
        stages = v.approvals.select_related("reviewer").order_by("stage_order")
        earlier_open = stages.filter(stage_order__lt=a.stage_order, decision__isnull=True).exists()
        latest = target.proposal_versions.order_by("-version_no").first()
        can_decide = (a.decision is None and v.status == ProposalVersion.PENDING and latest.pk == v.pk and not earlier_open
                      and policy.can_decide_stage(user, a, v.proposer_id))
        holds, assets = [], []
        if v.request_id:
            from apps.exchange.models import Reservation

            holds = Reservation.objects.filter(pk__in=[h["reservation_id"] for h in v.snapshot.get("holds", [])]).select_related("asset")
        advice = None
        if v.disposition_id and v.disposition.ai_result_id:
            advice = v.disposition.ai_result
        return self.render(request, {"a": a, "v": v, "target": target, "stages": stages, "can_decide": can_decide, "holds": holds, "latest": latest,
                                     "is_proposer": user.pk == v.proposer_id, "earlier_open": earlier_open, "advice": advice,
                                     "versions": target.proposal_versions.order_by("-version_no")})

    def post(self, request, pk):
        a = get_object_or_404(Approval, pk=pk)
        decision = request.POST.get("decision")
        try:
            self.command(request, "decide", {"approval": str(pk), "decision": decision},
                         lambda: {"id": str(services.decide(request.user, pk, request.POST.get("version_id"), decision, request.POST.get("reason", "")).pk)})
            verb = {"APPROVED": "approved", "REJECTED": "rejected", "RETURNED": "returned for clarification"}.get(decision, "recorded")
            messages.success(request, f"Stage {verb}. Approval authorises the next step only — custody changes after the receiver confirms receipt.")
        except BusinessError as exc:
            messages.error(request, exc.message)
        return redirect("workflow:approval", pk=pk)

