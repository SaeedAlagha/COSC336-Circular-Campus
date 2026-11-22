"""AI feature 3 — Sustainable-action advice (UC-11, FR-32..35).

All numbers shown come from application records computed here; Gemini may only
rank the allowed actions and explain them from that evidence. Disposal is always
presented last. Accepting advice opens the normal proposal forms — it never
executes or authorises anything (FR-35).
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.db.models import Sum

from apps.catalog.models import AssetStatus, Condition
from apps.exchange.models import RequestStatus, ResourceRequest
from apps.maintenance.models import MaintenanceStatus

from .gateway import AIGateway

ACTIONS = ["RETAIN", "TRANSFER", "REPAIR", "REFURBISH", "DONATE", "COMPONENT_RECOVERY", "RECYCLE", "RETIRE", "DISPOSE"]
ACTION_LABELS = {"RETAIN": "Retain in current use", "TRANSFER": "Transfer / reuse internally", "REPAIR": "Repair", "REFURBISH": "Refurbish",
                 "DONATE": "Donate", "COMPONENT_RECOVERY": "Disassemble for usable components", "RECYCLE": "Recycle", "RETIRE": "Retire", "DISPOSE": "Dispose (final option)"}


def evidence(asset):
    """Deterministic evidence pack (the only facts the advice may use)."""
    records = asset.maintenance_records.all()
    open_records = records.filter(status__in=MaintenanceStatus.OPEN)
    last = records.order_by("-reported_at").first()
    root = asset.category.parent or asset.category
    demand = ResourceRequest.objects.filter(status__in=[RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW, RequestStatus.APPROVED, RequestStatus.PARTIALLY_FULFILLED],
                                            category__in=[root] + list(root.children.all()))
    spent = records.aggregate(s=Sum("actual_cost_aed"))["s"]
    estimate = None
    if last and last.status in (MaintenanceStatus.AWAITING_APPROVAL, MaintenanceStatus.UNDER_INSPECTION) and last.proposed_cost_aed is not None:
        estimate = last.proposed_cost_aed
    repl = asset.replacement_value_aed
    return {
        "asset": asset.asset_tag, "name": asset.name, "category": str(asset.category), "quantity": asset.quantity,
        "condition": asset.condition_label, "status": asset.status_label, "age_years": asset.age_years(),
        "unsafe_or_restricted": asset.is_restricted, "unrepairable_finding": records.filter(status=MaintenanceStatus.UNREPAIRABLE).exists(),
        "open_service_records": open_records.count(), "service_records_total": records.count(),
        "repair_estimate_aed": float(estimate) if estimate is not None else None,
        "maintenance_spent_aed": float(spent) if spent is not None else None,
        "replacement_value_per_unit_aed": float(repl) if repl is not None else None,
        "repair_to_replacement_ratio": round(float(estimate / repl), 2) if estimate is not None and repl else None,
        "open_requests_same_category": demand.count(),
        "units_requested_same_category": demand.aggregate(q=Sum("quantity"))["q"] or 0,
        "expected_retirement_date": asset.expected_retirement_date.isoformat() if asset.expected_retirement_date else None,
    }


def _rules(ev):
    """Deterministic action hierarchy (fallback). Disposal is last and only after alternatives."""
    opts = []

    def add(action, why):
        opts.append({"action": action, "label": ACTION_LABELS[action], "reasons": why})

    poor = ev["condition"] in ("Poor",)
    if ev["unrepairable_finding"]:
        add("COMPONENT_RECOVERY", ["inspection recorded the asset as unrepairable; usable parts may still be recovered"])
        add("RECYCLE", ["unrepairable finding — recycling diverts material from landfill"])
    else:
        if ev["open_requests_same_category"] and not ev["unsafe_or_restricted"] and not poor:
            add("TRANSFER", [f"{ev['open_requests_same_category']} open request(s) in this category ({ev['units_requested_same_category']} units)"])
        ratio = ev["repair_to_replacement_ratio"]
        if ev["repair_estimate_aed"] is not None and ratio is not None and ratio < 0.5:
            add("REPAIR", [f"repair estimate AED {ev['repair_estimate_aed']:,.0f} is {ratio:.0%} of the AED {ev['replacement_value_per_unit_aed']:,.0f} replacement value"])
        elif poor or ev["unsafe_or_restricted"]:
            add("REPAIR", ["condition is Poor or the asset is restricted; an inspection and repair estimate is needed before reuse"])
        if not ev["open_requests_same_category"] and not poor and not ev["unsafe_or_restricted"]:
            add("RETAIN", ["no open internal demand in this category; keep in use while it is serviceable"])
            add("DONATE", ["functional stock without internal demand can be donated to an external partner"])
        add("RECYCLE", ["if reuse and repair are not feasible, recycling diverts material"])
    add("DISPOSE", ["final option only after the alternatives above have been considered"])
    seen, ordered = set(), []
    for o in opts:
        if o["action"] not in seen:
            seen.add(o["action"])
            ordered.append(o)
    return ordered


SCHEMA = {
    "type": "object",
    "properties": {
        "options": {"type": "array", "maxItems": 5, "items": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ACTIONS}, "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
            "required": ["action", "reasons"]}},
        "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    },
    "required": ["options", "limitations"],
}
SYSTEM = ("You advise a university custodian on the next sustainable action for an asset. Use ONLY the JSON evidence provided. "
          "Prefer retaining, internal transfer, repair or refurbishment; then donation, component recovery and recycling; disposal is "
          "the final option. Do not invent costs, carbon figures or dates; only repeat numbers present in the evidence. If information "
          "is missing say so in limitations. You cannot approve or execute anything. Do not output confidence scores.")


def _numbers(text):
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def recommend(user, asset, gateway=None):
    ev = evidence(asset)
    gateway = gateway or AIGateway()
    allowed_numbers = _numbers(str(ev))

    def ai_call(client):
        import json

        out, rev = client.generate_json(SYSTEM, "Evidence:\n" + json.dumps(ev, default=str), SCHEMA, timeout=client.timeout)
        options, notes = [], []
        for o in out.get("options", []):
            action = o.get("action")
            if action not in ACTIONS:
                raise ValueError("unsupported action")
            reasons = [str(r)[:200] for r in o.get("reasons", [])][:3]
            for r in reasons:
                extra = _numbers(r) - allowed_numbers
                if extra:
                    notes.append(f"An AI reason quoted a figure not in the evidence ({', '.join(sorted(extra))}); treat it as unsupported.")
            options.append({"action": action, "label": ACTION_LABELS[action], "reasons": reasons})
        if not options:
            raise ValueError("no options")
        # Disposal is always presented last (FR-32).
        options = [o for o in options if o["action"] != "DISPOSE"] + [o for o in options if o["action"] == "DISPOSE"]
        return {"options": options, "evidence": ev, "limitations": [str(x)[:200] for x in out.get("limitations", [])][:4] + notes}, rev

    def fallback(reason):
        return {"options": _rules(ev), "evidence": ev, "limitations": ["Rule-based hierarchy; costs and carbon impacts not estimated beyond recorded values."]}

    return gateway.run(function="action", user=user, input_refs=[str(asset.pk)], payload=ev, ai_call=ai_call, fallback=fallback,
                       deadline_ms=10000, input_version=f"{asset.asset_tag} v{asset.version}",
                       limitations=["Advice only — any action still needs the normal proposal and human approval.", "Confidence unavailable."])
