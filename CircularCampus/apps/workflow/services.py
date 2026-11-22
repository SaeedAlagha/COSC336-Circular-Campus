"""Workflow component (process 4.0): immutable proposal versions, staged human
approval (design 4.5.1), and transfer release / receipt / exception handling.

Invariants enforced here (all server-side):
* BR-02  no self-approval; submission, approval, release and receipt are separate events.
* FR-51  decisions bind one immutable ProposalVersion; stale versions are rejected.
* BR-13  custody, department and location change only on matching confirmed receipt.
* FR-49  the source asset row is locked before quantities are re-checked.
* NFR-03 business change + life-cycle event + audit + notices commit together.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone

from apps.accounts import policy
from apps.accounts.models import Role, User
from apps.catalog import services as catalog
from apps.catalog.models import AssetStatus, Condition
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.refs import next_reference
from apps.reporting import audit

from .models import Approval, ProposalVersion, Transfer, TransferStatus


# ======================================================== reviewer selection ===
def assigned_approver(department, exclude_user=None):
    """The department's designated approver, else the first approver with scope (never the proposer)."""
    candidates = []
    if department.default_approver_id:
        candidates.append(department.default_approver)
    candidates += list(User.objects.filter(is_active=True, role_grants__active=True, role_grants__role__code=Role.APPROVER).distinct().order_by("campus_id"))
    for user in candidates:
        if exclude_user is not None and user.pk == exclude_user.pk:
            continue
        if user.is_active and policy.has_role(user, Role.APPROVER, department.pk):
            return user
    return None


def _clear_role_cache(user):
    if hasattr(user, "_role_cache"):
        del user._role_cache


# ===================================================== proposal versions ===
def latest_version(target_field, target):
    return ProposalVersion.objects.filter(**{target_field: target}).order_by("-version_no").first()


def create_version(*, kind, target_field, target, proposer, snapshot, stages):
    """Create the next immutable version and its ordered stage records.

    ``stages`` is a list of (stage, scope_department, reviewer_or_None, applicable: bool).
    Earlier pending versions of the same target are superseded (BR-11).
    """
    prev = latest_version(target_field, target)
    if prev and prev.status == ProposalVersion.PENDING:
        prev.status = ProposalVersion.SUPERSEDED
        prev.save(update_fields=["status"])
    version = ProposalVersion.objects.create(
        kind=kind, version_no=(prev.version_no + 1) if prev else 1, snapshot=snapshot,
        snapshot_hash=ProposalVersion.hash_snapshot(snapshot), proposer=proposer, **{target_field: target},
    )
    now = timezone.now()
    for stage, scope, reviewer, applicable in stages:
        Approval.objects.create(
            version=version, stage=stage, stage_order=Approval.STAGE_ORDER[stage], scope_department=scope, reviewer=reviewer,
            decision=None if applicable else Approval.NOT_APPLICABLE, decided_at=None if applicable else now,
            reason="" if applicable else "Stage not applicable to this proposal (explicit not-applicable record).",
        )
    return version


def pending_stage(version):
    return version.approvals.filter(decision__isnull=True).order_by("stage_order").first()


def stage_reviewers(approval):
    """Users who may currently act on a pending stage (for notifications/queues)."""
    if approval.stage == Approval.AUTHORIZE:
        return [approval.reviewer] if approval.reviewer else []
    role = Role.FINANCE if approval.stage == Approval.COST_REVIEW else Role.CUSTODIAN
    users = User.objects.filter(is_active=True, role_grants__active=True, role_grants__role__code=role).distinct()
    return [u for u in users if policy.has_role(u, role, approval.scope_department_id) and u.pk != approval.version.proposer_id]


def notify_stage(version):
    stage = pending_stage(version)
    if not stage:
        return
    target = version.target
    audit.notify(stage_reviewers(stage), "APPROVAL_NEEDED", f"Review needed: {target.reference}",
                 f"{stage.get_stage_display()} — {version.get_kind_display()} {version.label}", target, reverse("workflow:approval", args=[stage.pk]))


