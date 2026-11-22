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

from .gateway import AIGateway, input_hash, model_version
from .models import AssetRepresentation, MatchRecommendation, ModelVersion
from .similarity import cosine, similarity_matrix

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


def _same_category(asset_cat, req_cat):
    """The requested category, a sub-category of it, or its parent group."""
    if asset_cat is None:
        return False
    return asset_cat.pk == req_cat.pk or asset_cat.parent_id == req_cat.pk or (req_cat.parent_id is not None and asset_cat.pk == req_cat.parent_id)


def hard_filter(asset, req, ignore_quantity=False):
    """Mandatory constraints (BR-12, FR-14, design 5.10). Returns (eligible, reasons)."""
    reasons = []
    if not asset.is_published or asset.status not in AssetStatus.LISTED:
        reasons.append(f"not published as available stock ({asset.status_label})")
    if asset.is_restricted:
        reasons.append("restricted for inspection/safety")
    if asset.reservations.filter(flagged_for_review=True, status__in=["PENDING_APPROVAL", "ALLOCATED"]).exists():
        reasons.append("held stock flagged for safety review")
    if req.category_id and not _same_category(asset.category, req.category):
        reasons.append(f"category {asset.category} does not match the requested {req.category}")
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


def _emission_factor(category):
    from apps.reporting.models import EmissionFactor

    return EmissionFactor.objects.filter(active=True, category__in=[c for c in (category, category.parent) if c]).order_by("-created_at").first()


def features(asset, req, s_value, need, free):
    comps, missing, reasons, limits, codes = {}, [], [], [], []
    comps["S"] = round(float(s_value), 3)
    reasons.append(f"Purpose/spec similarity {s_value:.2f}")
    if req.mandatory_list:
        reasons.append("all mandatory specifications stated: " + ", ".join(req.mandatory_list))
        codes.append("MANDATORY_SPECS_PASS")
    # Condition
    if asset.condition_rank > Condition.RANK[req.preferred_condition]:
        comps["C"] = 1.0
        reasons.append(f"condition {asset.condition_label} exceeds minimum {Condition.label(req.preferred_condition)}")
    else:
        comps["C"] = 0.8
        reasons.append(f"condition {asset.condition_label} meets the minimum")
    # Quantity
    q = min(free / need, 1.0) if need else 1.0
    comps["Q"] = round(q, 3)
    reasons.append(f"{free} free unit(s) for a need of {need}" + ("" if q >= 1 else " — partial supply"))
    if q < 1:
        codes.append("PARTIAL_SUPPLY")
    # Location
    if not asset.campus or not asset.building or not req.destination_building:
        missing.append("L")
        limits.append("location unknown")
    elif _norm(asset.campus) == _norm(req.destination_campus) and _norm(asset.building) == _norm(req.destination_building):
        comps["L"] = 1.0
        reasons.append("same building")
    elif _norm(asset.campus) == _norm(req.destination_campus):
        comps["L"] = 0.7
        reasons.append(f"same campus ({asset.building})")
    else:
        comps["L"] = 0.4
        reasons.append(f"other campus ({asset.campus})")
    # Date & urgency
    if not asset.available_from:
        missing.append("T")
        limits.append("readiness date unknown")
    else:
        buffer = timedelta(days=Urgency.BUFFER_DAYS.get(req.urgency, 1))
        if asset.available_from <= req.required_date - buffer:
            comps["T"] = 1.0
            reasons.append(f"ready {asset.available_from:%d %b}, inside the {req.get_urgency_display().lower()}-urgency buffer")
        else:
            comps["T"] = 0.7
            reasons.append(f"ready {asset.available_from:%d %b}, close to the required date")
    # Cost
    take = min(free, need) or 1
    if asset.estimated_transfer_cost_aed is None or not asset.replacement_value_aed:
        missing.append("K")
        limits.append("transfer cost or replacement value unknown")
    else:
        replacement = asset.replacement_value_aed * take
        k = max(Decimal(0), min(Decimal(1), 1 - asset.estimated_transfer_cost_aed / replacement))
        comps["K"] = round(float(k), 3)
        reasons.append(f"transfer AED {asset.estimated_transfer_cost_aed:,.0f} vs new AED {replacement:,.0f}")
    # Sustainability
    factor = _emission_factor(asset.category)
    if asset.cohort_entered_at is None or factor is None or factor.replacement_kgco2e_per_unit <= 0:
        missing.append("E")
        limits.append("sustainability evidence unavailable (no approved carbon factor)")
    else:
        repl = factor.replacement_kgco2e_per_unit * take
        carbon_fit = max(Decimal(0), min(Decimal(1), (repl - factor.reuse_transport_kgco2e_per_transfer) / repl))
        comps["E"] = round(float((Decimal(1) + carbon_fit) / 2), 3)
        reasons.append(f"diverts surplus units; carbon fit {carbon_fit:.2f} ({'synthetic' if factor.is_synthetic else 'sourced'} factor {factor.version})")
    observed = sum(WEIGHTS[k] for k in comps)
    score = Decimal(100) * sum(WEIGHTS[k] * Decimal(str(v)) for k, v in comps.items()) / observed
    return {
        "components": comps, "missing": missing, "coverage": float(round(observed, 2)), "score": float(round(score, 1)),
        "reasons": reasons, "limitations": limits, "reason_codes": codes,
        "transfer_cost": float(asset.estimated_transfer_cost_aed) if asset.estimated_transfer_cost_aed is not None else None,
    }


