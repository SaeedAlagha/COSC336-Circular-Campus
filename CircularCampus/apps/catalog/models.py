"""D2 Assets and attachments (design 5.3.2, Figures 19 and 21).

An Asset row is one physical unit or a homogeneous batch with one condition,
custody and location (BR-03). Holds are quantity-level records in D3; the
batch status machine is documented in docs/DESIGN_MAPPING.md (Figure 37/43).
"""
import uuid
from datetime import date

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Category(models.Model):
    """Category tree: a NULL parent is a top-level category; children are subcategories."""

    IT = "IT"
    FURNITURE = "FURNITURE"
    KINDS = [(IT, "IT equipment (ITAsset metadata)"), (FURNITURE, "Furniture (FurnitureAsset metadata)"), ("GENERAL", "General")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80)
    # [UML: Association (reflexive) — Category 0..1 "contains" 0..* Category (Figure 21)]
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    kind = models.CharField(max_length=12, choices=KINDS, default="GENERAL", help_text="Selects the validated type-specific metadata (generalisation, Fig. 19).")
    keywords = models.CharField(max_length=300, blank=True, help_text="Comma-separated terms used by rule-based classification fallback.")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["parent__name", "name"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(fields=["parent", "name"], name="uniq_category_name_in_parent")]

    def __str__(self):
        return f"{self.parent.name} / {self.name}" if self.parent_id else self.name

    @property
    def root(self):
        return self.parent or self

    @property
    def effective_kind(self):
        return self.kind if self.kind != "GENERAL" or not self.parent_id else self.parent.kind


class Condition:
    NEW = "NEW"
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"
    CHOICES = [(NEW, "New"), (EXCELLENT, "Excellent"), (GOOD, "Good"), (FAIR, "Fair"), (POOR, "Poor")]
    RANK = {NEW: 5, EXCELLENT: 4, GOOD: 3, FAIR: 2, POOR: 1}

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)


class AssetStatus:
    DRAFT = "DRAFT"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    TRANSFER_PENDING = "TRANSFER_PENDING"
    IN_USE = "IN_USE"
    UNDER_INSPECTION = "UNDER_INSPECTION"
    IN_MAINTENANCE = "IN_MAINTENANCE"
    UNAVAILABLE = "UNAVAILABLE"
    DISPOSITION_PENDING = "DISPOSITION_PENDING"
    RETIRED = "RETIRED"
    COMPONENT_RECOVERED = "COMPONENT_RECOVERED"
    DONATED = "DONATED"
    RECYCLED = "RECYCLED"
    DISPOSED = "DISPOSED"
    CHOICES = [
        (DRAFT, "Draft"), (AVAILABLE, "Available"), (RESERVED, "Reserved"), (TRANSFER_PENDING, "Transfer Pending"),
        (IN_USE, "In Use"), (UNDER_INSPECTION, "Under Inspection"), (IN_MAINTENANCE, "In Maintenance"),
        (UNAVAILABLE, "Unavailable"), (DISPOSITION_PENDING, "Disposition Pending"), (RETIRED, "Retired"),
        (COMPONENT_RECOVERED, "Component Recovered"), (DONATED, "Donated"), (RECYCLED, "Recycled"), (DISPOSED, "Disposed"),
    ]
    LISTED = {AVAILABLE, RESERVED, TRANSFER_PENDING}
    RESTRICTED = {UNDER_INSPECTION, IN_MAINTENANCE, UNAVAILABLE, DISPOSITION_PENDING}
    FINAL = {COMPONENT_RECOVERED, DONATED, RECYCLED, DISPOSED}

    # Permitted moves (design Figure 37 + Figure 43). Additions that the
    # diagram leaves implicit are listed in docs/DESIGN_MAPPING.md §Deviations.
    TRANSITIONS = {
        DRAFT: {AVAILABLE, UNDER_INSPECTION},
        AVAILABLE: {RESERVED, TRANSFER_PENDING, UNDER_INSPECTION, IN_USE},
        RESERVED: {AVAILABLE, TRANSFER_PENDING, UNDER_INSPECTION, IN_USE},
        TRANSFER_PENDING: {AVAILABLE, RESERVED, IN_USE, UNDER_INSPECTION},
        IN_USE: {AVAILABLE, UNDER_INSPECTION, DISPOSITION_PENDING},
        UNDER_INSPECTION: {IN_USE, UNAVAILABLE, IN_MAINTENANCE},
        IN_MAINTENANCE: {IN_USE, UNDER_INSPECTION},
        UNAVAILABLE: {DISPOSITION_PENDING, UNDER_INSPECTION},
        DISPOSITION_PENDING: {RETIRED, COMPONENT_RECOVERED, DONATED, RECYCLED, DISPOSED, UNAVAILABLE},
        RETIRED: {DISPOSITION_PENDING},
        COMPONENT_RECOVERED: set(), DONATED: set(), RECYCLED: set(), DISPOSED: set(),
    }

    @classmethod
    def label(cls, code):
        return dict(cls.CHOICES).get(code, code)

    @classmethod
    def can_move(cls, old, new):
        return new in cls.TRANSITIONS.get(old, set())


