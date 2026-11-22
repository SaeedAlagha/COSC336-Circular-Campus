"""D7 Reports and impacts; D8 Audit and notices (design 5.3.10/5.3.11, Figures 29/30)."""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class EmissionFactor(models.Model):
    """Carbon factors entered by a Sustainability Officer with a cited source.

    None are seeded: without an approved factor, carbon reduction is shown as
    unavailable (SUS-07/SUS-08, BR-06) rather than estimated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="emission_factors")
    replacement_kgco2e_per_unit = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    reuse_transport_kgco2e_per_transfer = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    source = models.CharField(max_length=300, help_text="Citation for the factor (publication, dataset, page).")
    version = models.CharField(max_length=40)
    boundary = models.CharField(max_length=200, default="Cradle-to-gate replacement vs. one internal transfer trip")
    is_synthetic = models.BooleanField(default=False, help_text="Tick when the factor is illustrative demo data, not a verified source.")
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]


class AvoidedPurchaseReview(models.Model):
    """SUS-03: an avoided purchase counts only when Procurement/Finance confirms the
    completed reuse replaced a needed purchase. One attribution per transfer (SUS-09)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    transfer = models.OneToOneField("workflow.Transfer", on_delete=models.PROTECT, related_name="avoided_purchase")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    confirmed = models.BooleanField(help_text="False records that the reuse did not replace a needed purchase.")
    substituted_item = models.CharField(max_length=160, blank=True)
    quantity = models.PositiveIntegerField(null=True, blank=True)
    reference_unit_price_aed = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    price_reference = models.CharField(max_length=200, blank=True, help_text="Quote, catalogue or PO reference for the price.")
    related_repair_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    related_other_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(default=timezone.now)

    @property
    def avoided_value(self):
        if not self.confirmed or self.quantity is None or self.reference_unit_price_aed is None:
            return None
        return self.quantity * self.reference_unit_price_aed


class SustainabilityImpact(models.Model):
    """One evidence-linked contribution to an indicator (event + asset), used for de-duplication."""

    REUSE = "REUSE"
    DIVERSION = "DIVERSION"
    REPAIR = "REPAIR"
    AVOIDED_PURCHASE = "AVOIDED_PURCHASE"
    LIFE_EXTENSION = "LIFE_EXTENSION"
    INDICATORS = [(REUSE, "Reuse"), (DIVERSION, "Waste diversion"), (REPAIR, "Repair / refurbishment"), (AVOIDED_PURCHASE, "Avoided purchase"), (LIFE_EXTENSION, "Life extension")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey("catalog.Asset", on_delete=models.PROTECT, related_name="impacts")
    # [UML: Association — LifeCycleEvent 1 "supports" 0..* SustainabilityImpact (Figure 29)]
    event = models.ForeignKey("maintenance.LifeCycleEvent", on_delete=models.PROTECT, related_name="impacts")
    indicator = models.CharField(max_length=20, choices=INDICATORS)
    quantity = models.PositiveIntegerField(default=0)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    unit = models.CharField(max_length=20)
    department = models.ForeignKey("accounts.Department", on_delete=models.PROTECT, related_name="+", help_text="Department credited (receiving or owning).")
    source_department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT, related_name="+", help_text="Department that owned the units when they entered the cohort.")
    cohort_entered_at = models.DateTimeField(null=True, blank=True)
    counted = models.BooleanField(default=True, help_text="False when these units were already counted (repeat transfer).")
    status = models.CharField(max_length=12, default="VERIFIED", choices=[("VERIFIED", "Verified record"), ("PROVISIONAL", "Provisional estimate")])
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [models.UniqueConstraint(fields=["event", "indicator"], name="uniq_impact_per_event_indicator")]


