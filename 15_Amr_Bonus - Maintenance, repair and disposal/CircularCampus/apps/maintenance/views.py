from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from apps.accounts import policy
from apps.accounts.models import Role
from apps.catalog.models import Asset, AssetStatus, Condition
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.views import AppView
from apps.intelligence.models import AIResult, HumanReview

from . import services
from .models import Disposition, DispositionAction, Inspection, MaintenanceRecord, MaintenanceStatus


def _dec(v):
    return v if v not in ("", None) else None


class MaintenanceListView(AppView):
    template_name = "maintenance/list.html"
    nav = "maintenance"

    def get(self, request):
        user = request.user
        records = [m for m in MaintenanceRecord.objects.select_related("asset", "reporter", "assigned_staff").order_by("-reported_at") if policy.can_view_maintenance(user, m)]
        disps = [d for d in Disposition.objects.select_related("asset", "proposer").order_by("-created_at") if policy.can_view_asset(user, d.asset) or d.proposer_id == user.pk]
        tab = request.GET.get("tab", "open")
        if tab == "open":
            records = [m for m in records if m.status in MaintenanceStatus.OPEN]
        return self.render(request, {"records": records, "dispositions": disps, "tab": tab, "is_staff": policy.has_role(user, Role.MAINTENANCE)})


class ReportDefectView(AppView):
    template_name = "maintenance/report.html"
    nav = "maintenance"

    def get(self, request, asset_pk):
        asset = get_object_or_404(Asset, pk=asset_pk)
        policy.require(policy.can_report_defect(request.user, asset), "You cannot report a defect for this asset.", "report_defect", asset)
        return self.render(request, {"asset": asset})

    def post(self, request, asset_pk):
        asset = get_object_or_404(Asset, pk=asset_pk)
        try:
            rec = services.report_defect(request.user, asset_pk, request.POST.get("description", ""), request.POST.get("evidence", ""))
            messages.success(request, f"{rec.reference} opened. {asset.asset_tag} is restricted from reuse until a custodian verifies it is safe.")
            return redirect("maintenance:record", pk=rec.pk)
        except BusinessError as exc:
            return self.render(request, {"asset": asset, "error": exc.message, "description": request.POST.get("description", ""), "evidence": request.POST.get("evidence", "")})


class RecordView(AppView):
    template_name = "maintenance/record.html"
    nav = "maintenance"

    def get(self, request, pk):
        r = get_object_or_404(MaintenanceRecord.objects.select_related("asset__owner_department", "reporter", "assigned_staff", "verified_by"), pk=pk)
        user = request.user
        policy.require(policy.can_view_maintenance(user, r), "You cannot view this service record.", "view_maintenance", r)
        flow = ["REPORTED", "UNDER_INSPECTION", "AWAITING_APPROVAL", "IN_SERVICE", "AWAITING_VERIFICATION", "COMPLETED"]
        idx = flow.index(r.status) if r.status in flow else 1
        steps = [{"label": MaintenanceStatus.label(s).split(" / ")[0], "state": "done" if i < idx else "current" if i == idx else "todo"} for i, s in enumerate(flow)]
        if r.status == MaintenanceStatus.UNREPAIRABLE:
            steps = steps[:2] + [{"label": "Unrepairable", "state": "stop"}]
        return self.render(request, {
            "r": r, "steps": steps, "inspections": r.inspections.select_related("inspector"), "versions": r.proposal_versions.prefetch_related("approvals__reviewer").order_by("-version_no"),
            "events": r.events.select_related("actor").order_by("occurred_at"), "results": Inspection.RESULTS, "conditions": Condition.CHOICES, "work_types": MaintenanceRecord.WORK_TYPES,
            "is_staff": policy.can_service(user, r.asset), "can_verify": policy.can_verify_service(user, r.asset) and r.assigned_staff_id != user.pk,
        })

    def post(self, request, pk):
        user, p, action = request.user, request.POST, request.POST.get("action")
        try:
            if action == "start":
                services.start_inspection(user, pk, p.get("version"))
                messages.success(request, "Inspection started and assigned to you.")
            elif action == "inspect":
                services.record_inspection(user, pk, p.get("version"), p.get("finding", ""), p.get("result", ""), p.get("work_type", ""), p.get("proposed_work", ""), p.get("proposed_cost"))
                messages.success(request, "Inspection recorded.")
            elif action == "work":
                services.record_work(user, pk, p.get("version"), p.get("work_done", ""), p.get("actual_cost"), p.get("after_condition", ""))
                messages.success(request, "Work recorded; a custodian must now verify the safe outcome.")
            elif action == "verify":
                revised = p.get("revised_usable_until") or None
                rec = services.verify(user, pk, p.get("version"), p.get("safe") == "yes", p.get("note", ""), p.get("after_condition") or None, revised)
                messages.success(request, "Safe return verified — the asset is back In Use; republish it if it is surplus." if rec.status == MaintenanceStatus.COMPLETED else "Recorded as unsafe; the asset stays restricted for reassessment.")
            else:
                messages.error(request, "Unknown action.")
        except (BusinessError, ValueError) as exc:
            messages.error(request, getattr(exc, "message", str(exc)))
        return redirect("maintenance:record", pk=pk)


