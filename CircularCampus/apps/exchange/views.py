from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone

from apps.accounts import policy
from apps.accounts.models import Role
from apps.catalog import services as catalog
from apps.catalog.models import Asset, Condition
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.views import AppView, apply_errors
from apps.intelligence import matching
from apps.intelligence.models import AIResult, HumanReview, MatchRecommendation
from apps.workflow.models import ProposalVersion

from . import services
from .forms import ReasonForm, RequestForm, SavedSearchForm, SubmitForm
from .models import RequestStatus, ResourceRequest


class RequestListView(AppView):
    template_name = "exchange/list.html"
    nav = "requests"

    def get(self, request):
        user = request.user
        policy.require(policy.is_requester_like(user) or policy.has_role(user, Role.AUDITOR), "Only requesters and department representatives have request lists.", "requests")
        qs = ResourceRequest.objects.select_related("category", "department", "assigned_approver")
        if policy.has_role(user, Role.DEPT_REP, user.department_id):
            qs = qs.filter(Q(requester=user) | Q(department=user.department))
        else:
            qs = qs.filter(requester=user)
        status = request.GET.get("status", "")
        if status == "open":
            qs = qs.filter(status__in=RequestStatus.OPEN)
        elif status == "closed":
            qs = qs.filter(status__in=RequestStatus.CLOSED)
        counts = {s: qs.filter(status=s).count() for s, _ in RequestStatus.CHOICES}
        return self.render(request, {"requests": qs.order_by("-created_at"), "status": status, "counts": counts})


class RequestFormView(AppView):
    template_name = "exchange/form.html"
    nav = "requests"

    def get_req(self, pk):
        if pk is None:
            policy.require(policy.can_create_request(self.request.user), "Only requesters or department representatives can create requests.", "create_request")
            return None
        req = get_object_or_404(ResourceRequest, pk=pk)
        policy.require(policy.can_edit_request(self.request.user, req), "You cannot edit this request.", "edit_request", req)
        return req

    def get(self, request, pk=None):
        req = self.get_req(pk)
        user = request.user
        if req:
            initial = {f: getattr(req, f) for f in services.REQUEST_FIELDS}
        else:
            initial = {"destination_campus": user.department.campus, "destination_building": user.department.building, "preferred_condition": "GOOD", "urgency": "NORMAL"}
            asset_pk = V.uuid_or_none(request.GET.get("asset"))
            asset = Asset.objects.filter(pk=asset_pk).first() if asset_pk else None
            if asset and policy.can_view_asset(user, asset):
                initial.update({"title": asset.name, "category": asset.category, "quantity": min(catalog.free_listed_quantity(asset) or 1, 1)})
            for key in ("title", "purpose", "specifications", "quantity", "required_date", "urgency"):
                if request.GET.get(key):
                    initial[key] = request.GET[key][:500]
            if request.GET.get("category"):
                from apps.catalog.models import Category

                cat = Category.objects.filter(parent__isnull=False).filter(name__iexact=request.GET["category"][:120].split(" / ")[-1]).first()
                if cat:
                    initial["category"] = cat
        form = RequestForm(initial=initial, department=req.department if req else user.department)
        return self.render(request, {"form": form, "req": req, "prefill_asset": str(V.uuid_or_none(request.GET.get("asset")) or ""), "from_assistant": request.GET.get("source") == "assistant"})

    def post(self, request, pk=None):
        req = self.get_req(pk)
        form = RequestForm(request.POST, department=req.department if req else request.user.department)
        if not form.is_valid():
            return self.render(request, {"form": form, "req": req})
        try:
            if req is None:
                saved = services.create_draft(request.user, form.cleaned_data)
                messages.success(request, f"{saved.reference} saved as a draft. Choose stock (or find AI matches), then submit.")
            else:
                saved = services.update_draft(request.user, req.pk, request.POST.get("version"), form.cleaned_data)
                messages.success(request, f"{saved.reference} updated.")
        except BusinessError as exc:
            return self.render(request, {"form": apply_errors(form, exc), "req": req})
        url = reverse("exchange:request", args=[saved.pk])
        prefill = V.uuid_or_none(request.POST.get("prefill_asset"))
        if prefill:
            url += f"?asset={prefill}"
        return redirect(url)


