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
from .forms import AssetForm, AttachmentForm, CatalogFilterForm
from .models import Asset, AssetCollection, AssetStatus, Attachment, Category, Condition, specialise


def visible_assets(user, base=None):
    """Search results as an AssetCollection [UML: Aggregation — Figure 19]."""
    qs = (base if base is not None else Asset.objects.all()).select_related("category__parent", "owner_department", "custodian")
    return AssetCollection(a for a in qs if policy.can_view_asset(user, a))


class CatalogView(AppView):
    template_name = "catalog/list.html"
    nav = "catalog"
    mine = False

    def get(self, request):
        form = CatalogFilterForm(request.GET or None)
        qs = Asset.objects.all()
        if self.mine:
            scopes = policy.scoped_departments(request.user, Role.CUSTODIAN)
            policy.require(Role.CUSTODIAN in policy.role_codes(request.user), "Only custodians have an asset register.", "my_assets")
            qs = qs if scopes is None else qs.filter(Q(owner_department__in=scopes) | Q(custodian=request.user))
        else:
            qs = qs.filter(is_published=True, status__in=AssetStatus.LISTED)
        filters_active = False
        if form.is_bound and form.is_valid():
            d = form.cleaned_data
            if d.get("status"):
                qs = Asset.objects.filter(status=d["status"]) if not self.mine else qs.filter(status=d["status"])
            if d.get("q"):
                terms = d["q"].split()
                for t in terms:
                    qs = qs.filter(Q(asset_tag__icontains=t) | Q(name__icontains=t) | Q(description__icontains=t) | Q(tags__icontains=t) | Q(specifications__icontains=t) | Q(category__name__icontains=t))
            if d.get("category"):
                c = d["category"]
                qs = qs.filter(Q(category=c) | Q(category__parent=c))
            if d.get("condition"):
                ok = [code for code, rank in Condition.RANK.items() if rank >= Condition.RANK[d["condition"]]]
                qs = qs.filter(condition__in=ok)
            if d.get("department"):
                qs = qs.filter(owner_department=d["department"])
            if d.get("location"):
                qs = qs.filter(Q(campus__icontains=d["location"]) | Q(building__icontains=d["location"]))
            filters_active = any(v not in (None, "") for k, v in d.items() if k != "sort")
        assets = visible_assets(request.user, qs.distinct())  # AssetCollection [UML: Aggregation]
        rows = []
        for a in assets:
            bal = services.balance(a)
            free = services.free_listed_quantity(a)
            age = a.age_years()
            rows.append({"asset": a, "free": free, "bal": bal, "age": age, "fin": policy.can_view_financials(request.user, a)})
        if form.is_bound and form.is_valid():
            d = form.cleaned_data
            if d.get("min_free"):
                rows = [r for r in rows if r["free"] >= d["min_free"]]
            # Unknown ages/values never satisfy a numeric range (design 5.9.3).
            if d.get("min_age") is not None:
                rows = [r for r in rows if r["age"] is not None and r["age"] >= float(d["min_age"])]
            if d.get("max_age") is not None:
                rows = [r for r in rows if r["age"] is not None and r["age"] <= float(d["max_age"])]
            if d.get("min_value") is not None:
                rows = [r for r in rows if r["fin"] and r["asset"].unit_value_aed is not None and r["asset"].unit_value_aed >= d["min_value"]]
            if d.get("max_value") is not None:
                rows = [r for r in rows if r["fin"] and r["asset"].unit_value_aed is not None and r["asset"].unit_value_aed <= d["max_value"]]
            sort = d.get("sort") or "tag"
            key = {"tag": lambda r: r["asset"].asset_tag, "name": lambda r: r["asset"].name.lower(), "free": lambda r: -r["free"], "newest": lambda r: -r["asset"].created_at.timestamp()}[sort]
            rows.sort(key=key)
        page = Paginator(rows, 12).get_page(request.GET.get("page"))
        params = request.GET.copy()
        params.pop("page", None)
        return self.render(request, {"form": form, "page": page, "total": len(rows), "filters_active": filters_active, "mine": self.mine,
                                     "querystring": params.urlencode(), "can_register": policy.has_role(request.user, Role.CUSTODIAN),
                                     "roots": Category.objects.filter(parent__isnull=True, active=True)})


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
    """Publish / unpublish / attach — POST only."""

    def post(self, request, pk):
        asset = get_object_or_404(Asset, pk=pk)
        action = request.POST.get("action")
        try:
            if action == "publish":
                self.command(request, "publish", {"asset": str(pk)}, lambda: {"id": str(services.publish(request.user, pk, request.POST.get("version")).pk)})
                messages.success(request, f"{asset.asset_tag} is now published in the catalog.")
            elif action == "unpublish":
                def run():
                    a, committed = services.unpublish(request.user, pk, request.POST.get("version"), request.POST.get("reason", ""))
                    return {"committed": committed}
                res = self.command(request, "unpublish", {"asset": str(pk)}, run)
                note = f" {res['committed']} held/allocated unit(s) are preserved until their workflow resolves." if res.get("committed") else ""
                messages.success(request, f"{asset.asset_tag} unpublished.{note}")
            elif action == "attach":
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
        from apps.intelligence import classification
        from apps.intelligence.models import AIResult, HumanReview

        asset = self.get_asset(pk)
        action = request.POST.get("action", "save")
        data = request.POST.copy()
        form = AssetForm(data, user=request.user, editing=asset is not None)
        suggestion = None
        if action == "suggest":
            try:
                outcome = classification.suggest(request.user, data.get("name", ""), data.get("description", ""), data.get("specifications", ""))
                suggestion = outcome.result
                data["ai_result"] = str(outcome.result.pk)
                data["ai_choice"] = ""
                form = AssetForm(data, user=request.user, editing=asset is not None)
            except ValueError as exc:
                messages.error(request, str(exc))
            return self.render(request, {"form": self._unvalidated(form), "asset": asset, "suggestion": suggestion})
        if action in ("apply_suggestion", "reject_suggestion"):
            result = AIResult.objects.filter(pk=V.uuid_or_none(data.get("ai_result")), task__requested_by=request.user).first()
            if result is None:
                messages.error(request, "That suggestion is no longer available.")
            elif action == "apply_suggestion":
                out = result.output
                if out.get("category_id"):
                    data["category"] = out["category_id"]
                if out.get("condition"):
                    data["condition"] = out["condition"]
                if out.get("tags"):
                    data["tags"] = ", ".join(out["tags"])
                if out.get("material") and not data.get("material"):
                    data["material"] = out["material"]
                data["ai_choice"] = HumanReview.ACCEPTED
                data["ai_suggested"] = f"{out.get('category_id')}|{out.get('condition')}|{', '.join(out.get('tags', []))}"
                messages.info(request, "Suggestion applied to the form. Review or edit the values — nothing is saved until you press Save.")
            else:
                data["ai_choice"] = HumanReview.REJECTED
                messages.info(request, "Suggestion rejected; your manual values are kept.")
            form = AssetForm(data, user=request.user, editing=asset is not None)
            return self.render(request, {"form": self._unvalidated(form), "asset": asset, "suggestion": result if action == "apply_suggestion" else None, "applied": action == "apply_suggestion"})
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


