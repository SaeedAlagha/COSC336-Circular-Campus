"""D4 Proposals, approvals and transfers (design 5.3.4/5.3.5, Figures 23/24; states 39/40)."""
import hashlib
import json
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class ProposalVersion(models.Model):
    """Immutable snapshot of exactly one proposal target (request, maintenance or disposition)."""

    REQUEST = "REQUEST"
    CANCELLATION = "CANCELLATION"
    REPAIR = "REPAIR"
    DISPOSITION = "DISPOSITION"
    KINDS = [(REQUEST, "Request / reservation"), (CANCELLATION, "Approved-request cancellation"), (REPAIR, "Repair cost"), (DISPOSITION, "Disposition")]

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"
    SUPERSEDED = "SUPERSEDED"
    WITHDRAWN = "WITHDRAWN"
    STATUSES = [(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected"), (RETURNED, "Returned"), (SUPERSEDED, "Superseded"), (WITHDRAWN, "Withdrawn")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — ResourceRequest 0..1 —— 0..* ProposalVersion "snapshots" (Figures 16, 23); exactly one target]
    request = models.ForeignKey("exchange.ResourceRequest", null=True, blank=True, on_delete=models.PROTECT, related_name="proposal_versions")
    # [UML: Association — MaintenanceRecord 0..1 —— 0..* ProposalVersion "has" (Figure 23)]
    maintenance = models.ForeignKey("maintenance.MaintenanceRecord", null=True, blank=True, on_delete=models.PROTECT, related_name="proposal_versions")
    # [UML: Association — Disposition 0..1 —— 0..* ProposalVersion "has" (Figure 23)]
    disposition = models.ForeignKey("maintenance.Disposition", null=True, blank=True, on_delete=models.PROTECT, related_name="proposal_versions")
    kind = models.CharField(max_length=14, choices=KINDS)
    version_no = models.PositiveIntegerField()
    snapshot = models.JSONField(encoder=DjangoJSONEncoder, )
    snapshot_hash = models.CharField(max_length=64)
    proposer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="proposals")
    status = models.CharField(max_length=12, choices=STATUSES, default=PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Exactly one populated target (design 5.3.4).
            models.CheckConstraint(
                condition=(
                    Q(request__isnull=False, maintenance__isnull=True, disposition__isnull=True)
                    | Q(request__isnull=True, maintenance__isnull=False, disposition__isnull=True)
                    | Q(request__isnull=True, maintenance__isnull=True, disposition__isnull=False)
                ),
                name="proposal_exactly_one_target",
            ),
            models.UniqueConstraint(fields=["request", "version_no"], condition=Q(request__isnull=False), name="uniq_request_version"),
            models.UniqueConstraint(fields=["maintenance", "version_no"], condition=Q(maintenance__isnull=False), name="uniq_maintenance_version"),
            models.UniqueConstraint(fields=["disposition", "version_no"], condition=Q(disposition__isnull=False), name="uniq_disposition_version"),
        ]

    @staticmethod
    def hash_snapshot(snapshot):
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()

    def matches(self, snapshot):
        """True when the live proposal content still equals this approved version (BR-11)."""
        return self.snapshot_hash == self.hash_snapshot(snapshot)

    def matches_current(self):
        """ProposalVersion.matchesCurrent(): still the latest version for its target and not superseded."""
        field = "request" if self.request_id else "maintenance" if self.maintenance_id else "disposition"
        latest = ProposalVersion.objects.filter(**{field: self.target}).order_by("-version_no").first()
        return latest is not None and latest.pk == self.pk and self.status in (self.PENDING, self.APPROVED)

    @property
    def target(self):
        return self.request or self.maintenance or self.disposition

    @property
    def label(self):
        return f"v{self.version_no}"

    def __str__(self):
        return f"{self.get_kind_display()} {self.label}"


class Approval(models.Model):
    """One stage decision on one immutable version (design 4.5.1, Figure 39)."""

    RELEASE_CHECK = "RELEASE_CHECK"
    COST_REVIEW = "COST_REVIEW"
    AUTHORIZE = "AUTHORIZE"
    STAGES = [(RELEASE_CHECK, "2 · Release eligibility & safety"), (COST_REVIEW, "3 · Finance cost review"), (AUTHORIZE, "4 · Authorise proposal")]
    STAGE_ORDER = {RELEASE_CHECK: 2, COST_REVIEW: 3, AUTHORIZE: 4}

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DECISIONS = [(APPROVED, "Approved"), (REJECTED, "Rejected"), (RETURNED, "Returned"), (NOT_APPLICABLE, "Not applicable")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — ProposalVersion 1 —— 0..* Approval "decides-on" (Figures 16, 23)]
    version = models.ForeignKey(ProposalVersion, on_delete=models.PROTECT, related_name="approvals")
    stage = models.CharField(max_length=14, choices=STAGES)
    stage_order = models.PositiveSmallIntegerField()
    scope_department = models.ForeignKey("accounts.Department", on_delete=models.PROTECT, related_name="+", help_text="Department whose role scope decides this stage.")
    # [UML: Association — User 1 —— 0..* Approval "reviews" (Figure 23)]
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="approvals_assigned", help_text="Assigned reviewer; for pooled stages the deciding user is stored at decision time.")
    decision = models.CharField(max_length=16, choices=DECISIONS, null=True, blank=True)
    reason = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["version", "stage_order"]
        constraints = [
            models.UniqueConstraint(fields=["version", "stage"], name="uniq_stage_per_version"),
            models.CheckConstraint(condition=Q(decision__isnull=True) | Q(decided_at__isnull=False), name="decided_at_required"),
        ]

    def decide(self, actor, decision, reason=""):
        """Approval.decide(actor): version-bound, ordered, non-proposer decision (BR-02, FR-51)."""
        from . import services

        return services.decide(actor, self.pk, self.version_id, decision, reason)

    @property
    def effective_status(self):
        if self.decision:
            if self.decision == self.APPROVED and self.version.status == ProposalVersion.SUPERSEDED:
                return "Superseded"
            return self.get_decision_display()
        if self.version.status in (ProposalVersion.SUPERSEDED, ProposalVersion.WITHDRAWN):
            return "Superseded"
        return "Pending"


