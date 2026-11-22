from datetime import date

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from apps.accounts import policy
from apps.accounts.models import Role
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.views import AppView, apply_errors

from . import services
from .forms import AssetForm, AttachmentForm
from .models import Asset, AssetCollection, AssetStatus, Attachment, Category, Condition, specialise


class AssetDetailView(AppView):
    template_name = "catalog/detail.html"
    nav = "catalog"

    def get(self, request, pk):
        asset = get_object_or_404(Asset.objects.select_related("category__parent", "owner_department", "custodian", "parent_batch"), pk=pk)
        policy.require(policy.can_view_asset(request.user, asset), "This asset is not visible to you.", "view_asset", asset)
        asset = specialise(asset)  # ITAsset / FurnitureAsset [UML: Inheritance — Figure 19]
        from apps.exchange.services import expire_due_holds

        expire_due_holds(asset)
        user = request.user
        manage = policy.can_manage_asset(user, asset)
        bal = services.balance(asset)
        events = asset.events.select_related("actor", "transfer", "maintenance", "disposition").order_by("-occurred_at")
        lineage_up = asset.lineage_parents.select_related("parent_asset", "event")
        lineage_down = asset.lineage_children.select_related("child_asset", "event")
        holds = asset.reservations.select_related("request").order_by("-created_at") if (manage or policy.can_view_financials(user, asset)) else []
        ctx = {
            "asset": asset, "bal": bal, "free": services.free_listed_quantity(asset), "manage": manage, "fin": policy.can_view_financials(user, asset),
            "events": events, "lineage_up": lineage_up, "lineage_down": lineage_down, "holds": holds,
            "attachments": asset.attachments.all(), "attach_form": AttachmentForm(),
            "maintenance": asset.maintenance_records.order_by("-reported_at")[:5], "dispositions": asset.dispositions.order_by("-created_at")[:5],
            "can_request": policy.is_requester_like(user) and asset.owner_department_id != user.department_id and services.free_listed_quantity(asset) > 0,
            "can_defect": policy.can_report_defect(user, asset) and asset.status not in AssetStatus.FINAL | {AssetStatus.DISPOSITION_PENDING} and not asset.maintenance_records.filter(status__in=["REPORTED", "UNDER_INSPECTION", "AWAITING_APPROVAL", "IN_SERVICE", "AWAITING_VERIFICATION"]).exists(),
            "can_advice": (manage or policy.has_role(user, Role.SUSTAINABILITY)) and asset.status not in AssetStatus.FINAL,
            "type_fields": [(k.replace("_", " ").title(), v) for k, v in asset.type_metadata.items()],
            "age": asset.age_years(),
            "can_edit": manage and asset.status not in AssetStatus.FINAL,
            "can_publish": manage and not asset.is_published and asset.status in (AssetStatus.DRAFT, AssetStatus.IN_USE),
            "can_propose": manage and asset.status in (AssetStatus.IN_USE, AssetStatus.UNAVAILABLE, AssetStatus.RETIRED) and not bal.committed,
        }
        return self.render(request, ctx)


class AssetActionView(AppView):
    """Attach a photo or document — POST only."""

    def post(self, request, pk):
        asset = get_object_or_404(Asset, pk=pk)
        action = request.POST.get("action")
        try:
            if action == "attach":
                form = AttachmentForm(request.POST, request.FILES)
                if not form.is_valid():
                    messages.error(request, "Choose a file to upload.")
                else:
                    att = services.add_attachment(request.user, pk, form.cleaned_data["file"])
                    messages.success(request, f"Attachment #{att.attachment_no} uploaded ({att.original_name}).")
            else:
                messages.error(request, "Unknown action.")
        except BusinessError as exc:
            messages.error(request, exc.message)
        return redirect("catalog:asset", pk=pk)