# --------------------------------------------------------- bonus: QR labels ---
class QRLookupView(AppView):
    """Where a scanned label lands: resolve the printed asset ID, then the normal permission check applies."""

    def get(self, request, tag):
        tag = (tag or "").strip().upper()[:24]
        try:
            Asset._meta.get_field("asset_tag").run_validators(tag)
        except Exception:  # noqa: BLE001 - malformed label → not found
            raise Http404("Unknown label")
        asset = get_object_or_404(Asset, asset_tag=tag)
        return redirect("catalog:asset", pk=asset.pk)


class LabelSheetView(AppView):
    """Printable QR labels: one asset (?asset=) or every batch the custodian manages."""

    template_name = "catalog/labels.html"
    nav = "assets"

    def get(self, request):
        from . import qr

        one = V.uuid_or_none(request.GET.get("asset"))
        if one:
            assets = [get_object_or_404(Asset.objects.select_related("owner_department", "category"), pk=one)]
        else:
            policy.require(policy.has_role(request.user, Role.CUSTODIAN), "Only asset custodians print labels.", "labels")
            assets = [a for a in Asset.objects.exclude(status__in=AssetStatus.FINAL).select_related("owner_department", "category").order_by("asset_tag")
                      if policy.can_manage_asset(request.user, a)][:60]
        for a in assets:
            policy.require(policy.can_manage_asset(request.user, a), "Only this department's custodian can print its labels.", "labels", a)
        return self.render(request, {"labels": [{"asset": a, "svg": qr.svg(request, a, scale=4), "url": qr.label_url(request, a)} for a in assets]})


class ScanView(AppView):
    """Camera scanner (any role): read a label and open that asset."""

    template_name = "catalog/scan.html"
    nav = "scan"

    def get(self, request):
        return self.render(request, {})

    def post(self, request):
        code = (request.POST.get("code") or "").strip()
        tag = code.rstrip("/").rsplit("/", 1)[-1].strip().upper()[:24]
        asset = Asset.objects.filter(asset_tag=tag).first() if tag else None
        if asset is None:
            messages.error(request, "No asset has that code. Check the printed ID under the QR label (e.g. A-101).")
            return redirect("catalog:scan")
        return redirect("catalog:asset", pk=asset.pk)
