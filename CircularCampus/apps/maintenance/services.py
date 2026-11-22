"""Service and disposition component (process 5.0): defects, inspection, approved
repair, verified safe return (UC-04) and evidence-backed final disposition (UC-13)."""
from __future__ import annotations

import re

from datetime import date
from decimal import Decimal

from django.db import transaction
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone

from apps.accounts import policy
from apps.accounts.models import Role, User
from apps.catalog import services as catalog
from apps.catalog.models import Asset, AssetStatus, Condition
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.refs import next_reference
from apps.exchange.models import ReservationStatus
from apps.reporting import audit, impacts
from apps.workflow import services as workflow
from apps.workflow.models import Approval, ProposalVersion

from .models import Disposition, DispositionAction, Inspection, MaintenanceRecord, MaintenanceStatus


def _move(record, new):
    if new not in MaintenanceStatus.TRANSITIONS[record.status]:
        raise BusinessError(BusinessError.INVALID_TRANSITION, f"{record.reference} is {record.status_label}; that step is not allowed now.")
    record.status = new
    record.version += 1


def _lock(record_id):
    record_id = V.parse_uuid(record_id, "record")
    try:
        return MaintenanceRecord.objects.select_for_update(of=("self",)).select_related("asset").get(pk=record_id)
    except MaintenanceRecord.DoesNotExist:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That service record does not exist.")


def _staff_for(asset):
    users = User.objects.filter(is_active=True, role_grants__active=True, role_grants__role__code=Role.MAINTENANCE).distinct()
    return [u for u in users if policy.has_role(u, Role.MAINTENANCE, asset.owner_department_id)]


def _lock_disposition(disposition_id):
    disposition_id = V.parse_uuid(disposition_id, "disposition")
    try:
        return Disposition.objects.select_for_update(of=("self",)).select_related("asset").get(pk=disposition_id)
    except Disposition.DoesNotExist:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That disposition does not exist.")


def _clean_disposition_text(justification, alternatives, evidence, estimated_cost, recipient):
    errors = {}
    for name, value in (("justification", justification), ("alternatives", alternatives)):
        if len((value or "").strip()) < 10:
            errors[name] = "At least 10 characters."
    if errors:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Give the justification and the alternatives considered (reuse, repair, donation…).", errors)
    return (V.text(justification, "justification", label="Justification", required=True, min_len=10, max_len=V.MAX_LONG, multiline=True),
            V.text(alternatives, "alternatives", label="Alternatives considered", required=True, min_len=10, max_len=V.MAX_LONG, multiline=True),
            V.evidence_ref(evidence, "evidence", required=False),
            V.money(estimated_cost, "estimated_cost", label="Estimated cost"),
            V.text(recipient, "recipient", label="Recipient", max_len=160))


def _check_version(obj, expected):
    if expected is not None and obj.version != V.version(expected):
        raise BusinessError(BusinessError.STALE_VERSION, "This record changed after you opened it. Reload and try again.")