class Report(models.Model):
    MONTHLY = "MONTHLY_EXCHANGE"
    SUSTAINABILITY = "SUSTAINABILITY_UPDATE"
    BULLETIN = "MANAGEMENT_BULLETIN"
    DEMAND = "HIGH_DEMAND"
    MAINTENANCE = "MAINTENANCE_TRENDS"
    PURCHASES = "AVOIDED_PURCHASES"
    TYPES = [(MONTHLY, "Monthly exchange summary"), (SUSTAINABILITY, "Sustainability update"), (BULLETIN, "Management bulletin"), (DEMAND, "High-demand categories"), (MAINTENANCE, "Maintenance trends"), (PURCHASES, "Avoided purchases & savings")]

    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    PUBLISHED = "PUBLISHED"
    STATUSES = [(DRAFT, "Draft — not published"), (REVIEWED, "Reviewed — ready to publish"), (PUBLISHED, "Published")]

    AI = "AI"
    TEMPLATE = "TEMPLATE"
    MANUAL = "MANUAL"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    report_type = models.CharField(max_length=24, choices=TYPES)
    period_start = models.DateField()
    period_end = models.DateField()
    scope_department = models.ForeignKey("accounts.Department", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    snapshot = models.JSONField(encoder=DjangoJSONEncoder, help_text="Immutable deterministic totals the narrative must follow.")
    snapshot_hash = models.CharField(max_length=64)
    snapshot_version = models.CharField(max_length=20, default="metrics-v1")
    # [UML: Association — User 1 "generates" 0..* Report (Figure 29)]
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reports_generated")
    generated_at = models.DateTimeField(default=timezone.now)
    ai_result = models.ForeignKey("intelligence.AIResult", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    narrative_source = models.CharField(max_length=10, blank=True, choices=[(AI, "Gemini draft"), (TEMPLATE, "Template draft (not AI)"), (MANUAL, "Written manually")])
    narrative_original = models.TextField(blank=True, help_text="Draft exactly as generated, preserved for provenance.")
    narrative_text = models.TextField(blank=True)
    validation = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=DRAFT)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-generated_at"]

    def __str__(self):
        return f"{self.reference} · {self.get_report_type_display()}"


class ReportMetric(models.Model):
    """Report–impact link freezing the displayed value (Figure 29)."""

    # [UML: Composition — Report 1 ◆—— 0..* ReportMetric "includes" (Figure 29); links are deleted with the report]
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="metrics")
    # [UML: Association — SustainabilityImpact 1 "supports" 0..* ReportMetric (Figure 29)]
    impact = models.ForeignKey(SustainabilityImpact, on_delete=models.PROTECT, related_name="report_links")
    display_value_snapshot = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["report", "impact"], name="uniq_report_impact")]


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — User 1 "receives" 0..* Notification (Figure 30)]
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="notifications")
    event_code = models.CharField(max_length=40)
    title = models.CharField(max_length=160)
    body = models.CharField(max_length=400, blank=True)
    record_type = models.CharField(max_length=30)
    record_id = models.CharField(max_length=40)
    link = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    delivery_state = models.CharField(max_length=10, default="DELIVERED")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "read_at"], name="notice_recipient_read")]

    @property
    def is_read(self):
        return self.read_at is not None


class AuditEvent(models.Model):
    """Append-only audit trail (FR-45). Updates and deletes are refused."""

    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    FAILED = "FAILED"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — User 0..1 "triggers" 0..* AuditEvent; else a named service principal (Figure 30)]
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    service_actor = models.CharField(max_length=40, blank=True, help_text="Named service principal when no human actor (e.g. hold-expiry job).")
    action = models.CharField(max_length=60)
    record_type = models.CharField(max_length=30)
    record_id = models.CharField(max_length=40, blank=True)
    record_label = models.CharField(max_length=80, blank=True)
    before = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    after = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True)
    outcome = models.CharField(max_length=8, default=SUCCESS)
    reason = models.TextField(blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    created_at = models.DateTimeField(default=timezone.now)

    TECHNICAL_TYPES = {"account", "department", "role_grant", "configuration", "access"}

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["record_type", "record_id", "created_at"], name="audit_record_time")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit events are append-only.")
        if not self.actor_id and not self.service_actor:
            raise ValidationError("An audit event needs a human actor or a named service principal.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events cannot be deleted.")