# ============================================================ decisions ===
def decide(actor, approval_id, version_id, decision, reason=""):
    """UC-03 Review Approval. Raises a BusinessError for stale, self, out-of-order or unauthorised decisions."""
    if decision not in (Approval.APPROVED, Approval.REJECTED, Approval.RETURNED):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose approve, reject or return.")
    approval_id = V.parse_uuid(approval_id, "approval")
    if V.uuid_or_none(version_id) is None:
        raise BusinessError(BusinessError.STALE_VERSION, "This decision form is out of date or was altered. Reload the current version.")
    reason = V.reason(reason, required=decision != Approval.APPROVED, label="Reason")
    with transaction.atomic():
        try:
            approval = Approval.objects.select_for_update(of=("self",)).select_related("version").get(pk=approval_id)
        except Approval.DoesNotExist:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "That approval no longer exists.")
        version = ProposalVersion.objects.select_for_update().get(pk=approval.version_id)
        if str(version.pk) != str(version_id):
            raise BusinessError(BusinessError.STALE_VERSION, "This decision refers to a different proposal version. Reload the current version.")
        target_field = "request" if version.request_id else "maintenance" if version.maintenance_id else "disposition"
        current = latest_version(target_field, version.target)
        if version.status != ProposalVersion.PENDING or current.pk != version.pk:
            raise BusinessError(BusinessError.STALE_VERSION, f"Version {version.label} is no longer current ({version.get_status_display()}). Reload to review the current version.")
        if approval.decision is not None:
            raise BusinessError(BusinessError.STALE_VERSION, "This stage has already been decided.")
        if actor.pk == version.proposer_id:
            raise BusinessError(BusinessError.SELF_APPROVAL, "You proposed this; another authorised reviewer must decide it.")
        earlier = version.approvals.filter(stage_order__lt=approval.stage_order, decision__isnull=True)
        if earlier.exists():
            raise BusinessError(BusinessError.MISSING_REVIEW, f"{earlier.first().get_stage_display()} must be completed first.")
        if not policy.can_decide_stage(actor, approval, version.proposer_id):
            policy.require(False, "You are not the reviewer for this stage.", "decide", approval)
        if decision in (Approval.REJECTED, Approval.RETURNED) and len(reason) < 5:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "A reason is required to reject or return a proposal.", {"reason": "Explain the decision (at least 5 characters)."})

        approval.decision = decision
        approval.reason = reason.strip()
        approval.decided_at = timezone.now()
        if approval.stage != Approval.AUTHORIZE:
            approval.reviewer = actor  # pooled stage: store the deciding reviewer
        approval.save()
        audit.record(actor, f"Stage {approval.stage} {decision.lower()}", "approval", approval,
                     after={"version": version.label, "kind": version.kind, "target": getattr(version.target, "reference", "")}, reason=reason)

        handler = EFFECTS[version.kind]
        if decision == Approval.APPROVED:
            if pending_stage(version) is None:
                handler.check_before_approve(actor, version)
                version.status = ProposalVersion.APPROVED
                version.decided_at = timezone.now()
                version.save(update_fields=["status", "decided_at"])
                handler.approved(actor, version, approval)
            else:
                handler.stage_progress(actor, version)
                notify_stage(version)
        else:
            version.status = ProposalVersion.REJECTED if decision == Approval.REJECTED else ProposalVersion.RETURNED
            version.decided_at = timezone.now()
            version.save(update_fields=["status", "decided_at"])
            if decision == Approval.REJECTED:
                handler.rejected(actor, version, reason)
            else:
                handler.returned(actor, version, reason)
        return approval


# [UML: Inheritance — abstract base class; subclasses override approved/rejected/returned (polymorphism)]
class Effects:
    def check_before_approve(self, actor, version):
        pass

    def stage_progress(self, actor, version):
        pass

    def approved(self, actor, version, approval):
        raise NotImplementedError

    def rejected(self, actor, version, reason):
        raise NotImplementedError

    def returned(self, actor, version, reason):
        raise NotImplementedError


