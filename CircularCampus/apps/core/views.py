"""Presentation-layer base classes and the role-aware dashboard.

Views stay thin: they parse forms, call one application service, and translate
BusinessError codes into field errors / messages. No business rule lives here.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponseRedirect
from django.shortcuts import render
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone
from django.views import View

from apps.accounts import policy
from apps.accounts.models import Role

from .commands import execute
from .errors import BusinessError


# [UML: Inheritance — every screen class inherits AppView (login + command handling)]
class AppView(LoginRequiredMixin, View):
    """Authenticated view; subclasses must also call policy checks for records."""

    template_name = None
    nav = ""

    def render(self, request, context=None, status=200):
        ctx = {"nav": self.nav, "form_key": uuid.uuid4()}
        ctx.update(context or {})
        return render(request, self.template_name, ctx, status=status)

    def command(self, request, name, payload, fn):
        """Run an idempotent business command keyed by the form's hidden ``idem`` field."""
        response, replayed = execute(request.user, name, request.POST.get("idem"), payload, fn)
        if replayed:
            messages.info(request, "This form was already submitted — showing the original result (no duplicate effect).")
        return response

    @staticmethod
    def back(request, fallback):
        ref = request.META.get("HTTP_REFERER", "")
        if ref and urlparse(ref).netloc in ("", request.get_host()):
            return HttpResponseRedirect(ref)
        return HttpResponseRedirect(fallback)


def apply_errors(form, exc: BusinessError):
    """Attach a BusinessError's field errors to a bound Django form; the rest goes to non-field errors."""
    placed = False
    for field, msg in exc.field_errors.items():
        if form is not None and field in form.fields:
            form.add_error(field, msg)
            placed = True
    if form is not None and (not placed or not exc.field_errors):
        form.add_error(None, exc.message)
    return form


def fail(request, exc: BusinessError):
    messages.error(request, exc.message)


