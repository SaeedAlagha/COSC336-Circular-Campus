from datetime import date, datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from apps.accounts import policy
from apps.accounts.models import Department, Role
from apps.catalog.models import Category
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.views import AppView
from apps.exchange.models import RequestStatus, ResourceRequest
from apps.workflow.models import Transfer, TransferStatus

from . import metrics, services
from .models import AuditEvent, EmissionFactor, Notification, Report

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class PeriodForm(forms.Form):
    start = forms.DateField(required=False, widget=DATE, label="From")
    end = forms.DateField(required=False, widget=DATE, label="To")
    department = forms.ModelChoiceField(queryset=Department.objects.order_by("name"), required=False, empty_label="Campus-wide")

    def period(self):
        today = timezone.localdate()
        d = self.cleaned_data if self.is_valid() else {}
        start, end = d.get("start") or today - timedelta(days=89), d.get("end") or today
        if start > end:
            start, end = end, start
        return start, end, d.get("department")


def _scope_or_deny(user, dept):
    scope = policy.report_scope(user)
    if scope is None:
        return dept
    if dept is None:
        # No campus-wide grant: default to the user's own scoped department.
        if user.department_id in scope:
            return user.department
        return Department.objects.filter(pk__in=scope).first()
    policy.require(dept.pk in scope, "That department is outside your reporting scope.", "report_scope")
    return dept


class SustainabilityView(AppView):
    template_name = "reporting/sustainability.html"
    nav = "sustainability"

    def get(self, request):
        policy.require(policy.can_view_reports(request.user), "You do not have access to sustainability reporting.", "sustainability")
        form = PeriodForm(request.GET or None)
        start, end, dept = form.period() if form.is_bound else PeriodForm({}).period()
        dept = _scope_or_deny(request.user, dept)
        data = metrics.compute(start, end, dept)
        ind = data["indicators"]
        div = ind["waste_diversion"]["inputs"]
        cohort = None
        if div:
            total = div.get("cohort units") or 0
            cohort = [{"key": "diverted", "label": "Diverted", "value": div.get("diverted", 0)}, {"key": "pending", "label": "Pending", "value": div.get("pending", 0)},
                      {"key": "discarded", "label": "Discarded", "value": div.get("discarded (disposed)", 0)}]
            for c in cohort:
                c["pct"] = round(c["value"] * 100 / total, 1) if total else 0
        # Reuse by category (counted units, period) — single series.
        from django.db.models import Sum

        from .models import SustainabilityImpact

        start_dt, end_dt = metrics._bounds(start, end)
        rows = (SustainabilityImpact.objects.filter(metrics._scope_q(dept), indicator="REUSE", counted=True, occurred_at__range=(start_dt, end_dt))
                .values("asset__category__parent__name").annotate(q=Sum("quantity")).order_by("-q"))
        by_cat = [{"label": r["asset__category__parent__name"] or "Other", "value": r["q"]} for r in rows]
        peak = max([r["value"] for r in by_cat] + [1])
        for r in by_cat:
            r["pct"] = round(r["value"] * 100 / peak, 1)
        factors = EmissionFactor.objects.filter(active=True).select_related("category", "entered_by")
        return self.render(request, {"form": form, "data": data, "ind": ind, "start": start, "end": end, "dept": dept, "cohort": cohort, "by_cat": by_cat,
                                     "factors": factors, "can_factor": policy.can_manage_factors(request.user),
                                     "categories": Category.objects.filter(active=True).select_related("parent").order_by("parent__name", "name")})

    def post(self, request):
        p = request.POST
        try:
            cat = Category.objects.filter(pk=V.parse_uuid(p.get("category"), "category"), active=True).first()
            if cat is None:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a category.", {"category": "Choose a category."})
            services.add_emission_factor(request.user, cat, p.get("replacement") or None, p.get("transport") or "0", p.get("source", ""), p.get("version", ""), p.get("is_synthetic") == "on")
            messages.success(request, "Emission factor recorded with its source. Carbon estimates now use it and stay labelled as estimates.")
        except BusinessError as exc:
            messages.error(request, exc.message)
        return redirect("reporting:sustainability")


class AuditView(AppView):
    """UC-14 / FR-55: read-only audit search. SYSADMIN sees technical events only; Auditor sees all in scope."""

    template_name = "reporting/audit.html"
    nav = "audit"

    def get(self, request):
        user = request.user
        policy.require(policy.can_search_audit(user), "Audit search needs the auditor or system-administrator permission.", "audit")
        qs = AuditEvent.objects.select_related("actor").order_by("-created_at")
        technical_only = not policy.has_role(user, Role.AUDITOR)
        if technical_only:
            qs = qs.filter(record_type__in=AuditEvent.TECHNICAL_TYPES)
        g = request.GET
        if g.get("record"):
            qs = qs.filter(Q(record_label__icontains=g["record"]) | Q(record_id__icontains=g["record"]))
        if g.get("actor"):
            qs = qs.filter(Q(actor__campus_id__icontains=g["actor"]) | Q(actor__full_name__icontains=g["actor"]) | Q(service_actor__icontains=g["actor"]))
        if g.get("action"):
            qs = qs.filter(action__icontains=g["action"])
        if g.get("outcome"):
            qs = qs.filter(outcome=g["outcome"])
        for key, lookup in (("from", "created_at__date__gte"), ("to", "created_at__date__lte")):
            if g.get(key):
                try:
                    qs = qs.filter(**{lookup: date.fromisoformat(g[key])})
                except ValueError:
                    messages.error(request, "Dates must be YYYY-MM-DD.")
        page = Paginator(qs, 25).get_page(g.get("page"))
        params = g.copy()
        params.pop("page", None)
        return self.render(request, {"page": page, "g": g, "technical_only": technical_only, "qs": params.urlencode(), "total": qs.count()})