# ------------------------------------------------------------------ defects ---
def report_defect(actor, asset_id, description, evidence=""):
    """FR-20/FR-24: open a service record and restrict the stock from ordinary reuse."""
    with transaction.atomic():
        asset = catalog.lock_asset(asset_id)
        policy.require(policy.can_report_defect(actor, asset), "You cannot report a defect for this asset.", "report_defect", asset)
        if len((description or "").strip()) < 10:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Describe the defect (at least 10 characters).", {"description": "Describe what is wrong."})
        description = V.text(description, "description", label="Description", required=True, min_len=10, max_len=V.MAX_LONG, multiline=True)
        evidence = V.evidence_ref(evidence, "evidence", required=False)
        if asset.maintenance_records.filter(status__in=MaintenanceStatus.OPEN).exists():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "This asset already has an open service record.")
        if asset.status in AssetStatus.FINAL | {AssetStatus.DISPOSITION_PENDING}:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Assets in disposition cannot enter maintenance.")
        record = MaintenanceRecord.objects.create(reference=next_reference("M"), asset=asset, reporter=actor, description=description.strip(),
                                                  evidence_note=evidence.strip(), before_condition=asset.condition)
        before_status = asset.status
        if asset.is_published:
            asset.is_published = False  # restricted stock leaves the catalog until verified safe
            asset.save(update_fields=["is_published"])
        catalog.transition(asset, AssetStatus.UNDER_INSPECTION, actor, f"Defect reported ({record.reference})", event=False)
        flagged = 0
        for hold in asset.reservations.select_for_update().filter(status__in=ReservationStatus.ACTIVE):
            hold.flagged_for_review = True
            hold.flag_reason = f"Safety review {record.reference}: stock restricted"
            hold.save(update_fields=["flagged_for_review", "flag_reason"])
            flagged += 1
            audit.notify([hold.request.requester, hold.request.assigned_approver], "HOLD_FLAGGED", f"{hold.reference} flagged for safety review",
                         f"{asset.asset_tag} is restricted while {record.reference} is open; release is blocked.", hold.request, reverse("exchange:request", args=[hold.request_id]))
        catalog.log_event(asset, actor, "DEFECT_REPORTED", maintenance=record, before={"status": before_status}, after={"status": asset.status, "holds_flagged": flagged}, reason=description[:500])
        audit.record(actor, "Defect reported", "maintenance", record, after={"asset": asset.asset_tag, "holds_flagged": flagged})
        audit.notify(_staff_for(asset) + [asset.custodian], "DEFECT_REPORTED", f"Defect reported on {asset.asset_tag}", description[:200], record, reverse("maintenance:record", args=[record.pk]))
        return record


def start_inspection(actor, record_id, expected_version=None):
    with transaction.atomic():
        r = _lock(record_id)
        policy.require(policy.can_service(actor, r.asset), "Only maintenance staff for this department can inspect.", "inspect", r)
        _check_version(r, expected_version)
        _move(r, MaintenanceStatus.UNDER_INSPECTION)
        r.assigned_staff = actor
        r.save()
        audit.record(actor, "Inspection started", "maintenance", r)
        return r


def record_inspection(actor, record_id, expected_version, finding, result, work_type="", proposed_work="", proposed_cost=None):
    with transaction.atomic():
        r = _lock(record_id)
        asset = catalog.lock_asset(r.asset_id)
        policy.require(policy.can_service(actor, asset), "Only maintenance staff for this department can record inspections.", "inspect", r)
        _check_version(r, expected_version)
        if r.status != MaintenanceStatus.UNDER_INSPECTION:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Start the inspection first.")
        if len((finding or "").strip()) < 8:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Record the inspection findings.", {"finding": "At least 8 characters."})
        finding = V.text(finding, "finding", label="Findings", required=True, min_len=8, max_len=V.MAX_LONG, multiline=True)
        if result not in dict(Inspection.RESULTS):
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose an inspection result.", {"result": "Choose a result."})
        Inspection.objects.create(maintenance=r, inspector=actor, finding=finding.strip(), result=result)
        catalog.log_event(asset, actor, "INSPECTION", maintenance=r, after={"result": result}, reason=finding[:500])
        if result == Inspection.SAFE_NO_REPAIR:
            _move(r, MaintenanceStatus.AWAITING_VERIFICATION)
            r.save()
            audit.notify([asset.custodian], "VERIFY_SERVICE", f"Verify safe return of {asset.asset_tag}", "Inspection found no repair needed. A custodian must verify before reuse.", r, reverse("maintenance:record", args=[r.pk]))
        elif result == Inspection.UNREPAIRABLE:
            _move(r, MaintenanceStatus.UNREPAIRABLE)
            r.save()
            catalog.transition(asset, AssetStatus.UNAVAILABLE, actor, f"Unrepairable ({r.reference})", event=False)
            catalog.log_event(asset, actor, "UNREPAIRABLE", maintenance=r, after={"status": asset.status}, reason="Not repairable safely; may enter an approved disposition proposal (never automatic disposal)")
            audit.notify([asset.custodian], "UNREPAIRABLE", f"{asset.asset_tag} is unrepairable", "The asset stays Unavailable. You may propose a disposition.", r, reverse("maintenance:record", args=[r.pk]))
        else:
            if work_type not in (MaintenanceRecord.REPAIR, MaintenanceRecord.REFURBISH):
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose repair or refurbish.", {"work_type": "Choose the type of work."})
            if len((proposed_work or "").strip()) < 8:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Describe the proposed repair or refurbishment.", {"proposed_work": "Describe the work (at least 8 characters)."})
            proposed_work = V.text(proposed_work, "proposed_work", label="Proposed work", required=True, min_len=8, max_len=V.MAX_LONG, multiline=True)
            proposed_cost = V.money(proposed_cost, "proposed_cost", label="Proposed cost")
            r.work_type, r.proposed_work, r.proposed_cost_aed = work_type, proposed_work.strip(), proposed_cost
            _move(r, MaintenanceStatus.AWAITING_APPROVAL)
            r.save()
            approver = workflow.assigned_approver(asset.owner_department, exclude_user=actor)
            if approver is None:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "No approver is assigned for this department.")
            needs_cost = proposed_cost is None or Decimal(proposed_cost) > 0
            version = workflow.create_version(
                kind=ProposalVersion.REPAIR, target_field="maintenance", target=r, proposer=actor,
                snapshot={"maintenance": r.reference, "asset": asset.asset_tag, "work_type": work_type, "work": r.proposed_work, "proposed_cost_aed": proposed_cost},
                stages=[(Approval.RELEASE_CHECK, asset.owner_department, None, False), (Approval.COST_REVIEW, asset.owner_department, None, needs_cost),
                        (Approval.AUTHORIZE, asset.owner_department, approver, True)])
            workflow.notify_stage(version)
        audit.record(actor, "Inspection recorded", "maintenance", r, after={"result": result, "proposed_cost": proposed_cost})
        return r


