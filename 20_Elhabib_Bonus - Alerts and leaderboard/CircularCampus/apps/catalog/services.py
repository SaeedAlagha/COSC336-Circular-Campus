"""Asset catalog component (process 2.0): registration, publication, stock balances,
status transitions, batch splitting and protected attachments."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone

from apps.accounts import policy
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.reporting import audit

from .models import Asset, AssetStatus, Attachment, Category, Condition

MATERIAL_FIELDS = ("condition", "status", "campus", "building", "room", "custodian_id", "owner_department_id", "quantity", "is_published")


# ------------------------------------------------------------ life-cycle log ---
def log_event(asset, actor, event_type, *, quantity=None, before=None, after=None, reason="", transfer=None, maintenance=None, disposition=None, correction_of=None):
    from apps.maintenance.models import LifeCycleEvent

    return LifeCycleEvent.objects.create(
        asset=asset, actor=actor if getattr(actor, "pk", None) else None, event_type=event_type, quantity=quantity,
        before=audit._clean(before or {}), after=audit._clean(after or {}), reason=reason,
        transfer=transfer, maintenance=maintenance, disposition=disposition, correction_of=correction_of,
    )


def snapshot(asset):
    return {f: getattr(asset, f) for f in MATERIAL_FIELDS}


# ----------------------------------------------------------------- statuses ---
def transition(asset, new_status, actor=None, reason="", event=True):
    """Move an asset batch along the designed state machine (C6) or refuse."""
    old = asset.status
    if old == new_status:
        return False
    if not AssetStatus.can_move(old, new_status):
        raise BusinessError(BusinessError.INVALID_TRANSITION,
                            f"{asset.asset_tag} cannot move from {AssetStatus.label(old)} to {AssetStatus.label(new_status)}.")
    asset.status = new_status
    asset.version += 1
    asset.save(update_fields=["status", "version", "updated_at"])
    if event:
        log_event(asset, actor, "STATUS_CHANGED", before={"status": old}, after={"status": new_status}, reason=reason)
    return True


@dataclass
class Balance:
    quantity: int
    pending: int
    allocated: int

    @property
    def committed(self):
        return self.pending + self.allocated

    @property
    def free(self):
        return max(0, self.quantity - self.committed)


def balance(asset) -> Balance:
    """BR-07: free = eligible stock − active pending holds − approved allocations."""
    from apps.exchange.models import ReservationStatus

    rows = asset.reservations.filter(status__in=ReservationStatus.ACTIVE).values("status").annotate(q=Sum("remaining_quantity"), p=Sum("quantity"))
    pending = allocated = 0
    for r in rows:
        if r["status"] == ReservationStatus.PENDING:
            pending += r["p"] or 0
        else:
            allocated += r["q"] or 0
    return Balance(asset.quantity, pending, allocated)


def free_listed_quantity(asset):
    """Quantity a requester may newly reserve: published, safe, uncommitted."""
    if not asset.is_published or asset.status not in AssetStatus.LISTED:
        return 0
    return balance(asset).free


def sync_listing_status(asset, actor=None, reason="Stock commitments changed"):
    """Keep Available / Reserved / Transfer Pending / In Use consistent with quantity holds."""
    if asset.status not in AssetStatus.LISTED:
        return  # restricted / in-use / final batches are not driven by holds
    b = balance(asset)
    if asset.is_published and b.free > 0:
        target = AssetStatus.AVAILABLE
    elif b.pending > 0:
        target = AssetStatus.RESERVED
    elif b.allocated > 0:
        target = AssetStatus.TRANSFER_PENDING
    else:
        target = AssetStatus.AVAILABLE if asset.is_published else AssetStatus.IN_USE
    if target != asset.status:
        transition(asset, target, actor, reason)


def lock_asset(asset_id):
    asset_id = V.parse_uuid(asset_id, "asset")
    try:
        return Asset.objects.select_for_update(of=("self",)).select_related("owner_department", "custodian", "category").get(pk=asset_id)
    except Asset.DoesNotExist:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That asset no longer exists.")


# ------------------------------------------------------------- registration ---
def clean_type_metadata(category, data):
    """Each Figure 19 subclass (ITAsset / FurnitureAsset) declares the metadata it accepts;
    every value is checked by its own rule (ports from an allow-list, serial/dimension formats)."""
    from .models import FurnitureAsset, ITAsset

    rules = {"serial_number": V.serial_number, "ports": V.ports, "material": V.material, "dimensions": V.dimensions}
    allowed = {"IT": ITAsset.METADATA_FIELDS, "FURNITURE": FurnitureAsset.METADATA_FIELDS}.get(category.effective_kind, ())
    clean = {}
    for key in allowed:
        value = rules[key]((data or {}).get(key, ""), key)
        if value:
            clean[key] = value
    return clean


def validate_asset_data(data, existing=None):
    """NFR-10: server-side rules for every asset field (forms only add convenience)."""
    from datetime import date

    out = dict(data)
    if "asset_tag" in data:
        out["asset_tag"] = V.text(data["asset_tag"], "asset_tag", label="Asset ID", required=True, max_len=24, needs_letter=False).upper()
    if "name" in data:
        out["name"] = V.text(data["name"], "name", label="Name", required=True, min_len=3, max_len=160)
    if "description" in data:
        out["description"] = V.text(data["description"], "description", label="Description", required=True, min_len=5, max_len=V.MAX_LONG, multiline=True)
    if "specifications" in data:
        out["specifications"] = V.text(data["specifications"], "specifications", label="Specifications", required=True, min_len=2, max_len=V.MAX_LONG, multiline=True, needs_letter=False)
    if "tags" in data:
        out["tags"] = ", ".join(V.keywords(data["tags"], "tags"))
    if "quantity" in data:
        out["quantity"] = V.integer(data["quantity"], "quantity", label="Quantity", min_value=1, max_value=V.MAX_ASSET_QTY)
    for f, lbl in (("campus", "Campus"), ("building", "Building"), ("room", "Room")):
        if f in data:
            out[f] = V.place(data[f], f, label=lbl, max_len=40 if f == "room" else 80)
    if "condition" in data:
        out["condition"] = V.choice(data["condition"], "condition", Condition.CHOICES, label="Condition")
    for f, lbl in (("unit_value_aed", "Current value"), ("replacement_value_aed", "New-purchase price"), ("estimated_transfer_cost_aed", "Transfer cost")):
        if f in data:
            out[f] = V.money(data[f], f, label=lbl)
    if "unit_weight_kg" in data:
        out["unit_weight_kg"] = V.decimal(data["unit_weight_kg"], "unit_weight_kg", label="Weight", max_value=V.MAX_WEIGHT_KG, allow_zero=False)
    today = date.today()
    purchase = V.a_date(data.get("purchase_date", getattr(existing, "purchase_date", None)), "purchase_date", label="Purchase date",
                        not_before=date(1970, 1, 1), not_after=today)
    if "purchase_date" in data:
        out["purchase_date"] = purchase
    if "available_from" in data:
        out["available_from"] = V.a_date(data["available_from"], "available_from", label="Ready for reuse from",
                                         not_before=purchase, not_after=V.years_from_today(2))
    if "expected_retirement_date" in data:
        out["expected_retirement_date"] = V.a_date(data["expected_retirement_date"], "expected_retirement_date", label="Expected retirement",
                                                   not_before=purchase, not_after=(purchase or today) + timedelta(days=30 * 365 + 7))
    unit = out.get("unit_value_aed", getattr(existing, "unit_value_aed", None))
    repl = out.get("replacement_value_aed", getattr(existing, "replacement_value_aed", None))
    if unit is not None and repl is not None and unit > repl:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "A used item cannot be worth more than buying it new.",
                            {"unit_value_aed": "Current value cannot exceed the new-purchase price."})
    return out


def register_asset(actor, data):
    """UC-01: create a Draft asset; publication is a separate step (UC-09)."""
    data = validate_asset_data(data)
    dept = data["owner_department"]
    policy.require(policy.can_manage_asset(actor, department=dept.pk), "Only an asset custodian for this department can register assets.", "register_asset")
    custodian = data.get("custodian") or actor
    if not policy.has_role(custodian, "CUSTODIAN", dept.pk):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The custodian must hold the Asset Custodian role for this department.", {"custodian": "Choose a custodian of this department."})
    if Asset.objects.filter(asset_tag=data["asset_tag"]).exists():
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That asset ID is already used.", {"asset_tag": "Choose a unique asset ID."})
    with transaction.atomic():
        asset = Asset(created_by=actor, custodian=custodian, status=AssetStatus.DRAFT)
        for field in ("asset_tag", "name", "description", "specifications", "tags", "category", "quantity", "owner_department",
                      "campus", "building", "room", "condition", "purchase_date", "unit_value_aed", "replacement_value_aed",
                      "estimated_transfer_cost_aed", "unit_weight_kg", "available_from", "expected_retirement_date"):
            if field in data:
                setattr(asset, field, data[field])
        asset.type_metadata = clean_type_metadata(asset.category, data.get("type_metadata"))
        asset.full_clean()
        asset.save()
        log_event(asset, actor, "REGISTERED", quantity=asset.quantity, after=snapshot(asset), reason="Asset registered as Draft")
        audit.record(actor, "Asset registered", "asset", asset, after=snapshot(asset))
        return asset


EDITABLE_ALWAYS = ("name", "description", "specifications", "tags", "category", "purchase_date", "unit_value_aed", "replacement_value_aed",
                   "estimated_transfer_cost_aed", "unit_weight_kg", "available_from", "expected_retirement_date")
CORRECTION_FIELDS = ("condition", "quantity", "campus", "building", "room")


def update_asset(actor, asset_id, expected_version, data, reason=""):
    """Edit metadata. Custody, department and status can never change here (FR-05);
    location/quantity/condition corrections need a reason and no active commitments."""
    with transaction.atomic():
        asset = lock_asset(asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can edit the asset.", "update_asset", asset)
        if asset.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "Someone else changed this asset after you opened it. Reload and review the current values.")
        data = validate_asset_data(data, asset)
        reason = V.reason(reason, required=False, label="Reason for correction")
        if asset.status in AssetStatus.FINAL:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Completed dispositions are read-only; record a correction event instead.")
        before = {f: getattr(asset, f) for f in EDITABLE_ALWAYS + CORRECTION_FIELDS}
        corrections = {f: data[f] for f in CORRECTION_FIELDS if f in data and data[f] != getattr(asset, f)}
        if corrections:
            if asset.status != AssetStatus.DRAFT and len(reason.strip()) < 8:
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Explain why the condition, location or quantity is being corrected.", {"reason": "Give a reason of at least 8 characters."})
            if balance(asset).committed and ({"quantity", "campus", "building", "room"} & corrections.keys()):
                raise BusinessError(BusinessError.STOCK_CONFLICT, "This batch has active holds or allocations; location and quantity cannot change until they are resolved. Use the transfer workflow to move stock.")
        for f in EDITABLE_ALWAYS:
            if f in data:
                setattr(asset, f, data[f])
        for f, v in corrections.items():
            setattr(asset, f, v)
        asset.type_metadata = clean_type_metadata(asset.category, data.get("type_metadata", asset.type_metadata))
        asset.version += 1
        asset.full_clean()
        asset.save()
        after = {f: getattr(asset, f) for f in EDITABLE_ALWAYS + CORRECTION_FIELDS}
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        if changed:
            event = "CORRECTION" if corrections and asset.status != AssetStatus.DRAFT else "UPDATED"
            log_event(asset, actor, event, before={k: before[k] for k in changed}, after=changed, reason=reason or "Metadata updated")
            audit.record(actor, "Asset updated", "asset", asset, before={k: before[k] for k in changed}, after=changed, reason=reason)
        return asset


def publish(actor, asset_id, expected_version, available_from=None):
    """UC-09: publish eligible stock (Draft/In Use -> Available)."""
    with transaction.atomic():
        asset = lock_asset(asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can publish the asset.", "publish", asset)
        if asset.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This asset changed after you opened it. Reload and try again.")
        if asset.is_restricted or asset.status in AssetStatus.FINAL | {AssetStatus.RETIRED}:
            raise BusinessError(BusinessError.UNSAFE_STOCK, "Only safe stock that is not under inspection, maintenance or disposition can be published.")
        if asset.condition == Condition.POOR:
            raise BusinessError(BusinessError.UNSAFE_STOCK, "Poor-condition stock must be inspected before it can be offered for reuse.")
        if asset.is_published:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "This asset is already published.")
        asset.is_published = True
        if available_from:
            asset.available_from = available_from
        elif not asset.available_from:
            asset.available_from = timezone.localdate()
        cohort_entry = asset.cohort_entered_at is None
        if cohort_entry:
            asset.cohort_entered_at = timezone.now()
        asset.version += 1
        asset.save()
        if asset.status in (AssetStatus.DRAFT, AssetStatus.IN_USE):
            transition(asset, AssetStatus.AVAILABLE, actor, "Published surplus", event=False)
        sync_listing_status(asset, actor)
        log_event(asset, actor, "PUBLISHED", quantity=asset.quantity,
                  after={"is_published": True, "status": asset.status, "cohort_entry": cohort_entry, "department": str(asset.owner_department_id)})
        audit.record(actor, "Asset published", "asset", asset, after={"is_published": True, "free": balance(asset).free})
        from apps.exchange.services import notify_wishlists

        notify_wishlists(asset, actor)  # bonus: availability alerts
        return asset


def unpublish(actor, asset_id, expected_version, reason=""):
    """FR-07: removes free stock from search; existing holds/allocations are preserved, never cancelled silently."""
    with transaction.atomic():
        asset = lock_asset(asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can unpublish the asset.", "unpublish", asset)
        if asset.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "This asset changed after you opened it. Reload and try again.")
        if not asset.is_published:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "This asset is not published.")
        reason = V.reason(reason, required=False)
        b = balance(asset)
        asset.is_published = False
        asset.version += 1
        asset.save()
        sync_listing_status(asset, actor, "Unpublished")
        log_event(asset, actor, "UNPUBLISHED", quantity=b.free, after={"is_published": False, "status": asset.status, "commitments_preserved": b.committed}, reason=reason)
        audit.record(actor, "Asset unpublished", "asset", asset, after={"commitments_preserved": b.committed}, reason=reason)
        return asset, b.committed


# ------------------------------------------------------------ batch splitting ---
def next_child_tag(parent):
    n = 1
    while Asset.objects.filter(asset_tag=f"{parent.asset_tag}-{n}").exists():
        n += 1
    return f"{parent.asset_tag}-{n}"


def split_batch(asset, quantity, actor, reason, *, status, kind="SPLIT", **overrides):
    """Split ``quantity`` units into a linked child batch (BR-03); total quantity is conserved."""
    from apps.maintenance.models import AssetLineage

    if quantity >= asset.quantity:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "A split must leave at least one unit in the source batch.")
    child = Asset(
        asset_tag=next_child_tag(asset), name=asset.name, description=asset.description, specifications=asset.specifications,
        tags=asset.tags, category=asset.category, type_metadata=dict(asset.type_metadata), quantity=quantity,
        owner_department=asset.owner_department, custodian=asset.custodian, campus=asset.campus, building=asset.building,
        room=asset.room, condition=asset.condition, status=status, is_published=False, purchase_date=asset.purchase_date,
        unit_value_aed=asset.unit_value_aed, replacement_value_aed=asset.replacement_value_aed,
        estimated_transfer_cost_aed=asset.estimated_transfer_cost_aed, unit_weight_kg=asset.unit_weight_kg,
        expected_retirement_date=asset.expected_retirement_date, revised_usable_until=asset.revised_usable_until,
        parent_batch=asset, reuse_counted=asset.reuse_counted, cohort_entered_at=asset.cohort_entered_at, created_by=actor,
    )
    for k, v in overrides.items():
        setattr(child, k, v)
    child.save()
    asset.quantity -= quantity
    asset.version += 1
    asset.save(update_fields=["quantity", "version", "updated_at"])
    ev = log_event(asset, actor, "SPLIT", quantity=quantity, before={"quantity": asset.quantity + quantity},
                   after={"quantity": asset.quantity, "child": child.asset_tag}, reason=reason)
    log_event(child, actor, "SPLIT", quantity=quantity, after={"parent": asset.asset_tag, "quantity": quantity}, reason=reason)
    AssetLineage.objects.create(parent_asset=asset, child_asset=child, event=ev, kind=kind, quantity=quantity)
    return child


# --------------------------------------------------------------- attachments ---
SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/pdf": (b"%PDF-",),
}
EXTENSIONS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".pdf": "application/pdf"}


def validate_upload(upload):
    """FR-06 / Phase 3 §6.2: JPEG, PNG or PDF only, 10 MB maximum, checked by content."""
    name = os.path.basename((upload.name or "file").replace("\\", "/"))
    name = V._CONTROL.sub("", name).strip()[:150] or "file"
    name = V.re.sub(r"[<>:\"|?*]", "_", name)
    ext = os.path.splitext(name)[1].lower()
    if ext not in EXTENSIONS:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Only JPEG, PNG or PDF files are accepted.", {"file": "Upload a .jpg, .png or .pdf file."})
    if upload.size > settings.ATTACHMENT_MAX_BYTES:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Files must be 10 MB or smaller.", {"file": "This file is larger than 10 MB."})
    if upload.size == 0:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The file is empty.", {"file": "Choose a non-empty file."})
    head = upload.read(16)
    upload.seek(0)
    mime = EXTENSIONS[ext]
    if not any(head.startswith(sig) for sig in SIGNATURES[mime]):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The file content does not match its type.", {"file": "The file is not a valid JPEG, PNG or PDF."})
    return name, mime


def add_attachment(actor, asset_id, upload):
    with transaction.atomic():
        asset = lock_asset(asset_id)
        policy.require(policy.can_manage_asset(actor, asset), "Only this department's custodian can add attachments.", "add_attachment", asset)
        name, mime = validate_upload(upload)
        digest = hashlib.sha256()
        key = f"{asset.pk}/{uuid.uuid4().hex}{os.path.splitext(name)[1].lower()}"
        path = settings.PROTECTED_MEDIA_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(path, "wb") as fh:
            for chunk in upload.chunks():
                digest.update(chunk)
                size += len(chunk)
                fh.write(chunk)
        try:
            number = (asset.attachments.order_by("-attachment_no").values_list("attachment_no", flat=True).first() or 0) + 1
            att = Attachment.objects.create(
                asset=asset, attachment_no=number, kind=Attachment.PHOTO if mime.startswith("image/") else Attachment.DOCUMENT,
                original_name=name[:200], storage_key=key, mime_type=mime, size_bytes=size, checksum_sha256=digest.hexdigest(), uploaded_by=actor,
            )
            log_event(asset, actor, "ATTACHMENT_ADDED", after={"attachment_no": number, "name": name, "sha256": att.checksum_sha256})
            audit.record(actor, "Attachment added", "asset", asset, after={"attachment_no": number, "mime": mime, "size": size})
        except Exception:
            path.unlink(missing_ok=True)  # no orphaned file when the record is not saved
            raise
        return att


def attachment_path(att):
    return settings.PROTECTED_MEDIA_ROOT / att.storage_key


# -------------------------------------------------------------------- search ---
def asset_url(asset):
    return reverse("catalog:asset", args=[asset.pk])


def category_choices():
    return Category.objects.filter(active=True).select_related("parent")


def today():
    return date.today()