# ============================================== request / reservation effects ===
# [UML: Inheritance — RequestEffects is-a Effects]
class RequestEffects(Effects):
    def _holds(self, version):
        from apps.exchange.models import Reservation

        ids = [h["reservation_id"] for h in version.snapshot.get("holds", [])]
        return list(Reservation.objects.select_for_update(of=("self",)).filter(pk__in=ids).select_related("asset"))

    def stage_progress(self, actor, version):
        from apps.exchange import services as exchange
        from apps.exchange.models import RequestStatus

        req = version.request
        if req.status == RequestStatus.SUBMITTED:
            exchange.set_request_status(req, RequestStatus.UNDER_REVIEW, actor, "Review started")

    def check_before_approve(self, actor, version):
        from apps.exchange import services as exchange
        from apps.exchange.models import ReservationStatus

        exchange.expire_due_holds()
        for hold in self._holds(version):
            catalog.lock_asset(hold.asset_id)
            hold.refresh_from_db()
            if hold.status != ReservationStatus.PENDING:
                raise BusinessError(BusinessError.STALE_VERSION,
                                    f"Hold {hold.reference} in this version is {hold.status_label.lower()}. Return the request so the requester can reserve again, or reject it.")
            if hold.asset.is_restricted or hold.flagged_for_review:
                raise BusinessError(BusinessError.UNSAFE_STOCK, f"{hold.asset.asset_tag} is restricted for safety review; it cannot be allocated until it is verified safe.")

    def approved(self, actor, version, approval):
        from apps.exchange import services as exchange
        from apps.exchange.models import RequestStatus, ReservationStatus

        req = version.request
        for hold in self._holds(version):
            hold.status = ReservationStatus.ALLOCATED
            hold.remaining_quantity = hold.quantity
            hold.expires_at = None  # allocations never expire automatically (BR-08)
            hold.decided_at = timezone.now()
            hold.save()
            asset = hold.asset
            transfer = Transfer.objects.create(
                reference=next_reference("T"), reservation=hold, approval=approval, source_asset=asset, quantity=hold.quantity,
                releasing_user=asset.custodian, receiving_user=req.receiving_custodian,
                destination_campus=req.destination_campus, destination_building=req.destination_building, destination_room=req.destination_room,
            )
            catalog.sync_listing_status(asset, actor, f"Allocated to {req.reference}")
            catalog.log_event(asset, actor, "ALLOCATED", quantity=hold.quantity, after={"request": req.reference, "transfer": transfer.reference}, transfer=transfer)
            audit.notify([asset.custodian, req.receiving_custodian], "TRANSFER_APPROVED", f"Transfer {transfer.reference} approved",
                         f"{hold.quantity} × {asset.name} for {req.reference}. Record the physical release, then the receiver confirms receipt.",
                         transfer, reverse("workflow:transfer", args=[transfer.pk]))
        if req.status in (RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW):
            exchange.set_request_status(req, RequestStatus.APPROVED, actor, f"Approved version {version.label}")
        audit.notify([req.requester], "REQUEST_APPROVED", f"{req.reference} approved", f"Version {version.label} was approved by {actor.full_name}.", req, reverse("exchange:request", args=[req.pk]))

    def rejected(self, actor, version, reason):
        from apps.exchange import services as exchange
        from apps.exchange.models import RequestStatus, ReservationStatus

        req = version.request
        for hold in self._holds(version):
            if hold.status == ReservationStatus.PENDING:
                exchange.release_hold(hold, ReservationStatus.REJECTED, actor, f"Rejected: {reason}")
        if req.status in (RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW):
            req.closed_reason = reason
            req.save(update_fields=["closed_reason"])
            exchange.set_request_status(req, RequestStatus.REJECTED, actor, reason)
        audit.notify([req.requester], "REQUEST_REJECTED", f"{req.reference} rejected", reason, req, reverse("exchange:request", args=[req.pk]))

    def returned(self, actor, version, reason):
        from apps.exchange import services as exchange
        from apps.exchange.models import RequestStatus, ReservationStatus

        req = version.request
        if req.status in (RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW):
            exchange.set_request_status(req, RequestStatus.RETURNED, actor, reason)
        else:
            # An additional-hold proposal on an approved request: release the hold so it can be re-proposed.
            for hold in self._holds(version):
                if hold.status == ReservationStatus.PENDING:
                    exchange.release_hold(hold, ReservationStatus.CANCELLED, actor, f"Returned for correction: {reason}")
        audit.notify([req.requester], "REQUEST_RETURNED", f"{req.reference} returned for clarification", reason, req, reverse("exchange:request", args=[req.pk]))


# [UML: Inheritance — CancellationEffects is-a Effects]
class CancellationEffects(Effects):
    def approved(self, actor, version, approval):
        from apps.exchange import services as exchange

        exchange.apply_approved_cancellation(actor, version.request, version)

    def rejected(self, actor, version, reason):
        req = version.request
        audit.notify([req.requester], "CANCELLATION_REJECTED", f"Cancellation of {req.reference} declined", reason, req, reverse("exchange:request", args=[req.pk]))

    returned = rejected