def repair_authorised(actor, record, version):
    r = _lock(record.pk)
    asset = catalog.lock_asset(r.asset_id)
    _move(r, MaintenanceStatus.IN_SERVICE)
    r.work_started_at = timezone.now()
    r.save()
    catalog.transition(asset, AssetStatus.IN_MAINTENANCE, actor, f"Repair authorised ({r.reference})", event=False)
    catalog.log_event(asset, actor, "REPAIR_APPROVED", maintenance=r, after={"version": version.label, "status": asset.status})
    audit.notify([r.assigned_staff], "REPAIR_APPROVED", f"Repair approved: {r.reference}", "You may start the authorised work.", r, reverse("maintenance:record", args=[r.pk]))


def repair_declined(actor, record, reason):
    r = _lock(record.pk)
    _move(r, MaintenanceStatus.UNDER_INSPECTION)
    r.save()
    audit.notify([r.assigned_staff], "REPAIR_DECLINED", f"Repair not authorised: {r.reference}", reason, r, reverse("maintenance:record", args=[r.pk]))


def record_work(actor, record_id, expected_version, work_done, actual_cost, after_condition):
    with transaction.atomic():
        r = _lock(record_id)
        policy.require(policy.can_service(actor, r.asset), "Only maintenance staff can record service work.", "record_work", r)
        _check_version(r, expected_version)
        if r.status != MaintenanceStatus.IN_SERVICE:
            raise BusinessError(BusinessError.MISSING_REVIEW, "Work can be recorded only after the repair proposal is authorised.")
        if len((work_done or "").strip()) < 8:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Describe the work completed.", {"work_done": "At least 8 characters."})
        work_done = V.text(work_done, "work_done", label="Work done", required=True, min_len=8, max_len=V.MAX_LONG, multiline=True)
        actual_cost = V.money(actual_cost, "actual_cost", label="Actual cost")
        if after_condition not in Condition.RANK:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose the resulting condition.", {"after_condition": "Choose a condition."})
        r.work_done, r.actual_cost_aed, r.after_condition, r.work_completed_at = work_done.strip(), actual_cost, after_condition, timezone.now()
        _move(r, MaintenanceStatus.AWAITING_VERIFICATION)
        r.save()
        catalog.log_event(r.asset, actor, "REPAIRED" if r.work_type == MaintenanceRecord.REPAIR else "REFURBISHED", maintenance=r,
                          after={"actual_cost_aed": actual_cost, "after_condition": after_condition}, reason=work_done[:500])
        audit.record(actor, "Service work recorded", "maintenance", r, after={"actual_cost": actual_cost, "after_condition": after_condition})
        audit.notify([r.asset.custodian], "VERIFY_SERVICE", f"Verify safe return of {r.asset.asset_tag}", "Service work is recorded and awaits your verification.", r, reverse("maintenance:record", args=[r.pk]))
        return r