class RequestDetailView(AppView):
    template_name = "exchange/detail.html"
    nav = "requests"

    def get(self, request, pk):
        req = get_object_or_404(ResourceRequest.objects.select_related("category", "department", "requester", "receiving_custodian", "assigned_approver"), pk=pk)
        policy.require(policy.can_view_request(request.user, req), "You cannot view this request.", "view_request", req)
        services.expire_due_holds()
        req.refresh_from_db()
        editable = policy.can_edit_request(request.user, req)
        versions = req.proposal_versions.prefetch_related("approvals__reviewer").order_by("-version_no")
        holds = req.reservations.select_related("asset").order_by("-created_at")
        from apps.workflow.models import Transfer

        transfers = Transfer.objects.filter(reservation__request=req).select_related("source_asset", "received_asset").order_by("created_at")
        candidates, preselect = [], None
        if editable and req.status in (RequestStatus.DRAFT, RequestStatus.RETURNED, RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED):
            for a in services.candidate_assets(request.user, req):
                ok, why = matching.hard_filter(a, req, ignore_quantity=True)
                candidates.append({"asset": a, "free": catalog.free_listed_quantity(a), "ok": ok and catalog.free_listed_quantity(a) > 0, "why": why})
            candidates.sort(key=lambda c: (not c["ok"], c["asset"].asset_tag))
            pre_id = request.GET.get("asset")
            if pre_id:
                preselect = next((c for c in candidates if str(c["asset"].pk) == pre_id), None)
        latest_match = AIResult.objects.filter(function="rank", matches__request=req).distinct().order_by("-created_at").first()
        ctx = {
            "req": req, "editable": editable, "versions": versions, "holds": holds, "transfers": transfers, "candidates": candidates,
            "preselect": preselect, "match_id": str(V.uuid_or_none(request.GET.get("match")) or ""), "remaining": services.remaining_need(req), "latest_match": latest_match,
            "pending_version": versions.filter(status=ProposalVersion.PENDING).first(),
            "can_withdraw": editable and req.status in (RequestStatus.DRAFT, RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW, RequestStatus.RETURNED),
            "can_cancel": editable and req.status in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED),
            "steps": _progress(req),
            "can_edit": editable and req.status in (RequestStatus.DRAFT, RequestStatus.RETURNED),
            "can_choose": editable and (req.status in (RequestStatus.DRAFT, RequestStatus.RETURNED) or (req.status in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED) and services.remaining_need(req) > 0)),
            "reserve_mode": req.status in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED),
        }
        return self.render(request, ctx)


def _progress(req):
    order = ["DRAFT", "SUBMITTED", "UNDER_REVIEW", "APPROVED", "PARTIALLY_FULFILLED", "FULFILLED"]
    labels = {"DRAFT": "Draft", "SUBMITTED": "Submitted", "UNDER_REVIEW": "Under review", "APPROVED": "Approved", "PARTIALLY_FULFILLED": "Partly received", "FULFILLED": "Fulfilled"}
    if req.status in (RequestStatus.REJECTED, RequestStatus.CANCELLED, RequestStatus.RETURNED):
        return [{"label": labels[s], "state": "done" if order.index(s) <= 1 else "todo"} for s in order[:4]] + [{"label": req.status_label, "state": "stop"}]
    idx = order.index(req.status)
    return [{"label": labels[s], "state": "done" if i < idx else ("current" if i == idx else "todo")} for i, s in enumerate(order) if not (s == "PARTIALLY_FULFILLED" and req.status != s)]


