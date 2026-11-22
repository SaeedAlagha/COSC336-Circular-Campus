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


class TransferListView(AppView):
    template_name = "workflow/transfers.html"
    nav = "transfers"

    def get(self, request):
        user = request.user
        qs = Transfer.objects.select_related("source_asset", "reservation__request", "releasing_user", "receiving_user").order_by("-created_at")
        rows = [t for t in qs if policy.can_view_transfer(user, t) or policy.has_role(user, Role.AUDITOR)]
        tab = request.GET.get("tab", "active")
        if tab == "active":
            rows = [t for t in rows if t.status not in (TransferStatus.COMPLETED, TransferStatus.CANCELLED)]
        elif tab == "done":
            rows = [t for t in rows if t.status in (TransferStatus.COMPLETED, TransferStatus.CANCELLED)]
        return self.render(request, {"transfers": rows, "tab": tab})


class ScheduleForm(forms.Form):
    method = forms.ChoiceField(choices=Transfer.METHODS)
    scheduled_for = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    arrangements = forms.CharField(required=False, max_length=600, widget=forms.Textarea(attrs={"rows": 2}))


class TransferView(AppView):
    template_name = "workflow/transfer.html"
    nav = "transfers"

    def get(self, request, pk):
        t = get_object_or_404(Transfer.objects.select_related("source_asset__owner_department", "received_asset", "reservation__request__department", "releasing_user", "receiving_user", "approval__version", "approval__reviewer"), pk=pk)
        user = request.user
        policy.require(policy.can_view_transfer(user, t) or policy.has_role(user, Role.AUDITOR) or policy.can_review_avoided_purchase(user, t.reservation.request.department_id),
                       "You cannot view this transfer.", "view_transfer", t)
        req = t.reservation.request
        review = getattr(t, "avoided_purchase", None) if hasattr(t, "avoided_purchase") else None
        steps = [
            {"label": "Approved", "done": True, "who": t.approval.reviewer.full_name if t.approval_id and t.approval.reviewer else "", "when": t.approval.decided_at if t.approval_id else None},
            {"label": "Scheduled", "done": bool(t.scheduled_for), "who": t.get_method_display() if t.method else "", "when": None},
            {"label": "Released", "done": bool(t.released_at), "who": t.releasing_user.full_name, "when": t.released_at},
            {"label": "Received" if t.status != TransferStatus.EXCEPTION else "Exception", "done": t.status == TransferStatus.COMPLETED, "who": t.receiving_user.full_name, "when": t.received_at, "error": t.status == TransferStatus.EXCEPTION},
        ]
        from apps.maintenance.models import LifeCycleEvent

        ctx = {
            "t": t, "req": req, "steps": steps, "conditions": Condition.CHOICES, "schedule_form": ScheduleForm(initial={"method": t.method or Transfer.COLLECTION, "scheduled_for": t.scheduled_for or date.today(), "arrangements": t.arrangements}),
            "can_schedule": user.pk in (t.releasing_user_id, t.receiving_user_id) and t.status in (TransferStatus.APPROVED, TransferStatus.SCHEDULED),
            "can_release": user.pk == t.releasing_user_id and t.status in (TransferStatus.APPROVED, TransferStatus.SCHEDULED),
            "can_receive": user.pk == t.receiving_user_id and t.status == TransferStatus.RELEASED,
            "can_resolve": t.status == TransferStatus.EXCEPTION and policy.can_resolve_exception(user, t),
            "can_review_purchase": t.status == TransferStatus.COMPLETED and review is None and policy.can_review_avoided_purchase(user, req.department_id),
            "review": review, "events": LifeCycleEvent.objects.filter(transfer=t).select_related("actor", "asset").order_by("occurred_at"),
            "restricted": t.source_asset.is_restricted or t.reservation.flagged_for_review,
        }
        return self.render(request, ctx)

    def post(self, request, pk):
        t = get_object_or_404(Transfer, pk=pk)
        action = request.POST.get("action")
        user = request.user
        ver = request.POST.get("version")
        try:
            if action == "schedule":
                form = ScheduleForm(request.POST)
                if not form.is_valid():
                    raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a method and a valid date. " + " ".join(e for errs in form.errors.values() for e in errs))
                services.schedule(user, pk, ver, form.cleaned_data["method"], form.cleaned_data["scheduled_for"], form.cleaned_data["arrangements"])
                messages.success(request, "Handover arrangements saved.")
            elif action == "release":
                cost = request.POST.get("transfer_cost") or None
                self.command(request, "release", {"t": str(pk)}, lambda: {"id": str(services.release(user, pk, ver, request.POST.get("quantity") or 0, request.POST.get("evidence", ""), cost).pk)})
                messages.success(request, "Release recorded. Custody stays with your department until the receiver confirms a matching receipt.")
            elif action == "receive":
                accepted = request.POST.get("accepted") == "yes"
                res = self.command(request, "receive", {"t": str(pk)}, lambda: {"status": services.confirm_receipt(user, pk, ver, request.POST.get("quantity") or 0, request.POST.get("condition", ""), accepted, request.POST.get("note", "")).status})
                if res["status"] == TransferStatus.COMPLETED:
                    messages.success(request, "Receipt confirmed. Custody, department and location were updated atomically.")
                else:
                    messages.warning(request, "The receipt did not match, so the transfer is in Exception. The last confirmed custody is preserved until an approver resolves it.")
            elif action == "resolve":
                services.resolve_exception(user, pk, ver, request.POST.get("resolution", ""), request.POST.get("reason", ""))
                messages.success(request, "Exception resolved.")
            elif action == "avoided":
                from apps.reporting import services as reporting

                confirmed = request.POST.get("confirmed") == "yes"
                reporting.review_avoided_purchase(user, t, confirmed, request.POST.get("substituted_item", ""), request.POST.get("quantity") or None,
                                                  request.POST.get("unit_price") or None, request.POST.get("price_reference", ""), request.POST.get("repair_cost") or None,
                                                  request.POST.get("other_cost") or None, request.POST.get("note", ""))
                messages.success(request, "Avoided-purchase decision recorded." if confirmed else "Recorded that this reuse did not replace a purchase.")
            else:
                messages.error(request, "Unknown action.")
        except BusinessError as exc:
            messages.error(request, exc.message)
            for f, m in exc.field_errors.items():
                messages.error(request, f"{f.replace('_', ' ').capitalize()}: {m}")
        return redirect("workflow:transfer", pk=pk)
