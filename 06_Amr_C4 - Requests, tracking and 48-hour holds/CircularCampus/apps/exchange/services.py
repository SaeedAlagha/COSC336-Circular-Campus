"""Request service (process 3.0): drafts, submission with an atomic 48-hour hold,
tracking, withdrawal / cancellation, hold expiry and fulfilment (UC-02, UC-15)."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone

from apps.accounts import policy
from apps.accounts.models import Role
from apps.catalog import services as catalog
from apps.catalog.models import AssetStatus, Condition
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.refs import next_reference
from apps.reporting import audit
from apps.workflow import services as workflow
from apps.workflow.models import Approval, ProposalVersion, TransferStatus

from .models import RequestStatus, Reservation, ReservationStatus, ResourceRequest, Urgency

REQUEST_FIELDS = ("title", "category", "purpose", "specifications", "mandatory_specs", "quantity", "preferred_condition", "urgency",
                  "destination_campus", "destination_building", "destination_room", "required_date", "allow_partial", "receiving_custodian")


def set_request_status(req, status, actor=None, reason=""):
    if status not in RequestStatus.TRANSITIONS.get(req.status, set()):
        raise BusinessError(BusinessError.INVALID_TRANSITION, f"{req.reference} cannot move from {req.status_label} to {RequestStatus.label(status)}.")
    old = req.status
    req.status = status
    req.version += 1
    req.save(update_fields=["status", "version", "updated_at"])
    audit.record(actor, f"Request {status.lower()}", "request", req, before={"status": old}, after={"status": status}, reason=reason,
                 service_actor="" if actor else "workflow")


def validate_need(data, *, today=None):
    """UC-02.1 Validate Request Data — field-level errors for every field at once, nothing saved on failure (FR-13).
    Returns the cleaned values (trimmed text, parsed numbers and dates)."""
    today = today or timezone.localdate()
    errors, out = {}, dict(data)
    checks = {
        "title": lambda v: V.text(v, "title", label="Title", required=True, min_len=3, max_len=160),
        "purpose": lambda v: V.text(v, "purpose", label="Purpose", required=True, min_len=5, max_len=V.MAX_LONG, multiline=True),
        "specifications": lambda v: V.text(v, "specifications", label="Specifications", max_len=V.MAX_LONG, multiline=True, needs_letter=False),
        "mandatory_specs": lambda v: ", ".join(V.keywords(v, "mandatory_specs", label="Mandatory specifications", max_items=10)),
        "quantity": lambda v: V.integer(v, "quantity", label="Quantity", min_value=1, max_value=V.MAX_REQUEST_QTY),
        "preferred_condition": lambda v: V.choice(v, "preferred_condition", Condition.CHOICES, label="Minimum condition"),
        "urgency": lambda v: V.choice(v, "urgency", Urgency.CHOICES, label="Urgency"),
        "required_date": lambda v: V.a_date(v, "required_date", label="Required date", required=True, not_before=today, not_after=V.years_from_today(2)),
        "destination_campus": lambda v: V.place(v, "destination_campus", label="Destination campus"),
        "destination_building": lambda v: V.place(v, "destination_building", label="Building"),
        "destination_room": lambda v: V.place(v, "destination_room", label="Room", max_len=40),
    }
    for field, check in checks.items():
        if field in data or field in ("title", "purpose", "quantity", "preferred_condition", "required_date", "destination_building", "destination_room"):
            try:
                out[field] = check(data.get(field))
            except BusinessError as exc:
                errors.update(exc.field_errors or {field: exc.message})
    if errors:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Please correct the highlighted fields.", errors)
    return out


def _check_receiver(receiver, department):
    if receiver is None or not receiver.is_active or not policy.has_role(receiver, Role.CUSTODIAN, department.pk):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The receiving custodian must be an active Asset Custodian of your department.", {"receiving_custodian": "Choose a custodian of your department."})


def create_draft(actor, data):
    policy.require(policy.can_create_request(actor), "Only requesters or department representatives can create requests.", "create_request")
    data = validate_need(data)
    _check_receiver(data.get("receiving_custodian"), actor.department)
    with transaction.atomic():
        req = ResourceRequest(reference=next_reference("R"), requester=actor, department=actor.department, status=RequestStatus.DRAFT)
        for f in REQUEST_FIELDS:
            if f in data:
                setattr(req, f, data[f])
        req.full_clean(exclude=["reference"])
        req.save()
        audit.record(actor, "Request draft created", "request", req, after={f: data.get(f) for f in ("title", "quantity", "required_date")})
        return req


def update_draft(actor, request_id, expected_version, data):
    """FR-48: drafts and returned requests are editable; submitted ones are not."""
    with transaction.atomic():
        req = ResourceRequest.objects.select_for_update().get(pk=request_id)
        policy.require(policy.can_edit_request(actor, req), "You can only edit your own or your department's requests.", "update_request", req)
        if req.status not in (RequestStatus.DRAFT, RequestStatus.RETURNED):
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Only drafts and returned requests can be edited. Material changes after submission need a new review.")
        if req.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This request changed after you opened it. Reload and review it.")
        data = validate_need(data)
        _check_receiver(data.get("receiving_custodian"), req.department)
        before = {f: getattr(req, f) for f in REQUEST_FIELDS}
        for f in REQUEST_FIELDS:
            if f in data:
                setattr(req, f, data[f])
        req.version += 1
        req.full_clean()
        req.save()
        audit.record(actor, "Request edited", "request", req, before=before, after={f: getattr(req, f) for f in REQUEST_FIELDS})
        return req


# ------------------------------------------------------------------ holds ---
def expire_due_holds(asset=None):
    """BR-08/FR-50: release unapproved holds once their hold period ends. Allocations never expire."""
    now = timezone.now()
    qs = Reservation.objects.select_for_update(of=("self",)).filter(status=ReservationStatus.PENDING, expires_at__lte=now).select_related("request", "asset")
    if asset is not None:
        qs = qs.filter(asset=asset)
    count = 0
    with transaction.atomic():
        for hold in qs:
            release_hold(hold, ReservationStatus.EXPIRED, None, f"Hold period of {settings.CAMPUS['HOLD_HOURS']} hours elapsed before approval")
            audit.notify([hold.request.requester], "HOLD_EXPIRED", f"Hold {hold.reference} expired",
                         f"The {hold.quantity}-unit hold on {hold.asset.asset_tag} expired before approval. Your request {hold.request.reference} stays open.",
                         hold.request, reverse("exchange:request", args=[hold.request_id]))
            count += 1
    return count


def release_hold(hold, status, actor, reason):
    """Release a pending hold exactly once (Rejected / Cancelled / Expired)."""
    if status not in ReservationStatus.TRANSITIONS[hold.status]:
        raise BusinessError(BusinessError.INVALID_TRANSITION, f"Hold {hold.reference} is already {hold.status_label}.")
    hold.status = status
    hold.released_at = timezone.now()
    hold.release_reason = reason
    if status != ReservationStatus.ALLOCATED:
        hold.remaining_quantity = 0
    hold.save()
    asset = catalog.lock_asset(hold.asset_id)
    catalog.sync_listing_status(asset, actor, f"Hold {hold.reference} {status.lower()}")
    catalog.log_event(asset, actor, "HOLD_RELEASED", quantity=hold.quantity, after={"hold": hold.reference, "status": status}, reason=reason)
    audit.record(actor, f"Hold {status.lower()}", "reservation", hold, after={"quantity": hold.quantity}, reason=reason, service_actor="hold-expiry-job")


def remaining_need(req):
    committed = sum(r.remaining_quantity if r.status == ReservationStatus.ALLOCATED else r.quantity for r in req.reservations.filter(status__in=ReservationStatus.ACTIVE))
    return req.quantity - req.received_quantity - committed


def _create_hold(actor, req, asset_id, quantity):
    """Lock the asset, recheck eligibility and free stock, then create the hold (FR-14, FR-49)."""
    from apps.intelligence.matching import hard_filter

    asset = catalog.lock_asset(asset_id)
    expire_due_holds(asset)
    if not policy.can_view_asset(actor, asset):
        policy.require(False, "That asset is not visible to you.", "reserve", asset)
    if asset.owner_department_id == req.department_id:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "This asset already belongs to your department; no transfer is needed.")
    quantity = V.integer(quantity, "quantity", label="Quantity", min_value=1, max_value=V.MAX_REQUEST_QTY)
    ok, reasons = hard_filter(asset, req, ignore_quantity=True)
    if not ok:
        raise BusinessError(BusinessError.UNSAFE_STOCK if asset.is_restricted else BusinessError.VALIDATION_ERROR, "This asset is not eligible: " + "; ".join(reasons))
    free = catalog.free_listed_quantity(asset)
    need = remaining_need(req)
    if quantity > need:
        raise BusinessError(BusinessError.STOCK_CONFLICT, f"Only {need} unit(s) of this request remain to be covered.", {"quantity": f"At most {need}."})
    if quantity > free:
        raise BusinessError(BusinessError.STOCK_CONFLICT, f"Only {free} unit(s) are free now — another request may have just reserved stock. Refresh and choose again.", {"quantity": f"At most {free} free."})
    if quantity < need and not req.allow_partial:
        raise BusinessError(BusinessError.VALIDATION_ERROR, f"This request needs {need} unit(s) and does not allow partial fulfilment. Tick 'allow partial fulfilment' on the request or choose a batch with enough stock.", {"quantity": f"Reserve {need}."})
    hold = Reservation.objects.create(
        reference=next_reference("H"), request=req, asset=asset, quantity=quantity, remaining_quantity=0, created_by=actor,
        expires_at=timezone.now() + timedelta(hours=settings.CAMPUS["HOLD_HOURS"]),
    )
    # Compare-and-swap on the asset row version: a concurrent writer forces a retry.
    asset.version += 1
    asset.save(update_fields=["version", "updated_at"])
    catalog.sync_listing_status(asset, actor, f"Hold {hold.reference} for {req.reference}")
    catalog.log_event(asset, actor, "RESERVED", quantity=quantity, after={"hold": hold.reference, "request": req.reference, "expires_at": hold.expires_at})
    audit.record(actor, "Hold created", "reservation", hold, after={"asset": asset.asset_tag, "quantity": quantity, "expires_at": hold.expires_at})
    return hold


def _request_snapshot(req, holds):
    return {
        # [UML: Composition — the owned RequestSpecification is frozen into the snapshot (Figure 19)]
        "request": {
            "reference": req.reference, "title": req.title, **req.specification.as_dict(),
            "destination_campus": req.destination_campus, "destination_building": req.destination_building,
            "destination_room": req.destination_room, "receiving_custodian": str(req.receiving_custodian_id),
        },
        "holds": [{"reservation_id": str(h.pk), "asset_id": str(h.asset_id), "asset_tag": h.asset.asset_tag, "quantity": h.quantity,
                   "estimated_transfer_cost_aed": h.asset.estimated_transfer_cost_aed} for h in holds],
    }


def _stages_for(req, holds, approver):
    stages = []
    if holds:
        src = holds[0].asset.owner_department
        stages.append((Approval.RELEASE_CHECK, src, None, True))
        cost = holds[0].asset.estimated_transfer_cost_aed
        stages.append((Approval.COST_REVIEW, req.department, None, cost is not None and cost > 0))
    else:
        stages.append((Approval.RELEASE_CHECK, req.department, None, False))
        stages.append((Approval.COST_REVIEW, req.department, None, False))
    stages.append((Approval.AUTHORIZE, req.department, approver, True))
    return stages


def submit(actor, request_id, expected_version, asset_id=None, quantity=None):
    """UC-02 steps 3–5: validate, then create the request version and a pending hold atomically."""
    with transaction.atomic():
        req = ResourceRequest.objects.select_for_update(of=("self",)).select_related("department").get(pk=request_id)
        policy.require(policy.can_edit_request(actor, req), "You can only submit your own or your department's requests.", "submit", req)
        if req.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This request changed after you opened it. Reload and review it.")
        if req.status not in (RequestStatus.DRAFT, RequestStatus.RETURNED):
            raise BusinessError(BusinessError.INVALID_TRANSITION, f"{req.reference} is {req.status_label}; it cannot be submitted again.")
        validate_need({f: getattr(req, f) for f in REQUEST_FIELDS})
        approver = workflow.assigned_approver(req.department, exclude_user=actor)
        if approver is None:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Your department has no assigned approver other than you. Ask the system administrator to assign one.")
        # A returned request: release holds from the returned version before re-proposing.
        for old in req.reservations.filter(status=ReservationStatus.PENDING):
            if not asset_id or str(old.asset_id) != str(asset_id) or (V.integer(quantity, "quantity", required=False, max_value=V.MAX_REQUEST_QTY) or 0) != old.quantity:
                release_hold(old, ReservationStatus.CANCELLED, actor, "Superseded by corrected submission")
        holds = list(req.reservations.filter(status=ReservationStatus.PENDING).select_related("asset"))
        if asset_id and not holds:
            holds = [_create_hold(actor, req, asset_id, quantity)]
        req.assigned_approver = approver
        req.submitted_at = timezone.now()
        req.save(update_fields=["assigned_approver", "submitted_at"])
        set_request_status(req, RequestStatus.SUBMITTED, actor, "Submitted for review")
        version = workflow.create_version(kind=ProposalVersion.REQUEST, target_field="request", target=req, proposer=actor,
                                          snapshot=_request_snapshot(req, holds), stages=_stages_for(req, holds, approver))
        workflow.notify_stage(version)
        audit.notify([actor], "REQUEST_SUBMITTED", f"{req.reference} submitted", f"Version {version.label} is with reviewers." + (f" Hold {holds[0].reference} expires {timezone.localtime(holds[0].expires_at):%d %b %H:%M}." if holds else ""), req, reverse("exchange:request", args=[req.pk]))
        return req, version, (holds[0] if holds else None)


def reserve_additional(actor, request_id, expected_version, asset_id, quantity):
    """UC-02 alternative flow: reserve eligible stock against an approved request's remaining need.
    Each additional hold gets its own authorisation (separate approved transfer, BR-15)."""
    with transaction.atomic():
        req = ResourceRequest.objects.select_for_update().get(pk=request_id)
        policy.require(policy.can_edit_request(actor, req), "You can only reserve for your own or your department's requests.", "reserve", req)
        if req.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This request changed after you opened it. Reload and review it.")
        if req.status not in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED):
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Additional holds can be added only to an approved, open request.")
        pending = ProposalVersion.objects.filter(request=req, status=ProposalVersion.PENDING).exists()
        if pending:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "A proposal for this request is already under review.")
        hold = _create_hold(actor, req, asset_id, quantity)
        approver = req.assigned_approver if req.assigned_approver_id and req.assigned_approver_id != actor.pk else workflow.assigned_approver(req.department, exclude_user=actor)
        version = workflow.create_version(kind=ProposalVersion.REQUEST, target_field="request", target=req, proposer=actor,
                                          snapshot=_request_snapshot(req, [hold]), stages=_stages_for(req, [hold], approver))
        req.version += 1
        req.save(update_fields=["version"])
        workflow.notify_stage(version)
        return req, version, hold


def withdraw(actor, request_id, expected_version, reason):
    """FR-48: requester withdraws an unapproved request; its pending holds are released once."""
    with transaction.atomic():
        req = ResourceRequest.objects.select_for_update().get(pk=request_id)
        policy.require(policy.can_edit_request(actor, req), "You can only withdraw your own or your department's requests.", "withdraw", req)
        if req.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This request changed after you opened it. Reload and review it.")
        if req.status not in (RequestStatus.DRAFT, RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW, RequestStatus.RETURNED):
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Approved requests need a cancellation decision from the approver. Use 'Request cancellation'.")
        reason = V.reason(reason, required=False) or "Withdrawn by requester"
        for hold in req.reservations.filter(status=ReservationStatus.PENDING):
            release_hold(hold, ReservationStatus.CANCELLED, actor, f"Withdrawn: {reason}")
        ProposalVersion.objects.filter(request=req, status=ProposalVersion.PENDING).update(status=ProposalVersion.WITHDRAWN, decided_at=timezone.now())
        req.closed_reason = reason
        req.save(update_fields=["closed_reason"])
        set_request_status(req, RequestStatus.CANCELLED, actor, reason or "Withdrawn by requester")
        return req


def request_cancellation(actor, request_id, expected_version, reason):
    """UC-15 step 4: an approved request sends a cancellation proposal to its approver."""
    with transaction.atomic():
        req = ResourceRequest.objects.select_for_update().get(pk=request_id)
        policy.require(policy.can_edit_request(actor, req), "You can only cancel your own or your department's requests.", "request_cancellation", req)
        if req.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This request changed after you opened it. Reload and review it.")
        if req.status not in (RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED):
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Only approved, open requests use cancellation proposals.")
        reason = V.reason(reason, label="Cancellation reason")
        from apps.workflow.models import Transfer

        in_flight = Transfer.objects.filter(reservation__request=req, status__in=[TransferStatus.RELEASED, TransferStatus.EXCEPTION])
        if in_flight.exists():
            raise BusinessError(BusinessError.INVALID_TRANSITION, f"Transfer {in_flight.first().reference} has already been released; complete receipt or exception resolution first. Only unreleased quantities can be cancelled.")
        if ProposalVersion.objects.filter(request=req, status=ProposalVersion.PENDING).exists():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Another proposal for this request is still under review.")
        approver = req.assigned_approver if req.assigned_approver_id and req.assigned_approver_id != actor.pk else workflow.assigned_approver(req.department, exclude_user=actor)
        snapshot = {"cancel": req.reference, "reason": reason.strip(), "received_quantity": req.received_quantity,
                    "unreleased": [str(t.pk) for t in Transfer.objects.filter(reservation__request=req, status__in=[TransferStatus.APPROVED, TransferStatus.SCHEDULED])]}
        version = workflow.create_version(kind=ProposalVersion.CANCELLATION, target_field="request", target=req, proposer=actor, snapshot=snapshot,
                                          stages=[(Approval.RELEASE_CHECK, req.department, None, False), (Approval.COST_REVIEW, req.department, None, False),
                                                  (Approval.AUTHORIZE, req.department, approver, True)])
        workflow.notify_stage(version)
        audit.record(actor, "Cancellation requested", "request", req, reason=reason)
        return version


def apply_approved_cancellation(actor, req, version):
    """Cancel unreleased allocations and pending holds; receipts already confirmed are retained."""
    from apps.workflow.models import Transfer

    req = ResourceRequest.objects.select_for_update().get(pk=req.pk)
    for t in Transfer.objects.select_for_update().filter(reservation__request=req, status__in=[TransferStatus.APPROVED, TransferStatus.SCHEDULED]):
        t.status = TransferStatus.CANCELLED
        t.version += 1
        t.resolution = "Cancelled before release by approved cancellation"
        t.save()
        hold = Reservation.objects.select_for_update().get(pk=t.reservation_id)
        hold.remaining_quantity = max(0, hold.remaining_quantity - t.quantity)
        if hold.remaining_quantity == 0:
            hold.status = ReservationStatus.CANCELLED
            hold.released_at = timezone.now()
            hold.release_reason = "Approved cancellation before release"
        hold.save()
        asset = catalog.lock_asset(t.source_asset_id)
        catalog.sync_listing_status(asset, actor, f"Cancellation of {req.reference}")
        catalog.log_event(asset, actor, "HOLD_RELEASED", quantity=t.quantity, after={"transfer": t.reference, "cancelled": True}, transfer=t)
    for hold in req.reservations.filter(status=ReservationStatus.PENDING):
        release_hold(hold, ReservationStatus.CANCELLED, actor, "Approved cancellation")
    req.closed_reason = version.snapshot.get("reason", "")
    req.save(update_fields=["closed_reason"])
    set_request_status(req, RequestStatus.CANCELLED, actor, "Approved cancellation; confirmed receipts retained")
    audit.notify([req.requester], "REQUEST_CANCELLED", f"{req.reference} cancelled", f"Cancellation approved by {actor.full_name}. {req.received_quantity} received unit(s) are retained.", req, reverse("exchange:request", args=[req.pk]))


def record_receipt(req, quantity, actor, transfer):
    """BR-15: fulfilled only when confirmed received quantity meets the approved need."""
    req = ResourceRequest.objects.select_for_update().get(pk=req.pk)
    req.received_quantity += quantity
    req.save(update_fields=["received_quantity"])
    target = RequestStatus.FULFILLED if req.received_quantity >= req.quantity else RequestStatus.PARTIALLY_FULFILLED
    if target != req.status:
        set_request_status(req, target, actor, f"Receipt {transfer.reference}")


def after_allocation_change(req, actor):
    """Hook after an allocation is released; the need stays open (no automatic close)."""
    return req


def free_for(asset):
    return catalog.free_listed_quantity(asset)


def candidate_assets(actor, req):
    """Eligible published stock outside the requester's department (manual selection path)."""
    from apps.catalog.models import Asset

    qs = Asset.objects.filter(is_published=True, status__in=AssetStatus.LISTED).exclude(owner_department=req.department).select_related("category", "owner_department")
    return [a for a in qs if policy.can_view_asset(actor, a)]