class TransferStatus:
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    RELEASED = "RELEASED"
    EXCEPTION = "EXCEPTION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    CHOICES = [(APPROVED, "Approved"), (SCHEDULED, "Scheduled"), (RELEASED, "Released"), (EXCEPTION, "Exception"), (COMPLETED, "Received / Completed"), (CANCELLED, "Cancelled")]
    TRANSITIONS = {  # Figure 40 (Proposed/Pending lives on the ProposalVersion)
        APPROVED: {SCHEDULED, RELEASED, CANCELLED},
        SCHEDULED: {SCHEDULED, RELEASED, CANCELLED},
        RELEASED: {COMPLETED, EXCEPTION},
        EXCEPTION: {COMPLETED, CANCELLED},
        COMPLETED: set(), CANCELLED: set(),
    }

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class Transfer(models.Model):
    COLLECTION = "COLLECTION"
    DELIVERY = "DELIVERY"
    METHODS = [(COLLECTION, "Collection by receiver"), (DELIVERY, "Delivery by releasing department")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    # [UML: Association — Reservation 1 —— 0..* Transfer "uses" / "allocates" (Figures 16, 24)]
    reservation = models.ForeignKey("exchange.Reservation", on_delete=models.PROTECT, related_name="transfers")
    # [UML: Association — Approval 0..1 —— 0..* Transfer "authorized-by" (Figures 16, 24)]
    approval = models.ForeignKey(Approval, null=True, blank=True, on_delete=models.PROTECT, related_name="transfers")
    # [UML: Association — Asset 1 —— 0..* Transfer "supplies" (Figure 24)]
    source_asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="outgoing_transfers")
    # [UML: Association — Asset 0..1 "creates/updates" received stock (Figure 24)]
    received_asset = models.ForeignKey("catalog.Asset", null=True, blank=True, on_delete=models.PROTECT, related_name="incoming_transfers")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    # [UML: Association — User 1 "assigned-to-release" 0..* Transfer (Figure 24)]
    releasing_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="transfers_to_release")
    # [UML: Association — User 1 "assigned-to-receive" 0..* Transfer (Figure 24)]
    receiving_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="transfers_to_receive")
    destination_campus = models.CharField(max_length=80)
    destination_building = models.CharField(max_length=80)
    destination_room = models.CharField(max_length=40)
    status = models.CharField(max_length=12, choices=TransferStatus.CHOICES, default=TransferStatus.APPROVED)
    method = models.CharField(max_length=12, choices=METHODS, blank=True)
    scheduled_for = models.DateField(null=True, blank=True)
    arrangements = models.TextField(blank=True)
    transfer_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)], help_text="Actual transfer cost; unknown stays empty.")
    released_at = models.DateTimeField(null=True, blank=True)
    released_quantity = models.PositiveIntegerField(null=True, blank=True)
    release_evidence = models.TextField(blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    received_quantity = models.PositiveIntegerField(null=True, blank=True)
    received_condition = models.CharField(max_length=12, blank=True)
    receipt_scan_verified = models.BooleanField(default=False, help_text="Bonus: the receiver scanned the asset's QR label at handover.")
    receipt_note = models.TextField(blank=True)
    exception_kind = models.CharField(max_length=20, blank=True)
    exception_reason = models.TextField(blank=True)
    resolution = models.TextField(blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=1), name="transfer_quantity_positive")]

    def __str__(self):
        return f"{self.reference} · {self.quantity} × {self.source_asset.asset_tag}"

    @property
    def status_label(self):
        return TransferStatus.label(self.status)

    @property
    def destination(self):
        return f"{self.destination_campus} · {self.destination_building} · Room {self.destination_room}"

    # --- Class-diagram operations (Figure 16) ---
    def record_release(self, actor, quantity, evidence, transfer_cost=None):
        """Transfer.recordRelease(): actual handover; custody does not change yet (BR-13)."""
        from . import services

        return services.release(actor, self.pk, self.version, quantity, evidence, transfer_cost)

    def confirm_receipt(self, actor, quantity, condition, accepted=True, note=""):
        """Transfer.confirmReceipt(): matching receipt commits custody; mismatch -> Exception (FR-52)."""
        from . import services

        return services.confirm_receipt(actor, self.pk, self.version, quantity, condition, accepted, note)