def verify(actor, record_id, expected_version, safe, note, after_condition=None, revised_usable_until=None):
    """Authorised safe verification by a custodian (not the technician) restores availability."""
    with transaction.atomic():
        r = _lock(record_id)
        asset = catalog.lock_asset(r.asset_id)
        policy.require(policy.can_verify_service(actor, asset), "Only an asset custodian for this department can verify safe return.", "verify", r)
        _check_version(r, expected_version)
        if r.status != MaintenanceStatus.AWAITING_VERIFICATION:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Nothing is awaiting verification.")
        if r.assigned_staff_id == actor.pk:
            raise BusinessError(BusinessError.SELF_APPROVAL, "The technician who did the work cannot also verify it.")
        if len((note or "").strip()) < 8:
            raise BusinessError(BusinessError.MISSING_EVIDENCE, "Record the verification evidence (tests performed, checks).", {"note": "At least 8 characters."})
        note = V.text(note, "note", label="Verification note", required=True, min_len=8, max_len=V.MAX_LONG, multiline=True)
        revised_usable_until = V.a_date(revised_usable_until, "revised_usable_until", label="Revised usable-until date", not_after=V.years_from_today(15))
        if after_condition not in (None, "") and after_condition not in Condition.RANK:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose the verified condition.", {"after_condition": "Choose a condition."})
        if not safe:
            _move(r, MaintenanceStatus.UNDER_INSPECTION)
            r.verification_note = note.strip()
            r.save()
            if asset.status == AssetStatus.IN_MAINTENANCE:
                catalog.transition(asset, AssetStatus.UNDER_INSPECTION, actor, "Verification failed — reassess", event=False)
            catalog.log_event(asset, actor, "INSPECTION", maintenance=r, after={"verification": "unsafe"}, reason=note)
            audit.record(actor, "Verification failed", "maintenance", r, reason=note)
            audit.notify([r.assigned_staff], "VERIFY_FAILED", f"{r.reference} failed verification", note, r, reverse("maintenance:record", args=[r.pk]))
            return r
        condition = after_condition or r.after_condition or asset.condition
        if condition not in Condition.RANK:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose the verified condition.")
        if revised_usable_until and revised_usable_until <= date.today():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "The revised usable-until date must be in the future.", {"revised_usable_until": "Choose a future date or leave blank."})
        _move(r, MaintenanceStatus.COMPLETED)
        r.verified_by, r.verified_at, r.verification_note, r.after_condition = actor, timezone.now(), note.strip(), condition
        r.revised_usable_until = revised_usable_until
        r.save()
        before = {"status": asset.status, "condition": asset.condition, "revised_usable_until": asset.revised_usable_until}
        asset.condition = condition
        if revised_usable_until:
            asset.revised_usable_until = revised_usable_until
        asset.save()
        catalog.transition(asset, AssetStatus.IN_USE, actor, f"Verified safe return ({r.reference})", event=False)
        for hold in asset.reservations.filter(flagged_for_review=True, status__in=ReservationStatus.ACTIVE):
            hold.flagged_for_review = False
            hold.flag_reason = f"Cleared by verified safe return {r.reference}"
            hold.save(update_fields=["flagged_for_review", "flag_reason"])
        event = catalog.log_event(asset, actor, "RETURNED_TO_SERVICE", maintenance=r, before=before,
                                  after={"status": asset.status, "condition": condition, "revised_usable_until": revised_usable_until}, reason=note)
        if r.work_type:
            impacts.record_repair(asset, event, r)
        if revised_usable_until:
            impacts.record_life_extension(asset, event)
        audit.record(actor, "Safe return verified", "maintenance", r, before=before, after={"condition": condition})
        audit.notify([r.reporter, r.assigned_staff], "SERVICE_CLOSED", f"{asset.asset_tag} returned to service", f"Verified by {actor.full_name}. The custodian may republish it.", r, reverse("maintenance:record", args=[r.pk]))
        return r