TAG_VALIDATOR = RegexValidator(r"^[A-Z]{1,4}-[0-9]{2,6}(-[0-9]{1,3})*$", "Use an ID such as A-104 (letters, hyphen, digits).")


class Asset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset_tag = models.CharField("asset ID", max_length=24, unique=True, validators=[TAG_VALIDATOR])
    name = models.CharField(max_length=160)
    description = models.TextField()
    specifications = models.TextField(blank=True, help_text="Technical specifications, separated by semicolons.")
    tags = models.CharField(max_length=300, blank=True)
    # [UML: Aggregation — Category 1 ◇—— 0..* Asset "classifies" (Figure 21)]
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="assets")
    type_metadata = models.JSONField(encoder=DjangoJSONEncoder, default=dict, blank=True, help_text="Validated ITAsset/FurnitureAsset attributes.")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    # [UML: Aggregation — Department 1 ◇—— 0..* Asset "owns" (Figure 21); deactivate, never cascade-delete]
    owner_department = models.ForeignKey("accounts.Department", on_delete=models.PROTECT, related_name="assets")
    # [UML: Association — User 1 —— 0..* Asset "holds" custody (Figure 21)]
    custodian = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="custody_assets")
    campus = models.CharField(max_length=80, default="Main Campus")
    building = models.CharField(max_length=80)
    room = models.CharField(max_length=40)
    condition = models.CharField(max_length=12, choices=Condition.CHOICES)
    status = models.CharField(max_length=22, choices=AssetStatus.CHOICES, default=AssetStatus.DRAFT)
    is_published = models.BooleanField(default=False)
    # Historical facts stay NULL when unknown (FR-05, BR-06) — never invented.
    purchase_date = models.DateField(null=True, blank=True)
    unit_value_aed = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    replacement_value_aed = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)], help_text="Current new-purchase price per unit.")
    estimated_transfer_cost_aed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    unit_weight_kg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    available_from = models.DateField(null=True, blank=True)
    expected_retirement_date = models.DateField(null=True, blank=True, help_text="Baseline expected retirement (SUS-06).")
    revised_usable_until = models.DateField(null=True, blank=True, help_text="Supported revised usable-until date after reuse/repair.")
    # Provenance and metric de-duplication (SUS-09).
    # [UML: Association (reflexive) — Asset 0..1 parent batch of 0..* split batches (Figures 21, 26)]
    parent_batch = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="child_batches")
    reuse_counted = models.BooleanField(default=False, help_text="Units in this batch were already counted as reused.")
    cohort_entered_at = models.DateTimeField(null=True, blank=True, help_text="When these units first entered the unwanted/surplus cohort.")
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["asset_tag"]
        constraints = [models.CheckConstraint(condition=Q(quantity__gte=1), name="asset_quantity_positive")]
        indexes = [models.Index(fields=["category", "status", "owner_department"], name="asset_cat_status_dept")]

    def __str__(self):
        return f"{self.asset_tag} · {self.name}"

    @property
    def status_label(self):
        return AssetStatus.label(self.status)

    @property
    def condition_label(self):
        return Condition.label(self.condition)

    @property
    def condition_rank(self):
        return Condition.RANK.get(self.condition, 0)

    @property
    def location(self):
        return f"{self.campus} · {self.building} · Room {self.room}"

    def age_years(self, on=None):
        """Derived from purchase_date (never stored); None when unknown."""
        if not self.purchase_date:
            return None
        on = on or date.today()
        return round((on - self.purchase_date).days / 365.25, 1)

    @property
    def is_restricted(self):
        return self.status in AssetStatus.RESTRICTED

    @property
    def spec_list(self):
        return [s.strip() for s in self.specifications.replace("\n", ";").split(";") if s.strip()]

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    # --- Class-diagram operations (Figures 16, 17) ---
    def free_quantity(self):
        """Asset.freeQuantity(): eligible stock minus active holds and allocations (BR-07)."""
        from . import services

        return services.free_listed_quantity(self)

    def restrict(self, actor, reason):
        """Asset.restrict(reason): open a service record and remove the stock from reuse (FR-24)."""
        from apps.maintenance import services

        return services.report_defect(actor, self.pk, reason)