# ------------------------------------------------------------------ dashboard ---
class DashboardView(AppView):
    template_name = "core/dashboard.html"
    nav = "dashboard"

    def get(self, request):
        from apps.catalog.models import Asset, AssetStatus
        from apps.exchange.models import RequestStatus, Reservation, ReservationStatus, ResourceRequest
        from apps.maintenance.models import MaintenanceRecord, MaintenanceStatus
        from apps.reporting import metrics
        from apps.reporting.models import Notification, Report, SustainabilityImpact
        from apps.workflow.models import Transfer, TransferStatus
        from apps.workflow.queues import my_pending_stages

        user = request.user
        roles = policy.role_codes(user)
        today = timezone.localdate()
        actions = []
        stages = my_pending_stages(user)
        for a in stages[:6]:
            actions.append({"icon": "shield-check", "tone": "info", "title": f"{a.get_stage_display()} — {a.version.target.reference}",
                            "meta": f"{a.version.get_kind_display()} {a.version.label} · proposed by {a.version.proposer.full_name}", "url": reverse("workflow:approval", args=[a.pk]), "cta": "Review"})
        for t in Transfer.objects.filter(releasing_user=user, status__in=[TransferStatus.APPROVED, TransferStatus.SCHEDULED]).select_related("source_asset")[:5]:
            actions.append({"icon": "truck", "tone": "brand", "title": f"Record release — {t.reference}", "meta": f"{t.quantity} × {t.source_asset.name} → {t.destination}", "url": reverse("workflow:transfer", args=[t.pk]), "cta": "Release"})
        for t in Transfer.objects.filter(receiving_user=user, status=TransferStatus.RELEASED).select_related("source_asset")[:5]:
            actions.append({"icon": "inbox", "tone": "success", "title": f"Confirm receipt — {t.reference}", "meta": f"{t.quantity} × {t.source_asset.name} released {timezone.localtime(t.released_at):%d %b}", "url": reverse("workflow:transfer", args=[t.pk]), "cta": "Confirm"})
        if Role.APPROVER in roles:
            for t in Transfer.objects.filter(status=TransferStatus.EXCEPTION).select_related("reservation__request", "source_asset")[:4]:
                if policy.can_resolve_exception(user, t):
                    actions.append({"icon": "triangle-alert", "tone": "danger", "title": f"Resolve exception — {t.reference}", "meta": t.exception_reason[:90], "url": reverse("workflow:transfer", args=[t.pk]), "cta": "Resolve"})
        if Role.MAINTENANCE in roles:
            for m in MaintenanceRecord.objects.filter(status__in=[MaintenanceStatus.REPORTED, MaintenanceStatus.UNDER_INSPECTION, MaintenanceStatus.IN_SERVICE]).select_related("asset")[:4]:
                if policy.can_service(user, m.asset):
                    actions.append({"icon": "wrench", "tone": "warning", "title": f"{m.status_label} — {m.reference}", "meta": f"{m.asset.asset_tag} {m.asset.name}", "url": reverse("maintenance:record", args=[m.pk]), "cta": "Open"})
        if Role.CUSTODIAN in roles:
            for m in MaintenanceRecord.objects.filter(status=MaintenanceStatus.AWAITING_VERIFICATION).select_related("asset")[:4]:
                if policy.can_verify_service(user, m.asset) and m.assigned_staff_id != user.pk:
                    actions.append({"icon": "circle-check", "tone": "warning", "title": f"Verify safe return — {m.reference}", "meta": f"{m.asset.asset_tag} {m.asset.name}", "url": reverse("maintenance:record", args=[m.pk]), "cta": "Verify"})
        if policy.can_author_report(user):
            for r in Report.objects.filter(status__in=[Report.DRAFT, Report.REVIEWED], generated_by=user)[:3]:
                actions.append({"icon": "file-text", "tone": "ai", "title": f"{r.get_status_display()} — {r.reference}", "meta": r.get_report_type_display(), "url": reverse("reporting:report", args=[r.pk]), "cta": "Continue"})

        my_requests = ResourceRequest.objects.filter(requester=user).select_related("category").order_by("-created_at")[:5]
        expiring = Reservation.objects.filter(request__requester=user, status=ReservationStatus.PENDING, expires_at__lte=timezone.now() + timedelta(hours=24)).select_related("asset", "request")
        start = today - timedelta(days=89)
        snapshot = metrics.compute(start, today) if (roles & (Role.REPORTING | {Role.AUDITOR, Role.APPROVER, Role.CUSTODIAN})) else None

        # Units reused per month (last 6 months) — counted units only (SUS-09).
        months = []
        first = (today.replace(day=1) - timedelta(days=155)).replace(day=1)
        rows = (SustainabilityImpact.objects.filter(indicator=SustainabilityImpact.REUSE, counted=True, occurred_at__date__gte=first)
                .annotate(m=TruncMonth("occurred_at")).values("m").annotate(q=Sum("quantity")))
        by_month = {r["m"].date().replace(day=1) if hasattr(r["m"], "date") else r["m"]: r["q"] for r in rows}
        cursor = first
        while cursor <= today:
            months.append({"label": cursor.strftime("%b"), "full": cursor.strftime("%B %Y"), "value": by_month.get(cursor, 0)})
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        peak = max([m["value"] for m in months] + [1])
        for m in months:
            m["pct"] = round(m["value"] * 100 / peak, 1)

        visible_listed = [a for a in Asset.objects.filter(is_published=True, status__in=AssetStatus.LISTED).select_related("category") if policy.can_view_asset(user, a)]
        from apps.catalog.services import free_listed_quantity

        kpis = {
            "free_units": sum(free_listed_quantity(a) for a in visible_listed),
            "listings": len(visible_listed),
            "open_requests": ResourceRequest.objects.filter(Q(requester=user) | Q(department=user.department), status__in=RequestStatus.OPEN - {RequestStatus.DRAFT}).count(),
            "my_actions": len(actions),
        }
        recent = Notification.objects.filter(recipient=user)[:6]
        new_listings = sorted(visible_listed, key=lambda a: a.updated_at, reverse=True)[:4]
        return self.render(request, {"actions": actions, "my_requests": my_requests, "expiring": expiring, "snapshot": snapshot, "months": months,
                                     "kpis": kpis, "recent": recent, "new_listings": new_listings, "period_start": start, "period_end": today})


class HelpView(AppView):
    template_name = "core/help.html"
    nav = "help"

    def get(self, request):
        from apps.intelligence.assistant import POLICIES
        from apps.intelligence.matching import LABELS, WEIGHTS

        return self.render(request, {"policies": POLICIES, "weights": [(k, LABELS[k], int(v * 100)) for k, v in WEIGHTS.items()]})


def error_403(request, exception=None):
    message = getattr(exception, "safe_message", "") or "You do not have permission to view this page or perform this action."
    return render(request, "errors/403.html", {"message": message}, status=403)


def error_404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)