# -------------------------------------------------------------- disposition ---
DISPOSABLE_FROM = {AssetStatus.IN_USE, AssetStatus.UNAVAILABLE, AssetStatus.RETIRED}


def propose_disposition(actor, asset_id, expected_version, action, quantity, justification, alternatives, evidence="", estimated_cost=None, recipient="", ai_result=None):
    """FR-53: proposal only — approval and completion evidence are separate steps."""
    with transaction.atomic():
        asset = catalog.lock_asset(asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can propose a disposition.", "propose_disposition", asset)
        if expected_version is not None and asset.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This asset changed after you opened it. Reload and try again.")
        if action not in dict(DispositionAction.CHOICES):
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a disposition route.", {"action": "Choose a route."})
        if asset.status not in DISPOSABLE_FROM:
            raise BusinessError(BusinessError.INVALID_TRANSITION, f"A {asset.status_label} asset cannot enter disposition. Unpublish it and resolve holds first (it must be In Use, Unavailable or Retired).")
        if action == DispositionAction.RETIRE and asset.status == AssetStatus.RETIRED:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "This asset is already retired; choose a later route.")
        if catalog.balance(asset).committed:
            raise BusinessError(BusinessError.STOCK_CONFLICT, "Unresolved holds or allocations exist for this asset.")
        quantity = V.integer(quantity, "quantity", label="Quantity", min_value=1, max_value=V.MAX_ASSET_QTY)
        if quantity < 1 or quantity > asset.quantity:
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"Quantity must be between 1 and {asset.quantity}.", {"quantity": f"1–{asset.quantity}"})
        justification, alternatives, evidence, estimated_cost, recipient = _clean_disposition_text(justification, alternatives, evidence, estimated_cost, recipient)
        prior = asset.status
        target = asset
        if quantity < asset.quantity:
            target = catalog.split_batch(asset, quantity, actor, "Partial disposition proposal", status=asset.status)
        d = Disposition.objects.create(reference=next_reference("D"), asset=target, proposer=actor, action=action, quantity=quantity,
                                       condition_at_proposal=target.condition, justification=justification.strip(), alternatives_considered=alternatives.strip(),
                                       evidence_note=evidence.strip(), estimated_cost_aed=estimated_cost, proposed_recipient=recipient.strip(), prior_status=prior, ai_result=ai_result)
        catalog.transition(target, AssetStatus.DISPOSITION_PENDING, actor, f"Disposition proposed ({d.reference})", event=False)
        catalog.log_event(target, actor, "DISPOSITION_PROPOSED", quantity=quantity, disposition=d, after={"action": action}, reason=justification[:500])
        approver = workflow.assigned_approver(target.owner_department, exclude_user=actor)
        if approver is None:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "No approver is assigned for this department.")
        needs_cost = estimated_cost is not None and Decimal(estimated_cost) > 0
        version = workflow.create_version(
            kind=ProposalVersion.DISPOSITION, target_field="disposition", target=d, proposer=actor,
            snapshot={"disposition": d.reference, "asset": target.asset_tag, "action": action, "quantity": quantity, "estimated_cost_aed": estimated_cost, "recipient": d.proposed_recipient},
            stages=[(Approval.RELEASE_CHECK, target.owner_department, None, False), (Approval.COST_REVIEW, target.owner_department, None, needs_cost),
                    (Approval.AUTHORIZE, target.owner_department, approver, True)])
        workflow.notify_stage(version)
        audit.record(actor, "Disposition proposed", "disposition", d, after={"action": action, "quantity": quantity, "asset": target.asset_tag})
        return d


def disposition_authorised(actor, disposition, version):
    d = Disposition.objects.select_for_update().get(pk=disposition.pk)
    d.status = Disposition.APPROVED
    d.save(update_fields=["status"])
    catalog.log_event(d.asset, actor, "DISPOSITION_APPROVED", quantity=d.quantity, disposition=d, after={"version": version.label, "route": d.action},
                      reason="Authorisation only — the asset is not marked complete until evidence is recorded")
    audit.notify([d.asset.custodian, d.proposer], "DISPOSITION_APPROVED", f"{d.reference} authorised", "Record completion evidence after the real-world action.", d, reverse("maintenance:disposition", args=[d.pk]))


