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


def _period_filter(form):
    start, end, dept = form.period()
    tz = timezone.get_current_timezone()
    return start, end, dept, timezone.make_aware(datetime.combine(start, time.min), tz), timezone.make_aware(datetime.combine(end, time.max), tz)


class OperationalFilter(PeriodForm):
    status = forms.ChoiceField(required=False, choices=[("", "Any status")] + sorted({(c, l) for c, l in RequestStatus.CHOICES} | {(c, l) for c, l in TransferStatus.CHOICES}))
    table = forms.ChoiceField(choices=[("requests", "Requests"), ("transfers", "Transfers")], required=False)


class OperationalReportView(AppView):
    """C9 — operational tables + CSV export using exactly the same filters and permissions."""

    template_name = "reporting/operational.html"
    nav = "reports"

    def data(self, request):
        policy.require(policy.can_view_reports(request.user), "You do not have access to operational reports.", "operational_report")
        form = OperationalFilter(request.GET or None)
        if form.is_bound:
            form.is_valid()
        else:
            form = OperationalFilter({})
            form.is_valid()
        start, end, dept, sdt, edt = _period_filter(form)
        dept = _scope_or_deny(request.user, dept)
        status = form.cleaned_data.get("status", "")
        reqs = ResourceRequest.objects.filter(created_at__range=(sdt, edt)).select_related("category", "department", "requester", "assigned_approver").order_by("-created_at")
        trs = Transfer.objects.filter(created_at__range=(sdt, edt)).select_related("source_asset__owner_department", "reservation__request__department", "releasing_user", "receiving_user").order_by("-created_at")
        if dept is not None:
            reqs = reqs.filter(department=dept)
            trs = trs.filter(Q(reservation__request__department=dept) | Q(source_asset__owner_department=dept))
        if status:
            reqs = reqs.filter(status=status)
            trs = trs.filter(status=status)
        return form, start, end, dept, reqs, trs

    def get(self, request):
        form, start, end, dept, reqs, trs = self.data(request)
        export = request.GET.get("export")
        if export in ("requests", "transfers"):
            if export == "requests":
                header = ["Request", "Title", "Category", "Department", "Requester", "Quantity", "Received", "Status", "Required date", "Created", "Approver"]
                rows = [[r.reference, r.title, str(r.category), r.department.name, r.requester.full_name, r.quantity, r.received_quantity, r.status_label, r.required_date,
                         timezone.localtime(r.created_at).strftime("%Y-%m-%d %H:%M"), r.assigned_approver.full_name if r.assigned_approver else ""] for r in reqs]
            else:
                header = ["Transfer", "Request", "Asset", "From", "To", "Quantity", "Received qty", "Status", "Released", "Received", "Releasing custodian", "Receiving custodian"]
                rows = [[t.reference, t.reservation.request.reference, t.source_asset.asset_tag, t.source_asset.owner_department.name, t.reservation.request.department.name,
                         t.quantity, t.received_quantity if t.received_quantity is not None else "", t.status_label,
                         timezone.localtime(t.released_at).strftime("%Y-%m-%d %H:%M") if t.released_at else "", timezone.localtime(t.received_at).strftime("%Y-%m-%d %H:%M") if t.received_at else "",
                         t.releasing_user.full_name, t.receiving_user.full_name] for t in trs]
            from . import audit

            audit.record(request.user, f"CSV export {export}", "report", label=f"{start}..{end}", after={"rows": len(rows), "department": dept.code if dept else "CAMPUS"})
            resp = HttpResponse(services.to_csv(header, rows), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="circular-campus-{export}-{start}-{end}.csv"'
            return resp
        statuses = sorted({(c, l) for c, l in RequestStatus.CHOICES} | {(c, l) for c, l in TransferStatus.CHOICES})
        rp = Paginator(reqs, 15).get_page(request.GET.get("rpage"))
        tp = Paginator(trs, 15).get_page(request.GET.get("tpage"))
        params = request.GET.copy()
        for k in ("rpage", "tpage", "export"):
            params.pop(k, None)
        return self.render(request, {"form": form, "start": start, "end": end, "dept": dept, "reqs": rp, "trs": tp, "statuses": statuses,
                                     "status": form.cleaned_data.get("status", ""), "qs": params.urlencode(),
                                     "summary": metrics.compute(start, end, dept)["operational"]})


class NewReportForm(forms.Form):
    report_type = forms.ChoiceField(choices=Report.TYPES, label="Narrative type")
    start = forms.DateField(widget=DATE, label="Period start")
    end = forms.DateField(widget=DATE, label="Period end")
    department = forms.ModelChoiceField(queryset=Department.objects.order_by("name"), required=False, empty_label="Campus-wide")

    def clean(self):
        d = super().clean()
        if d.get("start") and d.get("end") and d["start"] > d["end"]:
            self.add_error("end", "The period end must be on or after its start.")
        if d.get("start") and d["start"] > timezone.localdate():
            self.add_error("start", "The period cannot start in the future.")
        return d


class ReportListView(AppView):
    template_name = "reporting/reports.html"
    nav = "narratives"

    def get(self, request):
        user = request.user
        policy.require(policy.can_author_report(user) or policy.has_role(user, Role.AUDITOR) or policy.can_view_reports(user), "You cannot view reports.", "reports")
        reports = Report.objects.select_related("generated_by", "scope_department", "published_by")
        if not policy.can_author_report(user):
            reports = reports.filter(status=Report.PUBLISHED)
        today = timezone.localdate()
        return self.render(request, {"reports": reports, "form": NewReportForm(initial={"start": today.replace(day=1) - timedelta(days=60), "end": today}), "author": policy.can_author_report(user)})

    def post(self, request):
        form = NewReportForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Choose a narrative type and a valid period. " + " ".join(e for errs in form.errors.values() for e in errs))
            return redirect("reporting:reports")
        d = form.cleaned_data
        try:
            r = services.create_report(request.user, d["report_type"], d["start"], d["end"], d["department"])
        except BusinessError as exc:
            messages.error(request, exc.message)
            return redirect("reporting:reports")
        messages.success(request, f"{r.reference}: totals calculated by the database and frozen in an immutable snapshot. Generate the narrative next.")
        return redirect("reporting:report", pk=r.pk)


class ReportDetailView(AppView):
    """AI feature 5: database totals → Gemini draft → human edit → explicit review → separate publish."""

    template_name = "reporting/report.html"
    nav = "narratives"

    def get(self, request, pk):
        r = get_object_or_404(Report.objects.select_related("generated_by", "reviewed_by", "published_by", "ai_result__model_version", "scope_department"), pk=pk)
        author = policy.can_author_report(request.user)
        policy.require(author or (r.status == Report.PUBLISHED and policy.can_view_reports(request.user)), "This report is not published.", "view_report", r)
        return self.render(request, {"r": r, "author": author and r.status != Report.PUBLISHED, "ind": r.snapshot["indicators"], "op": r.snapshot["operational"],
                                     "links": r.metrics.count(), "edited": r.narrative_text != r.narrative_original})

    def post(self, request, pk):
        action = request.POST.get("action")
        ver = request.POST.get("version")
        try:
            if action == "generate":
                r, outcome = services.generate_narrative(request.user, pk)
                if outcome.fallback:
                    messages.warning(request, "Gemini was not used: " + outcome.message + " A TEMPLATE draft — not AI-generated — was created from the snapshot so the review workflow stays usable.")
                else:
                    messages.success(request, "Gemini drafted the narrative from the snapshot. Review and edit it — it is NOT published.")
            elif action == "save":
                services.edit_narrative(request.user, pk, ver, request.POST.get("narrative_text", ""))
                messages.success(request, "Narrative saved as a draft. Review it again before publishing.")
            elif action == "review":
                services.mark_reviewed(request.user, pk, ver, request.POST.get("confirmed") == "on")
                messages.success(request, "Marked as reviewed. Publishing is a separate, explicit step.")
            elif action == "publish":
                services.publish(request.user, pk, ver)
                messages.success(request, "Report published.")
            else:
                messages.error(request, "Unknown action.")
        except BusinessError as exc:
            messages.error(request, exc.message)
        return redirect("reporting:report", pk=pk)


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


class NotificationsView(AppView):
    template_name = "reporting/notifications.html"
    nav = "notifications"

    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user)
        show = request.GET.get("show", "all") if request.GET.get("show") in ("all", "unread") else "all"
        if show == "unread":
            qs = qs.filter(read_at__isnull=True)
        return self.render(request, {"page": Paginator(qs, 20).get_page(request.GET.get("page")), "show": show})

    def post(self, request):
        if request.POST.get("action") == "read_all":
            Notification.objects.filter(recipient=request.user, read_at__isnull=True).update(read_at=timezone.now())
            messages.success(request, "All notifications marked as read.")
            return redirect("reporting:notifications")
        n = get_object_or_404(Notification, pk=V.uuid_or_404(request.POST.get("id")), recipient=request.user)  # only own notices
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at"])
        if request.POST.get("action") == "open" and n.link.startswith("/") and not n.link.startswith("//"):
            return redirect(n.link)  # the target view re-checks permission
        return redirect("reporting:notifications")