# [UML: Inheritance — RepairEffects is-a Effects]
class RepairEffects(Effects):
    def approved(self, actor, version, approval):
        from apps.maintenance import services as maint

        maint.repair_authorised(actor, version.maintenance, version)

    def rejected(self, actor, version, reason):
        from apps.maintenance import services as maint

        maint.repair_declined(actor, version.maintenance, reason)

    returned = rejected


# [UML: Inheritance — DispositionEffects is-a Effects]
class DispositionEffects(Effects):
    def check_before_approve(self, actor, version):
        disp = version.disposition
        if disp.asset.reservations.filter(status__in=["PENDING_APPROVAL", "ALLOCATED"]).exists():
            raise BusinessError(BusinessError.STOCK_CONFLICT, "Unresolved holds or allocations exist for this asset; resolve them before authorising disposition.")

    def approved(self, actor, version, approval):
        from apps.maintenance import services as maint

        maint.disposition_authorised(actor, version.disposition, version)

    def rejected(self, actor, version, reason):
        from apps.maintenance import services as maint

        maint.disposition_declined(actor, version.disposition, reason, returned=False)

    def returned(self, actor, version, reason):
        from apps.maintenance import services as maint

        maint.disposition_declined(actor, version.disposition, reason, returned=True)


# [UML: Polymorphism — decide() calls EFFECTS[kind].approved(...); the right subclass runs]
EFFECTS = {
    ProposalVersion.REQUEST: RequestEffects(),
    ProposalVersion.CANCELLATION: CancellationEffects(),
    ProposalVersion.REPAIR: RepairEffects(),
    ProposalVersion.DISPOSITION: DispositionEffects(),
}


# ================================================================ transfers ===
def _lock_transfer(transfer_id):
    transfer_id = V.parse_uuid(transfer_id, "transfer")
    try:
        return Transfer.objects.select_for_update(of=("self",)).select_related("reservation__request", "source_asset", "approval__version").get(pk=transfer_id)
    except Transfer.DoesNotExist:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That transfer does not exist.")


def _move(transfer, new_status):
    if new_status not in TransferStatus.TRANSITIONS[transfer.status]:
        raise BusinessError(BusinessError.INVALID_TRANSITION, f"Transfer {transfer.reference} is {transfer.status_label}; that step is not allowed now.")
    transfer.status = new_status
    transfer.version += 1


def _check_authorisation_current(transfer):
    """BR-11: release only against the approved, current, unchanged version."""
    version = transfer.approval.version if transfer.approval_id else None
    req = transfer.reservation.request
    if version is None or version.status != ProposalVersion.APPROVED:
        raise BusinessError(BusinessError.STALE_VERSION, "This transfer no longer has a current approval.")
    holds = {(h["reservation_id"], h["asset_id"], h["quantity"]) for h in version.snapshot.get("holds", [])}
    if (str(transfer.reservation_id), str(transfer.reservation.asset_id), transfer.reservation.quantity) not in holds:
        raise BusinessError(BusinessError.STALE_VERSION, "The asset or quantity differs from the approved version; a new approval is required.")
    dest = version.snapshot.get("request", {})
    if (dest.get("destination_building"), dest.get("destination_room")) != (req.destination_building, req.destination_room):
        raise BusinessError(BusinessError.STALE_VERSION, "The destination changed after approval; a new approval is required.")


def schedule(actor, transfer_id, expected_version, method, scheduled_for, arrangements):
    with transaction.atomic():
        t = _lock_transfer(transfer_id)
        policy.require(actor.pk in (t.releasing_user_id, t.receiving_user_id), "Only the releasing or receiving custodian can arrange this handover.", "schedule", t)
        if t.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This transfer changed after you opened it. Reload and try again.")
        if method not in dict(Transfer.METHODS):
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose collection or delivery.", {"method": "Choose a method."})
        scheduled_for = V.a_date(scheduled_for, "scheduled_for", label="Handover date", not_after=V.years_from_today(1))
        if scheduled_for and scheduled_for < timezone.localdate():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "The handover date cannot be in the past.", {"scheduled_for": "Choose today or a later date."})
        if t.reservation.request.required_date and scheduled_for and scheduled_for > t.reservation.request.required_date + timedelta(days=90):
            raise BusinessError(BusinessError.VALIDATION_ERROR, "The handover date is far after the date the items are needed.", {"scheduled_for": "Choose a date within 90 days of the required date."})
        arrangements = V.text(arrangements, "arrangements", label="Arrangements", max_len=V.MAX_SHORT * 2, multiline=True)
        _move(t, TransferStatus.SCHEDULED)
        t.method, t.scheduled_for, t.arrangements = method, scheduled_for, arrangements.strip()
        t.save()
        catalog.log_event(t.source_asset, actor, "TRANSFER_SCHEDULED", quantity=t.quantity, after={"method": method, "date": scheduled_for}, transfer=t)
        audit.record(actor, "Handover scheduled", "transfer", t, after={"method": method, "scheduled_for": scheduled_for})
        other = t.receiving_user if actor.pk == t.releasing_user_id else t.releasing_user
        audit.notify([other], "TRANSFER_SCHEDULED", f"Handover arranged for {t.reference}", f"{t.get_method_display()} on {scheduled_for or 'date to confirm'}.", t, reverse("workflow:transfer", args=[t.pk]))
        return t


