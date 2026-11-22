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

from .models import AuditEvent, EmissionFactor, Notification, Report

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


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

