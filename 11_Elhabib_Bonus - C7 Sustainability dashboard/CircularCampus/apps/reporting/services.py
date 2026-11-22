"""Reporting component (process 7.0): immutable report snapshots, AI narrative
draft → human edit → explicit review → separate publication (AI feature 5, UC-06),
avoided-purchase confirmations (SUS-03), emission factors and CSV safety."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts import policy
from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.refs import next_reference
from apps.workflow.models import TransferStatus

from . import audit, metrics
from .models import AvoidedPurchaseReview, EmissionFactor, Report, ReportMetric


# ------------------------------------------------------------- validation ---
# -------------------------------------------------- avoided purchase review ---
def review_avoided_purchase(actor, transfer, confirmed, substituted_item="", quantity=None, unit_price=None, price_reference="", repair_cost=None, other_cost=None, note=""):
    req = transfer.reservation.request
    policy.require(policy.can_review_avoided_purchase(actor, req.department_id), "Only Procurement or Finance officers for the receiving department can confirm avoided purchases.", "avoided_purchase", transfer)
    if transfer.status != TransferStatus.COMPLETED:
        raise BusinessError(BusinessError.INVALID_TRANSITION, "Only completed transfers can be reviewed for avoided purchases.")
    if hasattr(transfer, "avoided_purchase"):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "This transfer already has an avoided-purchase decision (no double attribution).")
    errors = {}

    def take(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except BusinessError as exc:
            errors.update(exc.field_errors or {a[1]: exc.message})
            return None

    substituted_item = take(V.text, substituted_item, "substituted_item", label="Substituted item", max_len=160) or ""
    price_reference = take(V.text, price_reference, "price_reference", label="Price reference", max_len=200, needs_letter=False) or ""
    note = take(V.text, note, "note", label="Note", max_len=V.MAX_SHORT * 2, multiline=True) or ""
    quantity = take(V.integer, quantity, "quantity", label="Quantity", required=False, min_value=1, max_value=V.MAX_ASSET_QTY)
    unit_price = take(V.money, unit_price, "unit_price", label="Unit price")
    repair_cost = take(V.money, repair_cost, "repair_cost", label="Repair cost")
    other_cost = take(V.money, other_cost, "other_cost", label="Other cost")
    if confirmed and not errors:
        if len(substituted_item) < 3:
            errors["substituted_item"] = "Name the purchase that was avoided."
        if not quantity or quantity > (transfer.received_quantity or 0):
            errors["quantity"] = f"Between 1 and the {transfer.received_quantity} units received."
        if unit_price is None or unit_price <= 0:
            errors["unit_price"] = "Enter the reference unit price in AED."
        if len(price_reference) < 3:
            errors["price_reference"] = "Cite the quote, catalogue or PO used for the price."
    if errors:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Complete the avoided-purchase evidence.", errors)
    with transaction.atomic():
        r = AvoidedPurchaseReview.objects.create(transfer=transfer, reviewer=actor, confirmed=confirmed, substituted_item=substituted_item,
                                                 quantity=quantity if confirmed else None, reference_unit_price_aed=unit_price if confirmed else None,
                                                 price_reference=price_reference, related_repair_cost_aed=repair_cost, related_other_cost_aed=other_cost, note=note)
        audit.record(actor, "Avoided purchase reviewed", "transfer", transfer, after={"confirmed": confirmed, "value": r.avoided_value})
        return r


def add_emission_factor(actor, category, replacement, transport, source, version, is_synthetic):
    policy.require(policy.can_manage_factors(actor), "Only sustainability officers can record emission factors.", "emission_factor")
    if len((source or "").strip()) < 8 or len((version or "").strip()) < 1:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Cite the factor source and version.", {"source": "At least 8 characters."})
    source = V.text(source, "source", label="Source", required=True, min_len=8, max_len=300)
    version = V.text(version, "version", label="Version", required=True, max_len=40, needs_letter=False)
    replacement = V.decimal(replacement, "replacement", label="Replacement factor (kg CO2e/unit)", required=True, max_value=Decimal("100000"))
    transport = V.decimal(transport, "transport", label="Transport factor (kg CO2e/transfer)", required=True, max_value=Decimal("100000"))
    if transport >= replacement and replacement > 0:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The transport factor should be smaller than the replacement factor; check the units.", {"transport": "Must be less than the replacement factor."})
    with transaction.atomic():
        f = EmissionFactor.objects.create(category=category, replacement_kgco2e_per_unit=replacement, reuse_transport_kgco2e_per_transfer=transport, source=source.strip(),
                                          version=version.strip(), is_synthetic=is_synthetic, entered_by=actor)
        audit.record(actor, "Emission factor recorded", "configuration", f, after={"category": str(category), "version": version, "synthetic": is_synthetic})
        return f


# ------------------------------------------------------------------- CSV ---
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formula injection (CWE-1236)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(DANGEROUS) else text


def to_csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([csv_safe(h) for h in header])
    for row in rows:
        writer.writerow([csv_safe(v) for v in row])
    return "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 correctly