class RequestActionView(AppView):
    def post(self, request, pk):
        req = get_object_or_404(ResourceRequest, pk=pk)
        action = request.POST.get("action")
        user = request.user
        try:
            if action in ("submit", "reserve"):
                form = SubmitForm(request.POST)
                if not form.is_valid():
                    raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a valid asset and quantity: " + " ".join(e for errs in form.errors.values() for e in errs))
                asset_id, qty = form.cleaned_data["asset"], form.cleaned_data["quantity"]
                if asset_id and not qty:
                    raise BusinessError(BusinessError.VALIDATION_ERROR, "Enter the quantity to reserve.")
                if action == "submit":
                    res = self.command(request, "submit_request", {"req": str(pk), "asset": str(asset_id), "qty": qty},
                                       lambda: _submit_result(*services.submit(user, pk, request.POST.get("version"), asset_id, qty)))
                else:
                    res = self.command(request, "reserve_more", {"req": str(pk), "asset": str(asset_id), "qty": qty},
                                       lambda: _submit_result(*services.reserve_additional(user, pk, request.POST.get("version"), asset_id, qty)))
                _mark_match_selected(user, form.cleaned_data.get("match"), asset_id)
                hold = f" Hold {res['hold']} keeps {res['qty']} unit(s) until {res['expires']} unless approved." if res.get("hold") else " No stock is held — the need stays open."
                messages.success(request, f"{req.reference} sent for review (version v{res['version']}).{hold}")
            elif action == "withdraw":
                services.withdraw(user, pk, request.POST.get("version"), request.POST.get("reason", ""))
                messages.success(request, f"{req.reference} withdrawn. Any pending hold was released.")
            elif action == "cancel":
                services.request_cancellation(user, pk, request.POST.get("version"), request.POST.get("reason", ""))
                messages.success(request, "Cancellation proposal sent to the approver. Nothing changes until they decide.")
            else:
                messages.error(request, "Unknown action.")
        except BusinessError as exc:
            messages.error(request, exc.message)
        return redirect("exchange:request", pk=pk)


def _submit_result(req, version, hold):
    return {"version": version.version_no, "hold": hold.reference if hold else None, "qty": hold.quantity if hold else 0,
            "expires": timezone.localtime(hold.expires_at).strftime("%d %b %H:%M") if hold and hold.expires_at else ""}


def _mark_match_selected(user, match_id, asset_id):
    if not match_id:
        return
    m = MatchRecommendation.objects.filter(pk=match_id, asset_id=asset_id, request__requester=user).first() or MatchRecommendation.objects.filter(pk=match_id, asset_id=asset_id).first()
    if m and m.status != MatchRecommendation.SELECTED:
        m.status = MatchRecommendation.SELECTED
        m.save(update_fields=["status"])
        HumanReview.objects.create(ai_result=m.ai_result, actor=user, selected_match=m, choice=HumanReview.SELECTED, reason="Selected and submitted for reservation")


