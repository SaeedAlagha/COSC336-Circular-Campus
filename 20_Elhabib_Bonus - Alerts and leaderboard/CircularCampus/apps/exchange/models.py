"""D3 Requests and reservations (design 5.3.3, Figure 22; states Figures 38 and 42)."""
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.catalog.models import Condition


class Urgency:
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    CHOICES = [(HIGH, "High"), (NORMAL, "Normal"), (LOW, "Low")]
    BUFFER_DAYS = {HIGH: 2, NORMAL: 1, LOW: 0}  # design 5.10 date/urgency fit


class RequestStatus:
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    RETURNED = "RETURNED"
    APPROVED = "APPROVED"
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"
    FULFILLED = "FULFILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    CHOICES = [
        (DRAFT, "Draft"), (SUBMITTED, "Submitted"), (UNDER_REVIEW, "Under Review"), (RETURNED, "Returned"),
        (APPROVED, "Approved"), (PARTIALLY_FULFILLED, "Partially Fulfilled"), (FULFILLED, "Fulfilled"),
        (REJECTED, "Rejected"), (CANCELLED, "Cancelled"),
    ]
    OPEN = {DRAFT, SUBMITTED, UNDER_REVIEW, RETURNED, APPROVED, PARTIALLY_FULFILLED}
    CLOSED = {FULFILLED, REJECTED, CANCELLED}
    TRANSITIONS = {  # Figure 38
        DRAFT: {SUBMITTED, CANCELLED},
        SUBMITTED: {UNDER_REVIEW, RETURNED, APPROVED, REJECTED, CANCELLED},
        UNDER_REVIEW: {RETURNED, APPROVED, REJECTED, CANCELLED},
        RETURNED: {SUBMITTED, CANCELLED},
        APPROVED: {PARTIALLY_FULFILLED, FULFILLED, CANCELLED},
        PARTIALLY_FULFILLED: {FULFILLED, CANCELLED},
        FULFILLED: set(), REJECTED: set(), CANCELLED: set(),
    }

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class RequestSpecification:
    """[UML: Composition — Figure 19 "ResourceRequest ◆—— RequestSpecification (owns, 1 to 1)"]

    An owned value object: it is created by its ResourceRequest, has no identity or
    table of its own, and cannot exist without the request (filled diamond). Its values
    are stored in the request's columns and frozen into ProposalVersion.snapshot.
    Figure 19 attributes: categoryId: UUID, quantity: int, constraints: Map.
    """

    def __init__(self, category_id, quantity, constraints):
        self.category_id = category_id
        self.quantity = quantity
        self.constraints = dict(constraints)

    @property
    def mandatory_terms(self):
        return [t.strip() for t in self.constraints.get("mandatory_specs", "").split(",") if t.strip()]

    def as_dict(self):
        return {"category": str(self.category_id), "quantity": self.quantity, **{k: v for k, v in self.constraints.items()}}


class ResourceRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    title = models.CharField(max_length=160)
    # [UML: Association — Category 1 —— 0..* ResourceRequest "classifies" (Figure 22)]
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="requests")
    purpose = models.TextField()
    specifications = models.TextField(blank=True)
    mandatory_specs = models.CharField(max_length=300, blank=True, help_text="Critical terms every candidate must state, separated by commas (BR-12).")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    preferred_condition = models.CharField("minimum condition", max_length=12, choices=Condition.CHOICES, default=Condition.GOOD)
    urgency = models.CharField(max_length=8, choices=Urgency.CHOICES, default=Urgency.NORMAL)
    destination_campus = models.CharField(max_length=80, default="Main Campus")
    destination_building = models.CharField(max_length=80)
    destination_room = models.CharField(max_length=40)
    required_date = models.DateField()
    allow_partial = models.BooleanField(default=False, help_text="Explicitly accept partial fulfilment from smaller batches (BR-15).")
    # [UML: Association — User 1 —— 0..* ResourceRequest "submitted-by" (Figures 18, 22)]
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests_made")
    # [UML: Association — Department 1 —— 0..* ResourceRequest "has" (Figure 22)]
    department = models.ForeignKey("accounts.Department", on_delete=models.PROTECT, related_name="requests")
    receiving_custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requests_to_receive")
    assigned_approver = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="requests_to_approve")
    status = models.CharField(max_length=22, choices=RequestStatus.CHOICES, default=RequestStatus.DRAFT)
    version = models.PositiveIntegerField(default=1)
    received_quantity = models.PositiveIntegerField(default=0)
    closed_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=Q(received_quantity__lte=F("quantity")), name="request_received_le_quantity")]
        indexes = [models.Index(fields=["status", "required_date"], name="request_status_date")]

    def __str__(self):
        return f"{self.reference} · {self.title}"

    @property
    def status_label(self):
        return RequestStatus.label(self.status)

    @property
    def is_open(self):
        return self.status in RequestStatus.OPEN

    @property
    def specification(self):
        """[UML: Composition — the request owns its RequestSpecification (Figure 19)]"""
        return RequestSpecification(self.category_id, self.quantity, {
            "specifications": self.specifications, "mandatory_specs": self.mandatory_specs,
            "preferred_condition": self.preferred_condition, "required_date": self.required_date,
            "urgency": self.urgency, "allow_partial": self.allow_partial,
        })

    @property
    def mandatory_list(self):
        return self.specification.mandatory_terms

    @property
    def destination(self):
        return f"{self.destination_campus} · {self.destination_building} · Room {self.destination_room}"


    # --- Class-diagram operations (Figure 16) ---
    def submit(self, actor, asset=None, quantity=None):
        """ResourceRequest.submit(): new proposal version + optional 48-hour hold, atomically."""
        from . import services

        return services.submit(actor, self.pk, self.version, getattr(asset, "pk", asset), quantity)

    def cancel(self, actor, reason):
        """ResourceRequest.cancel(): withdraw before approval, or propose cancellation after it."""
        from . import services
        from .models import RequestStatus as RS

        if self.status in (RS.APPROVED, RS.PARTIALLY_FULFILLED):
            return services.request_cancellation(actor, self.pk, self.version, reason)
        return services.withdraw(actor, self.pk, self.version, reason)