def release(actor, transfer_id, expected_version, quantity, evidence, transfer_cost=None):
    """Stage 5: the releasing custodian records the actual handover. Custody does NOT change yet."""
    with transaction.atomic():
        t = _lock_transfer(transfer_id)
        asset = catalog.lock_asset(t.source_asset_id)
        policy.require(actor.pk == t.releasing_user_id, "Only the assigned releasing custodian can record the release.", "release", t)
        if t.status == TransferStatus.RELEASED:
            return t  # repeated confirmation has no further effect
        if t.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This transfer changed after you opened it. Reload and try again.")
        _check_authorisation_current(t)
        if asset.is_restricted or t.reservation.flagged_for_review:
            raise BusinessError(BusinessError.UNSAFE_STOCK, "This stock is restricted for a safety review and cannot be released (NFR-05).")
        if V.integer(quantity, "quantity", label="Released quantity", min_value=1, max_value=V.MAX_ASSET_QTY) != t.quantity:
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"Release exactly the approved quantity ({t.quantity}). A different quantity needs a new approval.", {"quantity": f"Approved quantity is {t.quantity}."})
        if len((evidence or "").strip()) < 8:
            raise BusinessError(BusinessError.MISSING_EVIDENCE, "Record release evidence (who collected, vehicle, note or reference).", {"evidence": "At least 8 characters."})
        evidence = V.evidence_ref(evidence, "evidence", label="Release evidence")
        transfer_cost = V.money(transfer_cost, "transfer_cost", label="Transfer cost")
        _move(t, TransferStatus.RELEASED)
        t.released_at, t.released_quantity, t.release_evidence = timezone.now(), t.quantity, evidence.strip()
        t.transfer_cost_aed = transfer_cost
        t.save()
        catalog.log_event(asset, actor, "RELEASED", quantity=t.quantity, after={"transfer": t.reference, "custody_unchanged_until_receipt": True}, transfer=t)
        audit.record(actor, "Transfer released", "transfer", t, after={"quantity": t.quantity, "evidence": evidence[:200]})
        audit.notify([t.receiving_user], "TRANSFER_RELEASED", f"{t.reference} released — confirm receipt", f"{t.quantity} × {asset.name} is on its way. Confirm the quantity and condition when it arrives.", t, reverse("workflow:transfer", args=[t.pk]))
        return t


def match_scanned_code(code, asset):
    """Bonus QR check: accept a label URL (…/q/A-101/) or the printed code; it must name this batch."""
    raw = V.text(code, "scanned_code", label="Scanned code", max_len=300, needs_letter=False)
    if not raw:
        return False
    tag = raw.rstrip("/").rsplit("/", 1)[-1].strip().upper()
    if tag != asset.asset_tag.upper():
        raise BusinessError(BusinessError.VALIDATION_ERROR, f"The scanned label is {tag[:30]}, but this transfer is for {asset.asset_tag}. Check you have the right item.",
                            {"scanned_code": "Wrong item scanned."})
    return True