def one_line(item):
    """Concise explanation built only from the actual strongest components (FR-30)."""
    comps = item["components"]
    best = sorted(((k, v) for k, v in comps.items()), key=lambda kv: -float(WEIGHTS[kv[0]]) * kv[1])[:2]
    parts = [f"{LABELS[k].split(' ')[0].lower()} {v:.2f}" for k, v in best]
    gap = f"; {len(item['missing'])} factor(s) unavailable" if item["missing"] else ""
    return f"Strongest: {', '.join(parts)}{gap}."


def _semantic_gemini(client, req, assets):
    """Embeddings with an input-hash cache (DR-04)."""
    mv = model_version(ModelVersion.GEMINI, client.embedding_model, "embed")
    texts, todo = {}, []
    for a in assets:
        h = input_hash(asset_text(a))
        rep = AssetRepresentation.objects.filter(asset=a, model_version=mv).first()
        if rep and rep.input_hash == h and rep.refresh_state == "CURRENT":
            texts[a.pk] = rep.vector
        else:
            todo.append((a, h))
    vectors = client.embed([request_text(req)] + [asset_text(a) for a, _ in todo])
    qv = vectors[0]
    for (a, h), v in zip(todo, vectors[1:]):
        AssetRepresentation.objects.update_or_create(asset=a, model_version=mv, defaults={"input_hash": h, "vector": v, "dimension": len(v), "refresh_state": "CURRENT", "updated_at": timezone.now()})
        texts[a.pk] = v
    for a in assets:
        if len(texts[a.pk]) != len(qv):
            raise ValueError("dimension mismatch")
    return {a.pk: cosine(qv, texts[a.pk]) for a in assets}


def _semantic_local(req, assets):
    sims = similarity_matrix(request_text(req), [asset_text(a) for a in assets])
    return {a.pk: s for a, s in zip(assets, sims)}


def rank(user, req, gateway=None):
    """Return (AIOutcome, [MatchRecommendation]). Viewing a match never reserves stock."""
    from apps.exchange.services import expire_due_holds, remaining_need

    policy.require(policy.can_edit_request(user, req) or policy.can_view_request(user, req), "You cannot view matches for this request.", "rank", req)
    expire_due_holds()
    gateway = gateway or AIGateway()
    need = max(1, remaining_need(req))
    pool = Asset.objects.filter(is_published=True, status__in=AssetStatus.LISTED).exclude(owner_department=req.department).select_related("category__parent", "owner_department")
    eligible, excluded = [], []
    for a in pool:
        if not policy.can_view_asset(user, a):
            continue
        ok, why = hard_filter(a, req)
        (eligible if ok else excluded).append((a, why))
    assets = [a for a, _ in eligible]
    free = {a.pk: catalog.free_listed_quantity(a) for a in assets}

    def build(sims, method):
        items = []
        for a in assets:
            f = features(a, req, sims[a.pk], need, free[a.pk])
            f.update({"asset_id": str(a.pk), "asset_tag": a.asset_tag, "name": a.name})
            f["explanation"] = one_line(f)
            items.append(f)
        items.sort(key=lambda i: (-i["score"], -i["coverage"], i["transfer_cost"] if i["transfer_cost"] is not None else 1e12, i["asset_tag"]))
        top = items[:TOP_N]
        supported = [i for i in top if i["coverage"] >= float(COVERAGE_MIN)]
        return {
            "method": method,
            "need": need,
            "weights": {k: float(v) for k, v in WEIGHTS.items()},
            "ranked": top,
            "eligible_count": len(items),
            "excluded": [{"asset_tag": a.asset_tag, "name": a.name, "reasons": why} for a, why in excluded][:25],
            "weak_matches": bool(supported) and all(i["score"] < float(WEAK_BELOW) for i in supported),
            "top_pick": top[0]["asset_tag"] if top and top[0]["coverage"] >= float(COVERAGE_MIN) and top[0]["score"] >= float(WEAK_BELOW) else None,
        }

    def ai_call(client):
        sims = _semantic_gemini(client, req, assets) if assets else {}
        return build(sims, f"Gemini embeddings ({client.embedding_model}) + deterministic weighted score"), "not disclosed"

    def fallback(reason):
        return build(_semantic_local(req, assets) if assets else {}, "Local concept-similarity fallback (not AI) + deterministic weighted score")

    outcome = gateway.run(function="rank", user=user, input_refs=[str(req.pk)] + [str(a.pk) for a in assets], payload={"request": request_text(req), "v": req.version, "assets": sorted(str(a.pk) for a in assets)},
                          ai_call=ai_call, fallback=fallback, deadline_ms=15000, input_version=f"{req.reference} v{req.version}",
                          limitations=["Suitability is a 0–100 rating of fit, not a probability that the AI is correct.", "Model confidence unavailable."])
    recs = []
    with transaction.atomic():
        for i, item in enumerate(outcome.output["ranked"], start=1):
            recs.append(MatchRecommendation.objects.create(
                ai_result=outcome.result, request=req, asset_id=item["asset_id"], rank=i, score=Decimal(str(item["score"])), coverage=Decimal(str(item["coverage"])),
                components={"observed": item["components"], "missing": item["missing"], "weights": outcome.output["weights"]},
                reason_codes=item["reason_codes"], reasons=item["reasons"], limitations=item["limitations"], input_version=req.version,
            ))
    return outcome, recs
