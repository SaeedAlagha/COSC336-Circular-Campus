"""D5 Service and life-cycle records (design 5.3.6/5.3.7, Figures 25/26; states 41/43)."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class MaintenanceStatus:
    REPORTED = "REPORTED"
    UNDER_INSPECTION = "UNDER_INSPECTION"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    IN_SERVICE = "IN_SERVICE"
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"
    COMPLETED = "COMPLETED"
    UNREPAIRABLE = "UNREPAIRABLE"
    CHOICES = [
        (REPORTED, "Reported / Awaiting Inspection"), (UNDER_INSPECTION, "Under Inspection"), (AWAITING_APPROVAL, "Awaiting Approval"),
        (IN_SERVICE, "In Service"), (AWAITING_VERIFICATION, "Awaiting Verification"), (COMPLETED, "Completed / Closed"), (UNREPAIRABLE, "Unrepairable"),
    ]
    TRANSITIONS = {  # Figure 41
        REPORTED: {UNDER_INSPECTION},
        UNDER_INSPECTION: {AWAITING_APPROVAL, AWAITING_VERIFICATION, UNREPAIRABLE},
        AWAITING_APPROVAL: {IN_SERVICE, UNDER_INSPECTION},
        IN_SERVICE: {AWAITING_VERIFICATION},
        AWAITING_VERIFICATION: {COMPLETED, UNDER_INSPECTION},
        COMPLETED: set(), UNREPAIRABLE: set(),
    }
    OPEN = {REPORTED, UNDER_INSPECTION, AWAITING_APPROVAL, IN_SERVICE, AWAITING_VERIFICATION}

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class MaintenanceRecord(models.Model):
    REPAIR = "REPAIR"
    REFURBISH = "REFURBISH"
    WORK_TYPES = [(REPAIR, "Repair"), (REFURBISH, "Refurbishment")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    # [UML: Association — Asset 1 —— 0..* MaintenanceRecord "concerns" / "has" (Figures 17, 25)]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="maintenance_records")
    # [UML: Association — User 1 —— 0..* MaintenanceRecord "reports" (Figure 25)]
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="defects_reported")
    description = models.TextField()
    evidence_note = models.TextField(blank=True)
    reported_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=22, choices=MaintenanceStatus.CHOICES, default=MaintenanceStatus.REPORTED)
    assigned_staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="service_assignments")
    before_condition = models.CharField(max_length=12)
    after_condition = models.CharField(max_length=12, blank=True)
    work_type = models.CharField(max_length=10, choices=WORK_TYPES, blank=True)
    proposed_work = models.TextField(blank=True)
    proposed_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    work_done = models.TextField(blank=True)
    actual_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)], help_text="Unknown stays empty, never zero.")
    work_started_at = models.DateTimeField(null=True, blank=True)
    work_completed_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_note = models.TextField(blank=True)
    revised_usable_until = models.DateField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-reported_at"]
        constraints = [models.CheckConstraint(condition=~Q(status="COMPLETED") | Q(verified_by__isnull=False), name="completed_service_requires_verifier")]

    def __str__(self):
        return f"{self.reference} · {self.asset.asset_tag}"

    @property
    def status_label(self):
        return MaintenanceStatus.label(self.status)



    # --- Class-diagram operations (Figure 17) ---
    def record_inspection(self, actor, finding, result, work_type="", proposed_work="", proposed_cost=None):
        """MaintenanceRecord.recordInspection()"""
        from . import services

        return services.record_inspection(actor, self.pk, self.version, finding, result, work_type, proposed_work, proposed_cost)

    def verify_safe_return(self, actor, safe, note, after_condition=None, revised_usable_until=None):
        """MaintenanceRecord.verifySafeReturn(): custodian (not the technician) restores availability."""
        from . import services

        return services.verify(actor, self.pk, self.version, safe, note, after_condition, revised_usable_until)


class Inspection(models.Model):
    SAFE_NO_REPAIR = "SAFE_NO_REPAIR"
    NEEDS_REPAIR = "NEEDS_REPAIR"
    UNREPAIRABLE = "UNREPAIRABLE"
    RESULTS = [(SAFE_NO_REPAIR, "Safe — no repair needed"), (NEEDS_REPAIR, "Repair or refurbishment needed"), (UNREPAIRABLE, "Cannot be repaired safely")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Composition — MaintenanceRecord 1 ◆—— 0..* Inspection "has" (Figure 25); an inspection exists only within its record]
    maintenance = models.ForeignKey(MaintenanceRecord, on_delete=models.PROTECT, related_name="inspections")
    # [UML: Association — User 1 —— 0..* Inspection "performs" (Figure 25)]
    inspector = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    finding = models.TextField()
    result = models.CharField(max_length=16, choices=RESULTS)
    inspected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["inspected_at"]


class DispositionAction:
    RETIRE = "RETIRE"
    COMPONENT_RECOVERY = "COMPONENT_RECOVERY"
    DONATE = "DONATE"
    RECYCLE = "RECYCLE"
    DISPOSE = "DISPOSE"
    CHOICES = [(RETIRE, "Retire"), (COMPONENT_RECOVERY, "Disassemble for usable components"), (DONATE, "Donate"), (RECYCLE, "Recycle"), (DISPOSE, "Dispose (final option)")]
    RESULT_STATUS = {RETIRE: "RETIRED", COMPONENT_RECOVERY: "COMPONENT_RECOVERED", DONATE: "DONATED", RECYCLE: "RECYCLED", DISPOSE: "DISPOSED"}
    DIVERTING = {COMPONENT_RECOVERY, DONATE, RECYCLE}

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class Disposition(models.Model):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    STATUSES = [(PROPOSED, "Proposed / pending approval"), (APPROVED, "Approved — awaiting completion evidence"), (REJECTED, "Rejected"), (RETURNED, "Returned for correction"), (COMPLETED, "Completed"), (CANCELLED, "Cancelled")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    # [UML: Association — Asset 1 —— 0..* Disposition "concerns" (Figures 17, 26)]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="dispositions")
    # [UML: Association — User 1 —— 0..* Disposition "proposes" (Figure 26)]
    proposer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="dispositions_proposed")
    action = models.CharField(max_length=20, choices=DispositionAction.CHOICES)
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    condition_at_proposal = models.CharField(max_length=12)
    justification = models.TextField()
    alternatives_considered = models.TextField()
    evidence_note = models.TextField(blank=True)
    estimated_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    proposed_recipient = models.CharField(max_length=160, blank=True)
    prior_status = models.CharField(max_length=22)
    status = models.CharField(max_length=12, choices=STATUSES, default=PROPOSED)
    completion_date = models.DateField(null=True, blank=True)
    completed_quantity = models.PositiveIntegerField(null=True, blank=True)
    completion_recipient = models.CharField(max_length=160, blank=True)
    completion_evidence = models.TextField(blank=True)
    recovered_components = models.TextField(blank=True, help_text="One component per line: name; quantity")
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)
    # [UML: Dependency — Disposition uses reviewed AI advice; advice never authorises (Figure 17)]
    ai_result = models.ForeignKey("intelligence.AIResult", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="Advice reviewed before proposing (advisory only).")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} · {self.get_action_display()}"

    # --- Class-diagram operations (Figure 17) ---
    @classmethod
    def propose(cls, actor, asset, action, quantity, justification, alternatives, **extra):
        """Disposition.propose(): a proposal only; approval and evidence come later (FR-53)."""
        from . import services

        return services.propose_disposition(actor, asset.pk, asset.version, action, quantity, justification, alternatives, **extra)

    def record_evidence(self, actor, completion_date, quantity, recipient, evidence, components_text=""):
        """Disposition.recordEvidence(): sets the final state only after approval + evidence (FR-54)."""
        from . import services

        return services.complete_disposition(actor, self.pk, completion_date, quantity, recipient, evidence, components_text)


class LifeCycleEvent(models.Model):
    """Append-only dated history (FR-09, FR-22, FR-23). Corrections reference the original."""

    TYPES = [(t, t.replace("_", " ").title()) for t in [
        "REGISTERED", "UPDATED", "CORRECTION", "PUBLISHED", "UNPUBLISHED", "RESERVED", "HOLD_RELEASED", "ALLOCATED",
        "TRANSFER_SCHEDULED", "RELEASED", "RECEIVED", "TRANSFER_EXCEPTION", "SPLIT", "DEFECT_REPORTED", "INSPECTION",
        "RESTRICTED", "REPAIR_APPROVED", "REPAIRED", "REFURBISHED", "RETURNED_TO_SERVICE", "UNREPAIRABLE",
        "DISPOSITION_PROPOSED", "DISPOSITION_APPROVED", "DISPOSITION_REJECTED", "RETIRED", "DONATED", "RECYCLED",
        "DISPOSED", "COMPONENT_RECOVERED", "STATUS_CHANGED", "ATTACHMENT_ADDED", "AI_CLASSIFICATION_REVIEWED",
    ]]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Composition — Asset 1 ◆—— 0..* LifeCycleEvent "has" (Figure 26); append-only history]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    event_type = models.CharField(max_length=30, choices=TYPES)
    quantity = models.PositiveIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    before = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    after = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    reason = models.TextField(blank=True)
    transfer = models.ForeignKey("workflow.Transfer", null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    maintenance = models.ForeignKey(MaintenanceRecord, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    # [UML: Association — Disposition 0..1 "records" 0..* LifeCycleEvent (Figure 26)]
    disposition = models.ForeignKey(Disposition, null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    correction_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="corrections")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["asset", "occurred_at"], name="event_asset_time")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Life-cycle events are append-only; record a correction instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Life-cycle events cannot be deleted.")

    @property
    def label(self):
        return dict(self.TYPES).get(self.event_type, self.event_type)


class AssetLineage(models.Model):
    """Parent/child stock provenance for batch splits and recovered components (Fig. 26)."""

    SPLIT = "SPLIT"
    RECOVERED_COMPONENT = "RECOVERED_COMPONENT"
    # [UML: Association — AssetLineage parent Asset (Figure 26)]
    parent_asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="lineage_children")
    # [UML: Association — AssetLineage child Asset (Figure 26)]
    child_asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="lineage_parents")
    # [UML: Association — LifeCycleEvent 1 "documents" 0..* AssetLineage (Figure 26)]
    event = models.ForeignKey(LifeCycleEvent, on_delete=models.PROTECT, related_name="lineage")
    kind = models.CharField(max_length=20, choices=[(SPLIT, "Batch split"), (RECOVERED_COMPONENT, "Recovered component")])
    quantity = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["parent_asset", "child_asset"], name="uniq_lineage_pair"),
            models.CheckConstraint(condition=~Q(parent_asset=F("child_asset")), name="lineage_no_self_link"),
        ]
