"""AI feature 2 — Matching and ranking (UC-05; design 5.10).

1. Hard filters run first (visibility, status, safety, condition, mandatory
   specifications, date, quantity). Semantic similarity can never resurrect a
   candidate that failed one.
2. Semantic suitability S comes from Gemini embeddings (cached per input hash
   and model version) or, when Gemini is unavailable, from the labelled local
   concept-similarity fallback.
3. The application computes the final 0–100 suitability deterministically from
   the designed weights. Missing non-critical features are excluded from both
   numerator and denominator and lower the evidence coverage.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts import policy
from apps.catalog import services as catalog
from apps.catalog.models import Asset, AssetStatus, Condition
from apps.exchange.models import Urgency


WEIGHTS = {"S": Decimal("0.30"), "C": Decimal("0.20"), "Q": Decimal("0.15"), "L": Decimal("0.10"), "T": Decimal("0.10"), "K": Decimal("0.10"), "E": Decimal("0.05")}
LABELS = {"S": "Purpose & specification fit", "C": "Condition fit", "Q": "Quantity fit", "L": "Location fit", "T": "Date & urgency fit", "K": "Cost fit", "E": "Sustainability fit"}
COVERAGE_MIN = Decimal("0.70")
WEAK_BELOW = Decimal("70")
TOP_N = 5


def _norm(text):
    return " ".join((text or "").lower().replace("-", " ").split())


def asset_text(a):
    return f"{a.name}. {a.category}. {a.description} Specifications: {a.specifications}. Tags: {a.tags}"


def request_text(r):
    return f"{r.title}. {r.category}. Purpose: {r.purpose} Specifications: {r.specifications}. Must have: {r.mandatory_specs}"


def hard_filter(asset, req, ignore_quantity=False):
    """Mandatory constraints (BR-12, FR-14, design 5.10). Returns (eligible, reasons)."""
    reasons = []
    if not asset.is_published or asset.status not in AssetStatus.LISTED:
        reasons.append(f"not published as available stock ({asset.status_label})")
    if asset.is_restricted:
        reasons.append("restricted for inspection/safety")
    if asset.reservations.filter(flagged_for_review=True, status__in=["PENDING_APPROVAL", "ALLOCATED"]).exists():
        reasons.append("held stock flagged for safety review")
    if Condition.RANK.get(asset.condition, 0) < Condition.RANK.get(req.preferred_condition, 0):
        reasons.append(f"condition {asset.condition_label} is below the minimum {Condition.label(req.preferred_condition)}")
    haystack = _norm(" ".join([asset.name, asset.description, asset.specifications, asset.tags]))
    for term in req.mandatory_list:
        if _norm(term) not in haystack:
            reasons.append(f"mandatory specification “{term}” not stated (needs inspection or clarification)")
    if asset.available_from and req.required_date and asset.available_from > req.required_date:
        reasons.append(f"ready {asset.available_from:%d %b} — after the required date")
    if not ignore_quantity:
        free = catalog.free_listed_quantity(asset)
        from apps.exchange.services import remaining_need

        need = max(1, remaining_need(req))
        if free < 1:
            reasons.append("no free units")
        elif free < need and not req.allow_partial:
            reasons.append(f"only {free} free of {need} needed and partial fulfilment is not allowed")
    return (not reasons), reasons
