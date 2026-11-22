"""Deterministic sustainability and operational calculations (UC-06.1; design 5.11).

Nothing here calls an AI service. Every indicator returns its formula, units,
period, scope, inputs, evidence source and status. Missing evidence or a zero
denominator produces ``status="unavailable"`` with a reason — never a zero or
an invented estimate (SUS-08, BR-06). Counted units are de-duplicated through
``SustainabilityImpact.counted`` (SUS-09).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from apps.catalog.models import Asset, AssetStatus
from apps.exchange.models import RequestStatus, ResourceRequest
from apps.maintenance.models import Disposition, LifeCycleEvent, MaintenanceRecord, MaintenanceStatus
from apps.workflow.models import Approval, Transfer, TransferStatus

from .models import AvoidedPurchaseReview, EmissionFactor, SustainabilityImpact

VERSION = "metrics-v1"


def _bounds(start, end):
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(start, time.min), tz), timezone.make_aware(datetime.combine(end, time.max), tz)


def _fmt_int(n):
    return f"{int(n):,}"


def _fmt_aed(v):
    return f"AED {Decimal(v):,.0f}"


def _scope_q(dept, field="department", alt="source_department"):
    if dept is None:
        return Q()
    return Q(**{field: dept}) | Q(**{alt: dept})


def indicator(key, label, *, value=None, display=None, unit="", formula="", inputs=None, source="", status="available", note="", ref=""):
    return {"key": key, "label": label, "value": value, "display": display if display is not None else ("Unavailable" if value is None else str(value)),
            "unit": unit, "formula": formula, "inputs": inputs or {}, "source": source, "status": status, "note": note, "ref": ref}


def cohort_units(dept, start_dt, end_dt):
    events = LifeCycleEvent.objects.filter(event_type="PUBLISHED", occurred_at__range=(start_dt, end_dt), after__cohort_entry=True)
    total, assets = 0, []
    for e in events:
        if dept is None or e.after.get("department") == str(dept.pk):
            total += e.quantity or 0
            assets.append(e.asset_id)
    return total, assets


def compute(start, end, department=None):
    start_dt, end_dt = _bounds(start, end)
    scope = department.name if department else "Campus-wide"
    meta = {"period_start": start.isoformat(), "period_end": end.isoformat(), "cutoff": end.isoformat(), "scope": scope, "version": VERSION, "computed_at": timezone.now().isoformat()}
    impacts = SustainabilityImpact.objects.filter(_scope_q(department))
    out = {}

    # SUS-01 reuse count
    reuse = impacts.filter(indicator=SustainabilityImpact.REUSE, occurred_at__range=(start_dt, end_dt))
    counted_units = reuse.filter(counted=True).aggregate(q=Sum("quantity"))["q"] or 0
    repeat_units = reuse.filter(counted=False).aggregate(q=Sum("quantity"))["q"] or 0
    out["items_reused"] = indicator(
        "items_reused", "Items reused", value=counted_units, display=_fmt_int(counted_units), unit="units",
        formula="Σ distinct units received through completed reuse transfers in the period (each unit counted once)",
        inputs={"completed reuse transfers": reuse.values("event__transfer").distinct().count(), "repeat-transfer units excluded": repeat_units},
        source="Completed transfers with matching receipt (LifeCycleEvent RECEIVED → SustainabilityImpact REUSE)", ref="SUS-01, SUS-09")

    # SUS-01 reuse rate and SUS-05 waste diversion share the same cohort denominator
    den, cohort_assets = cohort_units(department, start_dt, end_dt)
    cohort_q = impacts.filter(cohort_entered_at__range=(start_dt, end_dt), occurred_at__lte=end_dt, counted=True)
    if department is not None:
        cohort_q = cohort_q.filter(source_department=department)
    reuse_num = cohort_q.filter(indicator=SustainabilityImpact.REUSE).aggregate(q=Sum("quantity"))["q"] or 0
    div_num = cohort_q.filter(indicator=SustainabilityImpact.DIVERSION).aggregate(q=Sum("quantity"))["q"] or 0
    if den == 0:
        out["reuse_rate"] = indicator("reuse_rate", "Reuse rate", unit="%", status="unavailable", note="No units entered the surplus cohort in this period (zero denominator).",
                                      formula="units from the period's surplus cohort reused by the cutoff ÷ units entering that cohort × 100", ref="SUS-01, SUS-08")
        out["waste_diversion"] = indicator("waste_diversion", "Waste diversion rate", unit="%", status="unavailable", note="No units entered the unwanted-asset cohort in this period (zero denominator).",
                                           formula="cohort units reused, repaired, donated, recycled or component-recovered by the cutoff ÷ all units entering the cohort × 100", ref="SUS-05, SUS-08")
    else:
        disposed = Disposition.objects.filter(status=Disposition.COMPLETED, action="DISPOSE", asset__cohort_entered_at__range=(start_dt, end_dt)).aggregate(q=Sum("quantity"))["q"] or 0
        out["reuse_rate"] = indicator("reuse_rate", "Reuse rate", value=round(reuse_num * 100 / den, 1), display=f"{reuse_num * 100 / den:.1f}%", unit="%",
                                      formula="units from the period's surplus cohort reused by the cutoff ÷ units entering that cohort × 100",
                                      inputs={"reused cohort units": reuse_num, "cohort units": den}, source="PUBLISHED cohort-entry events and REUSE impacts", ref="SUS-01")
        out["waste_diversion"] = indicator("waste_diversion", "Waste diversion rate", value=round(div_num * 100 / den, 1), display=f"{div_num * 100 / den:.1f}%", unit="%",
                                           formula="cohort units reused, repaired, donated, recycled or component-recovered by the cutoff ÷ all units entering the cohort × 100",
                                           inputs={"diverted": div_num, "discarded (disposed)": disposed, "pending": max(0, den - div_num - disposed), "cohort units": den},
                                           source="Cohort-entry events; DIVERSION impacts from receipts, verified repairs and completed dispositions", ref="SUS-05, SUS-09")
    # kg diverted (weights where recorded)
    div_rows = impacts.filter(indicator=SustainabilityImpact.DIVERSION, counted=True, occurred_at__range=(start_dt, end_dt)).select_related("asset")
    total_units = sum(r.quantity for r in div_rows)
    known = [(r.quantity, r.asset.unit_weight_kg) for r in div_rows if r.asset.unit_weight_kg is not None]
    kg = sum(Decimal(q) * w for q, w in known)
    known_units = sum(q for q, _ in known)
    if not total_units or not known_units:
        out["kg_diverted"] = indicator("kg_diverted", "Waste diverted", unit="kg", status="unavailable",
                                       note="No diverted units with a recorded unit weight in this period." if total_units else "No diverted units in this period.",
                                       formula="Σ diverted units × recorded unit weight (kg)", ref="SUS-05, SUS-08")
    else:
        out["kg_diverted"] = indicator("kg_diverted", "Waste diverted", value=float(kg), display=f"{kg:,.0f} kg", unit="kg", formula="Σ diverted units × recorded unit weight (kg)",
                                       inputs={"units with weight": known_units, "diverted units": total_units}, source="Asset unit_weight_kg × DIVERSION impacts",
                                       status="available" if known_units == total_units else "provisional",
                                       note="" if known_units == total_units else f"Weight known for {known_units} of {total_units} diverted units; the rest are excluded, not estimated.", ref="SUS-05")

    # SUS-03 / SUS-04 avoided purchases and net savings
    reviews = AvoidedPurchaseReview.objects.filter(confirmed=True, transfer__status=TransferStatus.COMPLETED, transfer__received_at__range=(start_dt, end_dt)).select_related("transfer")
    if department is not None:
        reviews = reviews.filter(Q(transfer__reservation__request__department=department) | Q(transfer__source_asset__owner_department=department))
    avoided = sum((r.avoided_value or Decimal(0)) for r in reviews)
    units_avoided = sum(r.quantity or 0 for r in reviews)
    if not reviews:
        msg = "No Procurement/Finance-confirmed avoided purchases in this period — a transfer alone does not prove savings."
        out["avoided_value"] = indicator("avoided_value", "Purchase cost avoided", unit="AED", status="unavailable", note=msg,
                                         formula="Σ confirmed substituted quantity × reference unit price", ref="SUS-03")
        out["net_savings"] = indicator("net_savings", "Net financial savings", unit="AED", status="unavailable", note=msg,
                                       formula="avoided purchase value − transfer − repair − refurbishment − other related reuse costs", ref="SUS-04")
    else:
        transfer_costs = [r.transfer.transfer_cost_aed for r in reviews]
        unknown = sum(1 for c in transfer_costs if c is None)
        related = sum((c or Decimal(0)) for c in transfer_costs) + sum((r.related_repair_cost_aed or 0) + (r.related_other_cost_aed or 0) for r in reviews)
        net = avoided - related
        out["avoided_value"] = indicator("avoided_value", "Purchase cost avoided", value=float(avoided), display=_fmt_aed(avoided), unit="AED",
                                         formula="Σ confirmed substituted quantity × reference unit price", inputs={"confirmed reviews": len(reviews), "units": units_avoided},
                                         source="AvoidedPurchaseReview (Procurement/Finance confirmation with price reference)", ref="SUS-03")
        out["net_savings"] = indicator("net_savings", "Net financial savings", value=float(net), display=_fmt_aed(net), unit="AED",
                                       formula="avoided purchase value − transfer − repair − refurbishment − other related reuse costs",
                                       inputs={"avoided": float(avoided), "related costs": float(related), "transfers with unknown cost": unknown},
                                       source="Confirmed reviews and recorded transfer costs", status="provisional" if unknown else "available",
                                       note=(f"{unknown} transfer cost(s) unknown — savings are provisional, not reduced by an invented cost." if unknown else "") + (" Negative savings are shown as negative." if net < 0 else ""), ref="SUS-04, BR-05")

    # SUS-02 repair and refurbishment
    svc = MaintenanceRecord.objects.filter(asset__owner_department=department) if department else MaintenanceRecord.objects.all()
    done = svc.filter(status=MaintenanceStatus.COMPLETED, verified_at__range=(start_dt, end_dt))
    repaired = done.exclude(work_type="")
    cost_known = repaired.exclude(actual_cost_aed__isnull=True)
    out["repairs"] = indicator("repairs", "Repairs returned to service", value=repaired.count(), display=_fmt_int(repaired.count()), unit="jobs",
                               formula="count of verified repair/refurbishment jobs in the period (open and unrepairable jobs reported separately)",
                               inputs={"units returned to service": sum(r.asset.quantity for r in repaired.select_related("asset")), "inspections closed without repair": done.filter(work_type="").count(),
                                       "open jobs": svc.filter(status__in=MaintenanceStatus.OPEN).count(), "unrepairable": svc.filter(status=MaintenanceStatus.UNREPAIRABLE, reported_at__range=(start_dt, end_dt)).count(),
                                       "recorded repair cost (AED)": float(cost_known.aggregate(s=Sum("actual_cost_aed"))["s"] or 0), "jobs with unknown cost": repaired.count() - cost_known.count()},
                               source="MaintenanceRecord verified by a custodian", ref="SUS-02")

    # SUS-06 life extension
    ext = impacts.filter(indicator=SustainabilityImpact.LIFE_EXTENSION, counted=True, occurred_at__range=(start_dt, end_dt))
    if not ext:
        out["life_extension"] = indicator("life_extension", "Asset-life extension", unit="months", status="unavailable",
                                          note="No asset in this period has both a baseline retirement date and a supported revised usable-until date.",
                                          formula="Σ positive months between revised usable-until date and baseline expected retirement (no overlap per asset)", ref="SUS-06")
    else:
        months = sum(int(e.value) for e in ext)
        out["life_extension"] = indicator("life_extension", "Asset-life extension", value=months, display=f"{months} months", unit="months",
                                          formula="Σ positive months between revised usable-until date and baseline expected retirement (no overlap per asset)",
                                          inputs={"assets": ext.count()}, source="Verified service closures with revised usable-until dates", status="provisional",
                                          note="Revised dates are custodian estimates recorded at verification.", ref="SUS-06")

    # SUS-07 carbon
    if not reviews:
        out["carbon"] = indicator("carbon", "Estimated carbon avoided", unit="kg CO2e", status="unavailable", note="No confirmed avoided purchases to apply a factor to.",
                                  formula="Σ (substituted units × replacement factor) − Σ transport factor per transfer", ref="SUS-07")
    else:
        total, covered, synthetic, versions = Decimal(0), 0, False, set()
        for r in reviews:
            cat = r.transfer.source_asset.category
            f = EmissionFactor.objects.filter(active=True, category__in=[c for c in (cat, cat.parent) if c]).order_by("-created_at").first()
            if f is None:
                continue
            covered += 1
            synthetic |= f.is_synthetic
            versions.add(f"{f.version} ({f.source[:60]})")
            total += Decimal(r.quantity or 0) * f.replacement_kgco2e_per_unit - f.reuse_transport_kgco2e_per_transfer
        if covered == 0:
            out["carbon"] = indicator("carbon", "Estimated carbon avoided", unit="kg CO2e", status="unavailable",
                                      note="No approved emission factor is recorded for these categories, so no carbon figure is claimed.",
                                      formula="Σ (substituted units × replacement factor) − Σ transport factor per transfer", ref="SUS-07, BR-06")
        else:
            out["carbon"] = indicator("carbon", "Estimated carbon avoided", value=float(total), display=f"{total:,.0f} kg CO2e", unit="kg CO2e",
                                      formula="Σ (substituted units × replacement factor) − Σ transport factor per transfer",
                                      inputs={"transfers with factor": covered, "transfers without factor": len(reviews) - covered}, source="; ".join(sorted(versions)),
                                      status="provisional", note=("Uses an illustrative (unverified) factor. " if synthetic else "") + "Estimate, not a measured outcome." + (" Partial coverage." if covered < len(reviews) else ""), ref="SUS-07")

    # Operational totals (FR-41)
    assets = Asset.objects.all() if department is None else Asset.objects.filter(owner_department=department)
    listed = assets.filter(is_published=True, status__in=AssetStatus.LISTED)
    from apps.catalog.services import free_listed_quantity

    reqs = ResourceRequest.objects.all() if department is None else ResourceRequest.objects.filter(department=department)
    transfers = Transfer.objects.filter(status=TransferStatus.COMPLETED, received_at__range=(start_dt, end_dt))
    if department is not None:
        transfers = transfers.filter(Q(reservation__request__department=department) | Q(source_asset__owner_department=department))
    demand = Counter(str(r.category.parent or r.category) for r in reqs.filter(created_at__range=(start_dt, end_dt)).select_related("category__parent"))
    operational = {
        "listed_assets": listed.count(),
        "free_units": sum(free_listed_quantity(a) for a in listed),
        "open_requests": reqs.filter(status__in=RequestStatus.OPEN - {RequestStatus.DRAFT}).count(),
        "requests_created": reqs.filter(created_at__range=(start_dt, end_dt)).count(),
        "completed_transfers": transfers.count(),
        "units_received": transfers.aggregate(q=Sum("received_quantity"))["q"] or 0,
        "transfer_exceptions": Transfer.objects.filter(status=TransferStatus.EXCEPTION).count(),
        "approval_decisions": Approval.objects.filter(decided_at__range=(start_dt, end_dt)).exclude(decision=Approval.NOT_APPLICABLE).count(),
        "maintenance_opened": svc.filter(reported_at__range=(start_dt, end_dt)).count(),
        "maintenance_closed": done.count(),
        "custody_changes": LifeCycleEvent.objects.filter(event_type="RECEIVED", occurred_at__range=(start_dt, end_dt)).count(),
        "high_demand_categories": [{"category": c, "requests": n} for c, n in demand.most_common(3)],
    }
    return {"meta": meta, "indicators": out, "operational": operational}


def supporting_impacts(start, end, department=None):
    start_dt, end_dt = _bounds(start, end)
    return SustainabilityImpact.objects.filter(_scope_q(department), occurred_at__range=(start_dt, end_dt), counted=True)