class MatchView(AppView):
    """AI feature 2 — run ranking, then explicit accept (select) or reject per candidate."""

    template_name = "exchange/matches.html"
    nav = "requests"

    def _req(self, pk):
        req = get_object_or_404(ResourceRequest, pk=pk)
        policy.require(policy.can_edit_request(self.request.user, req), "Only the requester can review matches for this request.", "matches", req)
        return req

    def get(self, request, pk, result=None):
        req = self._req(pk)
        if result is None:
            latest = AIResult.objects.filter(function="rank", matches__request=req).distinct().order_by("-created_at").first()
            if latest:
                return redirect("exchange:match_result", pk=pk, result=latest.pk)
            return self.render(request, {"req": req, "result": None})
        res = get_object_or_404(AIResult, pk=result, function="rank")
        matches = res.matches.filter(request=req).select_related("asset__owner_department", "asset__category").order_by("rank")
        rows = []
        for m in matches:
            observed = m.components.get("observed", {})
            comps = [{"key": k, "label": matching.LABELS[k], "weight": int(float(matching.WEIGHTS[k]) * 100), "value": observed.get(k), "missing": k in m.components.get("missing", [])} for k in matching.WEIGHTS]
            rows.append({"m": m, "comps": comps, "free": catalog.free_listed_quantity(m.asset), "explanation": res.output["ranked"][m.rank - 1].get("explanation", "") if m.rank - 1 < len(res.output.get("ranked", [])) else ""})
        reviews = res.reviews.select_related("selected_match__asset").order_by("-reviewed_at")
        stale = req.version != (matches.first().input_version if matches else req.version)
        return self.render(request, {"req": req, "result": res, "rows": rows, "out": res.output, "reviews": reviews, "stale": stale})

    def post(self, request, pk, result=None):
        req = self._req(pk)
        action = request.POST.get("action")
        if action == "run":
            outcome, recs = matching.rank(request.user, req)
            if outcome.fallback:
                messages.warning(request, "Gemini was not used: " + outcome.message + " Ranked with the labelled local fallback; scores still use the designed weights.")
            return redirect("exchange:match_result", pk=pk, result=outcome.result.pk)
        res = get_object_or_404(AIResult, pk=result, function="rank")
        if action == "reject":
            m = get_object_or_404(MatchRecommendation, pk=V.uuid_or_404(request.POST.get("match")), ai_result=res, request=req)
            m.status = MatchRecommendation.REJECTED
            m.save(update_fields=["status"])
            HumanReview.objects.create(ai_result=res, actor=request.user, selected_match=m, choice=HumanReview.REJECTED, reason=request.POST.get("reason", "")[:500])
            messages.info(request, f"{m.asset.asset_tag} rejected. Your feedback is recorded with the AI result.")
        elif action == "reject_all":
            reason = request.POST.get("reason", "").strip()
            HumanReview.objects.create(ai_result=res, actor=request.user, choice=HumanReview.REJECTED, reason=reason[:500] or "None of the suggestions fit")
            res.matches.filter(request=req, status=MatchRecommendation.PROPOSED).update(status=MatchRecommendation.REJECTED)
            messages.info(request, "All suggestions rejected. Your request stays open — use manual search or submit the need without a reservation.")
            return redirect("exchange:request", pk=pk)
        elif action == "select":
            m = get_object_or_404(MatchRecommendation, pk=V.uuid_or_404(request.POST.get("match")), ai_result=res, request=req)
            messages.info(request, f"{m.asset.asset_tag} selected. Confirm the quantity and submit — selecting does not reserve or approve anything.")
            return redirect(reverse("exchange:request", args=[pk]) + f"?asset={m.asset_id}&match={m.pk}#choose")
        return redirect("exchange:match_result", pk=pk, result=res.pk)


class AlertsView(AppView):
    """Bonus: saved searches that notify the requester when matching stock is published."""

    template_name = "exchange/alerts.html"
    nav = "alerts"

    def ctx(self, request, form=None, **extra):
        from .models import SavedSearch

        policy.require(policy.can_create_request(request.user), "Only requesters or department representatives can set availability alerts.", "alerts")
        searches = SavedSearch.objects.filter(user=request.user).select_related("category__parent").prefetch_related("hits__asset")
        initial = {"keywords": request.GET.get("q", "")[:100], "min_quantity": 1}
        if request.GET.get("category"):
            from apps.catalog.models import Category

            initial["category"] = Category.objects.filter(pk=V.uuid_or_none(request.GET["category"])).first()
        if request.GET.get("condition") in dict(Condition.CHOICES):
            initial["min_condition"] = request.GET["condition"]
        if request.GET.get("q"):
            initial["name"] = request.GET["q"][:60].strip().capitalize()
        return {"searches": searches, "form": form or SavedSearchForm(initial=initial), "open_create": bool(request.GET) or form is not None, **extra}

    def get(self, request):
        return self.render(request, self.ctx(request))

    def post(self, request):
        if request.POST.get("action") == "delete":
            try:
                services.delete_search(request.user, request.POST.get("id"))
                messages.success(request, "Alert deleted.")
            except BusinessError as exc:
                messages.error(request, exc.message)
            return redirect("exchange:alerts")
        form = SavedSearchForm(request.POST)
        if not form.is_valid():
            return self.render(request, self.ctx(request, form))
        d = form.cleaned_data
        try:
            s, existing = services.save_search(request.user, d["name"], d["keywords"], d["category"], d["min_condition"], d["min_quantity"])
        except BusinessError as exc:
            return self.render(request, self.ctx(request, apply_errors(form, exc)))
        note = f" {len(existing)} item(s) already match — see below." if existing else " You will get a notification when matching stock is published."
        messages.success(request, f"Alert “{s.name}” saved.{note}")
        return self.render(request, self.ctx(request, existing=existing, existing_for=s))