class Attachment(models.Model):
    """Photo/document linked to an asset; identified by (asset, attachment_no) (Fig. 21)."""

    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Composition — Asset 1 ◆—— 0..* Attachment "has"; key (asset_id, attachment_no) (Figure 21)]
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="attachments")
    attachment_no = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=[(PHOTO, "Photo"), (DOCUMENT, "Document")])
    original_name = models.CharField(max_length=200)
    storage_key = models.CharField(max_length=300, help_text="Path inside protected storage — not a public URL.")
    mime_type = models.CharField(max_length=40)
    size_bytes = models.PositiveIntegerField()
    checksum_sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["asset", "attachment_no"]
        constraints = [models.UniqueConstraint(fields=["asset", "attachment_no"], name="uniq_asset_attachment_no")]


# =====================================================================================
# [UML: Generalisation / Inheritance — Figure 19 "ITAsset specializes Asset",
#  "FurnitureAsset specializes Asset"]
# Proxy subclasses: real Python inheritance of Asset with no extra table, so there is
# no performance cost. Each subclass owns the validation of its own metadata.
# =====================================================================================
class ITAssetManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(models.Q(category__kind=Category.IT) | models.Q(category__parent__kind=Category.IT))


class FurnitureAssetManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(models.Q(category__kind=Category.FURNITURE) | models.Q(category__parent__kind=Category.FURNITURE))


class ITAsset(Asset):  # [UML: Inheritance — ITAsset is-a Asset (Figure 19)]
    """Figure 19 ITAsset: - serialNumber: String?  - ports: String[]"""

    METADATA_FIELDS = ("serial_number", "ports")
    objects = ITAssetManager()

    class Meta:
        proxy = True

    @property
    def serial_number(self):
        return self.type_metadata.get("serial_number")  # optional ("String?")

    @property
    def ports(self):
        return [p.strip() for p in self.type_metadata.get("ports", "").split(",") if p.strip()]


class FurnitureAsset(Asset):  # [UML: Inheritance — FurnitureAsset is-a Asset (Figure 19)]
    """Figure 19 FurnitureAsset: - material: String?  - dimensions: Dimensions?"""

    METADATA_FIELDS = ("material", "dimensions")
    objects = FurnitureAssetManager()

    class Meta:
        proxy = True

    @property
    def material(self):
        return self.type_metadata.get("material")

    @property
    def dimensions(self):
        return self.type_metadata.get("dimensions")


def specialise(asset):
    """Return the asset as its Figure 19 subclass (ITAsset / FurnitureAsset) or plain Asset."""
    kind = asset.category.effective_kind
    cls = {Category.IT: ITAsset, Category.FURNITURE: FurnitureAsset}.get(kind)
    if cls is None:
        return asset
    asset.__class__ = cls  # same row, specialised behaviour (no query)
    return asset


# =====================================================================================
# [UML: Aggregation — Figure 19 "AssetCollection ◇—— Asset (groups, 0..* to 0..*)"]
# A transient grouping of independent assets: removing the collection never deletes
# an asset (hollow diamond). Used by the catalog screen for search results.
# =====================================================================================
class AssetCollection:
    """Figure 19 AssetCollection: - results: Asset[]   + add(asset): void"""

    def __init__(self, assets=None):
        self.results = []  # [UML: Aggregation — holds references, does not own the assets]
        for a in assets or []:
            self.add(a)

    def add(self, asset):
        self.results.append(asset)

    def __iter__(self):
        return iter(self.results)

    def __len__(self):
        return len(self.results)