class ReservationStatus:
    PENDING = "PENDING_APPROVAL"
    ALLOCATED = "ALLOCATED"
    CONSUMED = "CONSUMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    CHOICES = [(PENDING, "Pending Approval"), (ALLOCATED, "Allocated"), (CONSUMED, "Consumed"), (REJECTED, "Rejected"), (CANCELLED, "Cancelled"), (EXPIRED, "Expired")]
    ACTIVE = {PENDING, ALLOCATED}
    TRANSITIONS = {  # Figure 42
        PENDING: {ALLOCATED, REJECTED, CANCELLED, EXPIRED},
        ALLOCATED: {ALLOCATED, CONSUMED, CANCELLED},
        CONSUMED: set(), REJECTED: set(), CANCELLED: set(), EXPIRED: set(),
    }

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class Reservation(models.Model):
    """A quantity hold: Pending Approval (expires after the hold period) or an
    Allocated approved quantity that never expires automatically (BR-08)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    # [UML: Association — ResourceRequest 1 —— 0..* Reservation "belongs-to" (Figures 16, 22)]
    request = models.ForeignKey(ResourceRequest, on_delete=models.PROTECT, related_name="reservations")
    # [UML: Association — Asset 1 —— 0..* Reservation "reserves" / "supplies" (Figures 16, 22)]
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    remaining_quantity = models.PositiveIntegerField(help_text="Allocated quantity not yet consumed by a confirmed receipt.")
    status = models.CharField(max_length=18, choices=ReservationStatus.CHOICES, default=ReservationStatus.PENDING)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True, help_text="When held stock was released back (rejection/cancellation/expiry).")
    release_reason = models.TextField(blank=True)
    flagged_for_review = models.BooleanField(default=False)
    flag_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=1), name="reservation_quantity_positive"),
            models.CheckConstraint(condition=Q(remaining_quantity__lte=F("quantity")), name="reservation_remaining_le_quantity"),
        ]
        indexes = [models.Index(fields=["asset", "status", "expires_at"], name="reservation_asset_status")]

    def __str__(self):
        return f"{self.reference} · {self.quantity} × {self.asset.asset_tag}"

    @property
    def status_label(self):
        return ReservationStatus.label(self.status)

    @property
    def is_expired_now(self):
        return self.status == ReservationStatus.PENDING and self.expires_at and self.expires_at <= timezone.now()

    # --- Class-diagram operations (Figure 16) ---
    def allocate(self):
        """Reservation.allocate(): only an approved current proposal version allocates a hold."""
        raise PermissionError("Holds are allocated only by approving the proposal version (Approval.decide).")

    def release_once(self, status, actor, reason):
        """Reservation.releaseOnce(): release a pending hold exactly once (rejected/cancelled/expired)."""
        from . import services

        return services.release_hold(self, status, actor, reason)


# ------------------------------------------------------- bonus: wishlist alerts ---
class SavedSearch(models.Model):
    """A requester's standing wish ("notify me when an HDMI monitor in Good condition is published")."""

    MAX_PER_USER = 10

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — User 1 "saves" 0..* SavedSearch]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_searches")
    name = models.CharField(max_length=80)
    keywords = models.CharField(max_length=100, blank=True, help_text="Every word must appear in the item's name, description, tags or specifications.")
    category = models.ForeignKey("catalog.Category", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    min_condition = models.CharField(max_length=12, choices=Condition.CHOICES, blank=True)
    min_quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_matched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def matches(self, asset, free):
        """Pure rule check — no AI. Category includes its sub-categories."""
        if free < self.min_quantity:
            return False
        if self.category_id and self.category_id not in (asset.category_id, asset.category.parent_id):
            return False
        if self.min_condition and Condition.RANK.get(asset.condition, 0) < Condition.RANK.get(self.min_condition, 0):
            return False
        haystack = " ".join([asset.name, asset.description, asset.tags, asset.specifications, asset.category.name]).lower()
        return all(word in haystack for word in self.keywords.lower().split())


class WishlistHit(models.Model):
    """One notification per (saved search, asset) — republishing never spams the same alert."""

    search = models.ForeignKey(SavedSearch, on_delete=models.CASCADE, related_name="hits")
    asset = models.ForeignKey("catalog.Asset", on_delete=models.CASCADE, related_name="+")
    notified_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["search", "asset"], name="uniq_wishlist_hit")]