class AttachmentDownloadView(AppView):
    """FR-06: attachments inherit the asset's access rules; no public media URL exists."""

    def get(self, request, pk, number):
        att = get_object_or_404(Attachment.objects.select_related("asset"), asset_id=pk, attachment_no=number)
        policy.require(policy.can_view_asset(request.user, att.asset), "You cannot open this attachment.", "attachment", att.asset)
        path = services.attachment_path(att)
        if not path.exists():
            raise Http404("File missing from protected storage.")
        resp = FileResponse(open(path, "rb"), content_type=att.mime_type, as_attachment=att.kind == Attachment.DOCUMENT, filename=att.original_name)
        resp["X-Content-Type-Options"] = "nosniff"
        resp["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        return resp


class AssetFormView(AppView):
    """UC-01 register/edit with optional UC-10 AI classification (accept / edit / reject)."""

    template_name = "catalog/form.html"
    nav = "assets"

    def get_asset(self, pk):
        if pk is None:
            policy.require(policy.has_role(self.request.user, Role.CUSTODIAN), "Only asset custodians can register assets.", "register_asset")
            return None
        asset = get_object_or_404(Asset, pk=pk)
        policy.require(policy.can_manage_asset(self.request.user, asset), "Only this department's custodian can edit the asset.", "edit_asset", asset)
        return asset

    def get(self, request, pk=None):
        asset = self.get_asset(pk)
        if asset:
            initial = {f: getattr(asset, f) for f in ("name", "description", "specifications", "tags", "category", "quantity", "campus", "building", "room", "condition",
                                                     "purchase_date", "unit_value_aed", "replacement_value_aed", "estimated_transfer_cost_aed", "unit_weight_kg", "available_from", "expected_retirement_date")}
            initial.update(asset.type_metadata)
            form = AssetForm(initial=initial, user=request.user, editing=True)
        else:
            dept = request.user.department
            form = AssetForm(initial={"owner_department": dept if policy.has_role(request.user, Role.CUSTODIAN, dept.pk) else None, "campus": dept.campus, "building": dept.building,
                                      "condition": Condition.GOOD, "quantity": 1}, user=request.user)
        return self.render(request, {"form": form, "asset": asset})

    def post(self, request, pk=None):
        from apps.intelligence.models import AIResult, HumanReview

        asset = self.get_asset(pk)
        action = request.POST.get("action", "save")
        data = request.POST.copy()
        form = AssetForm(data, user=request.user, editing=asset is not None)
        suggestion = None
        if not form.is_valid():
            return self.render(request, {"form": form, "asset": asset})
        try:
            payload = form.service_data()
            if asset is None:
                saved = services.register_asset(request.user, payload)
            else:
                saved = services.update_asset(request.user, asset.pk, request.POST.get("version"), payload, form.cleaned_data.get("reason", ""))
        except BusinessError as exc:
            return self.render(request, {"form": apply_errors(form, exc), "asset": asset})
        self._record_ai_review(request, form, saved)
        messages.success(request, f"{saved.asset_tag} saved." + (" It stays Draft until you publish it." if asset is None else ""))
        return redirect("catalog:asset", pk=saved.pk)

    @staticmethod
    def _unvalidated(form):
        """Re-render a bound form without showing validation errors for an AI step."""
        form._errors = {}
        form.cleaned_data = {}
        return form

    @staticmethod
    def _record_ai_review(request, form, asset):
        from apps.intelligence import classification
        from apps.intelligence.models import AIResult, HumanReview

        rid, choice = form.cleaned_data.get("ai_result"), form.cleaned_data.get("ai_choice")
        if not rid:
            return
        result = AIResult.objects.filter(pk=rid, task__requested_by=request.user).first()
        if not result:
            return
        out = result.output
        edits = {}
        if choice == HumanReview.ACCEPTED:
            if out.get("category_id") and str(asset.category_id) != out["category_id"]:
                edits["category"] = {"suggested": out.get("category_label"), "saved": str(asset.category)}
            if out.get("condition") and asset.condition != out["condition"]:
                edits["condition"] = {"suggested": out["condition"], "saved": asset.condition}
            choice = HumanReview.EDITED if edits else HumanReview.ACCEPTED
        elif choice != HumanReview.REJECTED:
            choice = HumanReview.REJECTED  # shown but not applied = not used
        classification.record_review(request.user, result, choice, edits, f"Asset {asset.asset_tag}")
        services.log_event(asset, request.user, "AI_CLASSIFICATION_REVIEWED", after={"choice": choice, "source": result.source_label, "edits": edits})