def confirm_receipt(actor, transfer_id, expected_version, quantity, condition, accepted, note="", scanned_code=""):
    """Stage 6/7: matching receipt commits custody atomically; any mismatch -> Exception (FR-52)."""
    with transaction.atomic():
        t = _lock_transfer(transfer_id)
        source = catalog.lock_asset(t.source_asset_id)
        policy.require(actor.pk == t.receiving_user_id, "Only the designated receiving custodian can confirm receipt.", "confirm_receipt", t)
        if t.status in (TransferStatus.COMPLETED, TransferStatus.EXCEPTION):
            return t  # idempotent: a repeated confirmation never moves stock twice
        if t.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This transfer changed after you opened it. Reload and try again.")
        if t.status != TransferStatus.RELEASED:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Receipt can only be confirmed after the releasing custodian records the release.")
        quantity = V.integer(quantity, "quantity", label="Received quantity", min_value=0, max_value=V.MAX_ASSET_QTY)
        note = V.text(note, "note", label="Note", max_len=V.MAX_SHORT * 2, multiline=True)
        if quantity < 0 or quantity > t.quantity:
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"Received quantity must be between 0 and {t.quantity}.", {"quantity": "Enter the counted quantity."})
        if condition not in Condition.RANK:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose the condition you observed.", {"condition": "Choose a condition."})
        t.receipt_scan_verified = match_scanned_code(scanned_code, source)
        t.received_quantity, t.received_condition, t.receipt_note, t.received_at = quantity, condition, note.strip(), timezone.now()
        problems = []
        if quantity != t.quantity:
            problems.append(("QUANTITY_MISMATCH", f"Counted {quantity} of {t.quantity} released units."))
        if Condition.RANK[condition] < Condition.RANK.get(source.condition, 0):
            problems.append(("DAMAGE", f"Received in {Condition.label(condition)} condition; released as {source.condition_label}."))
        if not accepted:
            problems.append(("NOT_ACCEPTED", "Receiver did not accept the delivery."))
        if problems:
            _move(t, TransferStatus.EXCEPTION)
            t.exception_kind = problems[0][0]
            t.exception_reason = " ".join(p[1] for p in problems) + (f" Note: {note.strip()}" if note.strip() else "")
            t.save()
            catalog.log_event(source, actor, "TRANSFER_EXCEPTION", quantity=quantity, after={"transfer": t.reference, "problem": t.exception_reason}, transfer=t,
                              reason="Last confirmed custody preserved until authorised resolution")
            audit.record(actor, "Receipt exception", "transfer", t, after={"received": quantity, "condition": condition}, reason=t.exception_reason)
            req = t.reservation.request
            audit.notify([t.releasing_user, req.assigned_approver, req.requester], "TRANSFER_EXCEPTION", f"{t.reference} needs resolution", t.exception_reason, t, reverse("workflow:transfer", args=[t.pk]))
            return t
        _complete(actor, t, source, quantity, condition)
        return t


def _complete(actor, t, source, quantity, condition):
    from apps.exchange import services as exchange
    from apps.exchange.models import Reservation, ReservationStatus
    from apps.reporting import impacts

    req = t.reservation.request
    hold = Reservation.objects.select_for_update().get(pk=t.reservation_id)
    before = catalog.snapshot(source)
    source_dept = source.owner_department
    whole_batch = quantity == source.quantity and catalog.balance(source).committed == quantity
    if whole_batch:
        received = source
        old_status = received.status
        received.owner_department = req.department
        received.custodian = t.receiving_user
        received.campus, received.building, received.room = t.destination_campus, t.destination_building, t.destination_room
        received.condition = condition
        received.is_published = False
        received.save()
        # Consume the hold first so the batch no longer shows a commitment.
        hold.remaining_quantity -= quantity
        hold.status = ReservationStatus.CONSUMED if hold.remaining_quantity == 0 else ReservationStatus.ALLOCATED
        hold.save()
        if old_status != AssetStatus.IN_USE:
            catalog.transition(received, AssetStatus.IN_USE, actor, f"Received through {t.reference}", event=False)
    else:
        received = catalog.split_batch(
            source, quantity, actor, f"Partial transfer {t.reference}", status=AssetStatus.IN_USE,
            owner_department=req.department, custodian=t.receiving_user, campus=t.destination_campus,
            building=t.destination_building, room=t.destination_room, condition=condition,
        )
        hold.remaining_quantity -= quantity
        hold.status = ReservationStatus.CONSUMED if hold.remaining_quantity == 0 else ReservationStatus.ALLOCATED
        hold.save()
        catalog.sync_listing_status(source, actor, f"{quantity} unit(s) transferred out")
    _move(t, TransferStatus.COMPLETED)
    t.received_asset = received
    t.save()
    event = catalog.log_event(received, actor, "RECEIVED", quantity=quantity, before=before, after=catalog.snapshot(received), transfer=t,
                              reason=f"Matching receipt confirmed for {t.reference}; custody, department and location updated")
    impacts.record_reuse(received, event, quantity, req.department, source_counted=source.reuse_counted, cohort_entered_at=source.cohort_entered_at,
                         source_department=source_dept)
    if not received.reuse_counted:
        received.reuse_counted = True
        received.save(update_fields=["reuse_counted"])
    exchange.record_receipt(req, quantity, actor, t)
    audit.record(actor, "Transfer completed", "transfer", t, before=before, after={"received_asset": received.asset_tag, "custodian": t.receiving_user.campus_id, "department": req.department.code, "quantity": quantity, "qr_verified": t.receipt_scan_verified})
    audit.notify([req.requester, t.releasing_user, req.assigned_approver], "TRANSFER_RECEIVED", f"{t.reference} received",
                 f"{quantity} × {received.name} confirmed at {t.destination}. Custody now with {t.receiving_user.full_name}.", t, reverse("workflow:transfer", args=[t.pk]))