def disposition_declined(actor, disposition, reason, returned):
    d = Disposition.objects.select_for_update().get(pk=disposition.pk)
    asset = catalog.lock_asset(d.asset_id)
    d.status = Disposition.RETURNED if returned else Disposition.REJECTED
    d.save(update_fields=["status"])
    if not returned:
        catalog.transition(asset, AssetStatus.UNAVAILABLE, actor, f"Disposition {d.reference} rejected", event=False)
        catalog.log_event(asset, actor, "DISPOSITION_REJECTED", disposition=d, after={"status": asset.status}, reason=reason)
    audit.notify([d.proposer], "DISPOSITION_DECIDED", f"{d.reference} {'returned' if returned else 'rejected'}", reason, d, reverse("maintenance:disposition", args=[d.pk]))


def resubmit_disposition(actor, disposition_id, justification, alternatives, evidence, estimated_cost=None, recipient=""):
    with transaction.atomic():
        d = _lock_disposition(disposition_id)
        policy.require(policy.can_manage_asset(actor, d.asset), "Only this department's custodian can correct the proposal.", "resubmit_disposition", d)
        if d.status != Disposition.RETURNED:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Only returned proposals can be corrected.")
        justification, alternatives, evidence, estimated_cost, recipient = _clean_disposition_text(justification, alternatives, evidence, estimated_cost, recipient)
        d.justification, d.alternatives_considered, d.evidence_note = justification.strip(), alternatives.strip(), evidence.strip()
        d.estimated_cost_aed, d.proposed_recipient, d.status = estimated_cost, recipient.strip(), Disposition.PROPOSED
        d.save()
        approver = workflow.assigned_approver(d.asset.owner_department, exclude_user=actor)
        needs_cost = estimated_cost is not None and Decimal(estimated_cost) > 0
        version = workflow.create_version(
            kind=ProposalVersion.DISPOSITION, target_field="disposition", target=d, proposer=actor,
            snapshot={"disposition": d.reference, "asset": d.asset.asset_tag, "action": d.action, "quantity": d.quantity, "estimated_cost_aed": estimated_cost, "recipient": d.proposed_recipient},
            stages=[(Approval.RELEASE_CHECK, d.asset.owner_department, None, False), (Approval.COST_REVIEW, d.asset.owner_department, None, needs_cost),
                    (Approval.AUTHORIZE, d.asset.owner_department, approver, True)])
        workflow.notify_stage(version)
        return d