class ProposeDispositionView(AppView):
    template_name = "maintenance/propose.html"
    nav = "maintenance"

    def get(self, request, asset_pk):
        asset = get_object_or_404(Asset, pk=asset_pk)
        policy.require(policy.can_manage_asset(request.user, asset), "Only this department's custodian can propose a disposition.", "propose_disposition", asset)
        return self.render(request, {"asset": asset, "routes": DispositionAction.CHOICES, "route": request.GET.get("route", "") if request.GET.get("route", "") in dict(DispositionAction.CHOICES) else "",
                                     "advice": str(V.uuid_or_none(request.GET.get("advice")) or ""), "values": {}})

    def post(self, request, asset_pk):
        asset = get_object_or_404(Asset, pk=asset_pk)
        p = request.POST
        adv_pk = V.uuid_or_none(p.get("advice"))
        adv = AIResult.objects.filter(pk=adv_pk, function="action").first() if adv_pk else None
        try:
            d = services.propose_disposition(request.user, asset_pk, p.get("version"), p.get("action", ""), p.get("quantity") or 0, p.get("justification", ""),
                                             p.get("alternatives", ""), p.get("evidence", ""), p.get("estimated_cost"), p.get("recipient", ""), adv)
            messages.success(request, f"{d.reference} proposed. The asset is Disposition Pending; nothing is final until approval AND completion evidence.")
            return redirect("maintenance:disposition", pk=d.pk)
        except BusinessError as exc:
            policy.require(policy.can_manage_asset(request.user, asset), exc.message, "propose_disposition", asset)
            return self.render(request, {"asset": asset, "routes": DispositionAction.CHOICES, "route": p.get("action", ""), "advice": str(adv_pk or ""), "error": exc.message, "errors": exc.field_errors, "values": p})


class DispositionView(AppView):
    template_name = "maintenance/disposition.html"
    nav = "maintenance"

    def get(self, request, pk):
        d = get_object_or_404(Disposition.objects.select_related("asset__owner_department", "proposer", "completed_by", "ai_result"), pk=pk)
        policy.require(policy.can_view_asset(request.user, d.asset) or d.proposer_id == request.user.pk, "You cannot view this disposition.", "view_disposition", d)
        from .models import AssetLineage

        return self.render(request, {"d": d, "versions": d.proposal_versions.prefetch_related("approvals__reviewer").order_by("-version_no"),
                                     "manage": policy.can_manage_asset(request.user, d.asset), "today": date.today(),
                                     "components": AssetLineage.objects.filter(parent_asset=d.asset, kind=AssetLineage.RECOVERED_COMPONENT).select_related("child_asset")})

    def post(self, request, pk):
        p = request.POST
        try:
            if p.get("action") == "complete":
                cdate = p.get("completion_date") or None
                self.command(request, "complete_disposition", {"d": str(pk)}, lambda: {"id": str(services.complete_disposition(request.user, pk, cdate, p.get("quantity") or 0, p.get("recipient", ""), p.get("evidence", ""), p.get("components", "")).pk)})
                messages.success(request, "Completion evidence recorded; the final state is now set.")
            elif p.get("action") == "resubmit":
                services.resubmit_disposition(request.user, pk, p.get("justification", ""), p.get("alternatives", ""), p.get("evidence", ""), p.get("estimated_cost"), p.get("recipient", ""))
                messages.success(request, "Corrected proposal resubmitted as a new version.")
            else:
                messages.error(request, "Unknown action.")
        except (BusinessError, ValueError) as exc:
            messages.error(request, getattr(exc, "message", str(exc)))
            for f, m in getattr(exc, "field_errors", {}).items():
                messages.error(request, f"{f.replace('_', ' ').capitalize()}: {m}")
        return redirect("maintenance:disposition", pk=pk)