def resolve_exception(actor, transfer_id, expected_version, resolution, reason):
    """Authorised Exception resolution: accept the confirmed evidence, or return stock to source."""
    from apps.exchange import services as exchange
    from apps.exchange.models import Reservation, ReservationStatus

    with transaction.atomic():
        t = _lock_transfer(transfer_id)
        source = catalog.lock_asset(t.source_asset_id)
        policy.require(policy.can_resolve_exception(actor, t), "Only an authorised approver (not the requester) can resolve transfer exceptions.", "resolve_exception", t)
        if t.status != TransferStatus.EXCEPTION:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "This transfer is not in Exception.")
        if t.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This transfer changed after you opened it. Reload and try again.")
        if resolution not in ("ACCEPT_RECEIVED", "RETURN_TO_SOURCE"):
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose how to resolve the exception.", {"resolution": "Choose accept or return to source."})
        if len((reason or "").strip()) < 8:
            raise BusinessError(BusinessError.MISSING_EVIDENCE, "Record the resolution evidence and reason.", {"reason": "At least 8 characters."})
        reason = V.reason(reason, min_len=8)
        t.resolution, t.resolved_by, t.resolved_at = f"{resolution}: {reason.strip()}", actor, timezone.now()
        if resolution == "ACCEPT_RECEIVED":
            qty = t.received_quantity or 0
            if qty < 1:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Nothing was received; choose 'return to source' instead.")
            shortfall = t.quantity - qty
            t.save()
            _complete(actor, t, source, qty, t.received_condition or source.condition)
            if shortfall:
                follow = Transfer.objects.create(
                    reference=next_reference("T"), reservation=t.reservation, approval=t.approval, source_asset=source, quantity=shortfall,
                    releasing_user=t.releasing_user, receiving_user=t.receiving_user, destination_campus=t.destination_campus,
                    destination_building=t.destination_building, destination_room=t.destination_room,
                )
                audit.record(actor, "Follow-up transfer for remaining allocation", "transfer", follow, after={"quantity": shortfall, "from": t.reference})
        elif resolution == "RETURN_TO_SOURCE":
            _move(t, TransferStatus.CANCELLED)
            t.save()
            hold = Reservation.objects.select_for_update().get(pk=t.reservation_id)
            hold.remaining_quantity -= t.quantity
            if hold.remaining_quantity == 0:
                hold.status = ReservationStatus.CANCELLED
                hold.released_at = timezone.now()
                hold.release_reason = f"Exception resolved — stock returned to source: {reason.strip()}"
            hold.save()
            catalog.sync_listing_status(source, actor, "Exception resolved; stock returned to source")
            catalog.log_event(source, actor, "TRANSFER_EXCEPTION", quantity=t.quantity, after={"resolution": "returned to source"}, transfer=t, reason=reason)
            exchange.after_allocation_change(t.reservation.request, actor)
        else:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a resolution.")
        audit.record(actor, "Exception resolved", "transfer", t, after={"resolution": resolution}, reason=reason)
        req = t.reservation.request
        audit.notify([req.requester, t.releasing_user, t.receiving_user], "EXCEPTION_RESOLVED", f"{t.reference} exception resolved", t.resolution, t, reverse("workflow:transfer", args=[t.pk]))
        return t