def complete_disposition(actor, disposition_id, completion_date, quantity, recipient, evidence, components_text=""):
    """FR-54 / NFR-06: the final state is recorded only after approval AND completion evidence."""
    with transaction.atomic():
        d = _lock_disposition(disposition_id)
        asset = catalog.lock_asset(d.asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can record completion.", "complete_disposition", d)
        if d.status == Disposition.COMPLETED:
            return d
        if d.status != Disposition.APPROVED:
            raise BusinessError(BusinessError.MISSING_REVIEW, "Approval alone is required before completion can be recorded; this proposal is not approved.")
        quantity = V.integer(quantity, "quantity", label="Quantity", min_value=1, max_value=V.MAX_ASSET_QTY)
        if quantity != d.quantity:
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"The authorised quantity is {d.quantity}. A different quantity needs a new proposal.", {"quantity": f"Must be {d.quantity}."})
        completion_date = V.a_date(completion_date, "completion_date", label="Completion date", required=True, not_before=date(2000, 1, 1))
        if not completion_date or completion_date > date.today():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Enter the actual completion date (not in the future).", {"completion_date": "Today or earlier."})
        if d.created_at and completion_date < timezone.localtime(d.created_at).date():
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Completion cannot be before the proposal was made.", {"completion_date": "On or after the proposal date."})
        if len((evidence or "").strip()) < 10:
            raise BusinessError(BusinessError.MISSING_EVIDENCE, "Record completion evidence (receipt number, certificate, processor reference).", {"evidence": "At least 10 characters."})
        evidence = V.text(evidence, "evidence", label="Completion evidence", required=True, min_len=10, max_len=V.MAX_LONG, multiline=True)
        recipient = V.text(recipient, "recipient", label="Recipient", max_len=160)
        components_text = V.text(components_text, "components", label="Components", max_len=V.MAX_LONG, multiline=True, needs_letter=False)
        if d.action in (DispositionAction.DONATE, DispositionAction.RECYCLE, DispositionAction.DISPOSE) and len(recipient) < 3:
            raise BusinessError(BusinessError.MISSING_EVIDENCE, "Name the recipient or processor.", {"recipient": "Required for this route."})
        components = []
        if d.action == DispositionAction.COMPONENT_RECOVERY:
            for line in components_text.splitlines():
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(";")]
                if len(parts) != 2 or not re.fullmatch(r"\d{1,4}", parts[1]) or not V._LETTER.search(parts[0]) or len(parts[0]) < 3 or len(parts[0]) > 160:
                    raise BusinessError(BusinessError.VALIDATION_ERROR, "List recovered components as 'name; quantity' per line.", {"components": "e.g. 16GB DDR4 RAM module; 2"})
                n = int(parts[1])
                if not 1 <= n <= V.MAX_ASSET_QTY:
                    raise BusinessError(BusinessError.VALIDATION_ERROR, "Each component quantity must be between 1 and 10,000.", {"components": "Use a positive quantity."})
                components.append((parts[0], n))
            if len(components) > 50:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "At most 50 component lines.", {"components": "Group similar parts on one line."})
            if not components:
                raise BusinessError(BusinessError.MISSING_EVIDENCE, "List at least one recovered component.", {"components": "Required for component recovery."})
        d.status = Disposition.COMPLETED
        d.completion_date, d.completed_quantity, d.completion_recipient, d.completion_evidence = completion_date, quantity, recipient, evidence
        d.recovered_components, d.completed_by, d.completed_at = components_text.strip(), actor, timezone.now()
        d.save()
        result_status = DispositionAction.RESULT_STATUS[d.action]
        catalog.transition(asset, result_status, actor, f"{d.get_action_display()} completed ({d.reference})", event=False)
        asset.is_published = False
        asset.save(update_fields=["is_published"])
        event = catalog.log_event(asset, actor, result_status if result_status != "COMPONENT_RECOVERED" else "COMPONENT_RECOVERED", quantity=d.quantity, disposition=d,
                                  after={"status": result_status, "recipient": d.completion_recipient}, reason=evidence[:500])
        for name, qty in components:
            from apps.maintenance.models import AssetLineage

            child = Asset.objects.create(
                asset_tag=catalog.next_child_tag(asset), name=name[:160], description=f"Component recovered from {asset.asset_tag} ({asset.name}) under {d.reference}.",
                specifications=f"Recovered component of {asset.asset_tag}", category=asset.category, quantity=qty, owner_department=asset.owner_department, custodian=asset.custodian,
                campus=asset.campus, building=asset.building, room=asset.room, condition=Condition.FAIR, status=AssetStatus.DRAFT, created_by=actor,
                parent_batch=None, reuse_counted=True, cohort_entered_at=None,
            )
            catalog.log_event(child, actor, "REGISTERED", quantity=qty, disposition=d, after={"recovered_from": asset.asset_tag}, reason="Recovered component — new typed stock with its own units")
            AssetLineage.objects.create(parent_asset=asset, child_asset=child, event=event, kind=AssetLineage.RECOVERED_COMPONENT, quantity=qty)
        if d.action in DispositionAction.DIVERTING:
            impacts.record_diversion(asset, event, d.quantity)
        audit.record(actor, "Disposition completed", "disposition", d, after={"status": result_status, "quantity": d.quantity, "recipient": d.completion_recipient})
        audit.notify([d.proposer], "DISPOSITION_COMPLETED", f"{d.reference} completed", f"{asset.asset_tag} is now {asset.status_label}.", d, reverse("maintenance:disposition", args=[d.pk]))
        return d
