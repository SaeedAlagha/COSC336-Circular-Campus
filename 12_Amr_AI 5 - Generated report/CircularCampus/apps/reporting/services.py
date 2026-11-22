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
from apps.intelligence.gateway import AIGateway
from apps.workflow.models import TransferStatus

from . import audit, metrics
from .models import AvoidedPurchaseReview, EmissionFactor, Report, ReportMetric


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def create_report(actor, report_type, start, end, department=None):
    policy.require(policy.can_author_report(actor), "Only reporting officers (procurement, finance, sustainability) can create report drafts.", "create_report")
    if department is not None:
        policy.require(policy.has_role(actor, "PROCUREMENT", department.pk) or policy.has_role(actor, "FINANCE", department.pk) or policy.has_role(actor, "SUSTAINABILITY", department.pk),
                       "That department is outside your reporting scope.", "create_report")
    else:
        policy.require(any(policy.scoped_departments(actor, r) is None and policy.has_role(actor, r) for r in ("PROCUREMENT", "FINANCE", "SUSTAINABILITY")),
                       "Campus-wide reports need a campus-wide reporting grant; choose your department.", "create_report")
    if report_type not in dict(Report.TYPES):
        raise BusinessError(BusinessError.VALIDATION_ERROR, "Choose a report type.", {"report_type": "Choose a type."})
    start = V.a_date(start, "period_start", label="Period start", required=True, not_before=date(2000, 1, 1), not_after=timezone.localdate())
    end = V.a_date(end, "period_end", label="Period end", required=True, not_before=date(2000, 1, 1), not_after=timezone.localdate() + timedelta(days=366))
    if start > end:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The period start must be before its end.", {"period_start": "Start must not be after end."})
    if (end - start).days > 366 * 5:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "A report period can cover at most five years.", {"period_end": "Choose a shorter period."})
    snapshot = metrics.compute(start, end, department)
    with transaction.atomic():
        report = Report.objects.create(reference=next_reference("REP"), report_type=report_type, period_start=start, period_end=end, scope_department=department,
                                       snapshot=snapshot, snapshot_hash=_hash(snapshot), snapshot_version=metrics.VERSION, generated_by=actor)
        for impact in metrics.supporting_impacts(start, end, department):
            ReportMetric.objects.get_or_create(report=report, impact=impact, defaults={"display_value_snapshot": f"{impact.indicator}:{impact.quantity}{' ' + str(impact.value) if impact.value is not None else ''}"})
        audit.record(actor, "Report snapshot created", "report", report, after={"type": report_type, "period": f"{start}..{end}", "hash": report.snapshot_hash[:12]})
        return report


# ------------------------------------------------------------- validation ---
NUM = re.compile(r"(?<![A-Za-z\-])\d[\d,]*(?:\.\d+)?")


def _allowed_numbers(snapshot, report):
    allowed = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float, Decimal)):
            f = float(v)
            allowed.update({f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", str(int(f)) if f == int(f) else f"{f}"})
        elif isinstance(v, str):
            for m in NUM.findall(v):
                allowed.add(m.replace(",", ""))
        elif isinstance(v, dict):
            for x in v.values():
                add(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x)

    # The generation timestamp is metadata, not a reportable figure (its minutes must not "allow" numbers).
    add({k: v for k, v in snapshot.items() if k != "meta"})
    meta = snapshot.get("meta", {})
    add({k: v for k, v in meta.items() if k != "computed_at"})
    for d in (report.period_start, report.period_end):
        allowed.update({str(d.year), str(d.day), f"{d.day:02d}", str(d.month)})
    allowed.update({"1", "2", "3", "100"})  # ordinal phrasing ("top 3"), percent base
    return allowed


def validate_narrative(report, text):
    """Every figure in the narrative must appear in the immutable snapshot (AI-05, UC-06 step 4)."""
    def norm(n):
        n = n.replace(",", "")
        return n.rstrip("0").rstrip(".") if "." in n else n

    allowed = {norm(a) for a in _allowed_numbers(report.snapshot, report)}
    unsupported = sorted({n for n in NUM.findall(text) if norm(n) not in allowed})
    unavailable = [i["label"] for i in report.snapshot["indicators"].values() if i["status"] == "unavailable"]
    mentioned_unavailable = [u for u in unavailable if u.lower() in text.lower() and re.search(rf"{re.escape(u.lower())}[^.]*\d", text.lower())]
    return {"unsupported_numbers": unsupported, "unavailable_claimed": mentioned_unavailable, "ok": not unsupported and not mentioned_unavailable,
            "checked_at": timezone.now().isoformat()}


def template_narrative(report):
    """Deterministic draft used when Gemini is unavailable — labelled 'Template draft (not AI)'."""
    s, ind, op = report.snapshot, report.snapshot["indicators"], report.snapshot["operational"]
    parts = [f"{report.get_report_type_display()} for {s['meta']['scope']}, {report.period_start:%d %b %Y} to {report.period_end:%d %b %Y}."]
    parts.append(f"{op['completed_transfers']} transfers were completed ({op['units_received']} units received) and {op['open_requests']} requests remain open.")
    for key in ("items_reused", "waste_diversion", "avoided_value", "net_savings", "repairs"):
        i = ind[key]
        parts.append(f"{i['label']}: {i['display']}." if i["status"] != "unavailable" else f"{i['label']} is not available: {i['note']}")
    if ind["carbon"]["status"] == "unavailable":
        parts.append("No carbon reduction is claimed because no approved emission factor applies.")
    if op["high_demand_categories"]:
        parts.append("Highest demand: " + ", ".join(f"{c['category']} ({c['requests']})" for c in op["high_demand_categories"]) + ".")
    return " ".join(parts)


SCHEMA = {"type": "object", "properties": {"narrative": {"type": "string"}, "cited_metrics": {"type": "array", "items": {"type": "string"}}}, "required": ["narrative", "cited_metrics"]}
SYSTEM = ("You write a short, factual management summary (90-160 words) of a university asset-reuse report. Use ONLY the figures in the JSON "
          "snapshot; copy numbers exactly as given in 'display' fields; do not compute new totals, percentages or estimates. For any indicator with "
          "status 'unavailable', say it is unavailable and why — never give it a number. Mention provisional figures as provisional. "
          "List cited indicator keys in cited_metrics. Plain prose, no headings, no markdown.")


def _lock_report(report_id):
    report_id = V.parse_uuid(report_id, "report")
    try:
        return Report.objects.select_for_update().get(pk=report_id)
    except Report.DoesNotExist:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "That report does not exist.")


def generate_narrative(actor, report_id, gateway=None):
    with transaction.atomic():
        report = _lock_report(report_id)
        policy.require(policy.can_author_report(actor), "Only reporting officers can generate narratives.", "generate_narrative", report)
        if report.status == Report.PUBLISHED:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Published reports are immutable; create a new report.")
    gateway = gateway or AIGateway()
    minimal = {"type": report.get_report_type_display(), "meta": report.snapshot["meta"],
               "indicators": {k: {f: v[f] for f in ("label", "display", "unit", "status", "note")} for k, v in report.snapshot["indicators"].items()},
               "operational": report.snapshot["operational"]}

    def ai_call(client):
        out, rev = client.generate_json(SYSTEM, "Snapshot:\n" + json.dumps(minimal, default=str), SCHEMA, timeout=client.timeout)
        text = str(out.get("narrative", "")).strip()
        if len(text) < 40:
            raise ValueError("narrative too short")
        bad = [m for m in out.get("cited_metrics", []) if m not in report.snapshot["indicators"] and m not in report.snapshot["operational"]]
        return {"narrative": text[:3000], "cited_metrics": [m for m in out.get("cited_metrics", []) if m not in bad], "dropped_citations": bad}, rev

    outcome = gateway.run(function="report", user=actor, input_refs=[str(report.pk)], payload={"snapshot_hash": report.snapshot_hash, "type": report.report_type},
                          ai_call=ai_call, fallback=lambda reason: {"narrative": template_narrative(report), "cited_metrics": []}, deadline_ms=15000,
                          input_version=report.snapshot_hash[:16], limitations=["Narrative confidence unavailable; human review is mandatory before publication."])
    with transaction.atomic():
        report = _lock_report(report_id)
        text = outcome.output["narrative"]
        report.ai_result = outcome.result
        report.narrative_source = Report.TEMPLATE if outcome.fallback else Report.AI
        report.narrative_original = text
        report.narrative_text = text
        report.validation = validate_narrative(report, text)
        report.status = Report.DRAFT
        report.reviewed_by = report.reviewed_at = None
        report.version += 1
        report.save()
        audit.record(actor, "Narrative drafted", "report", report, after={"source": report.narrative_source, "valid": report.validation["ok"], "ai_result": str(outcome.result.pk)})
    return report, outcome


def edit_narrative(actor, report_id, expected_version, text):
    with transaction.atomic():
        report = _lock_report(report_id)
        policy.require(policy.can_author_report(actor), "Only reporting officers can edit narratives.", "edit_narrative", report)
        if report.status == Report.PUBLISHED:
            raise BusinessError(BusinessError.INVALID_TRANSITION, "Published reports are immutable; create a new report.")
        if report.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "The narrative changed after you opened it. Reload to see the latest text.")
        if len((text or "").strip()) < 20:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Write a narrative of at least 20 characters.", {"narrative_text": "Too short."})
        text = V.text(text, "narrative_text", label="Narrative", required=True, min_len=20, max_len=5000, multiline=True)
        before = report.narrative_text
        report.narrative_text = text[:5000]
        if not report.narrative_source:
            report.narrative_source = Report.MANUAL
        report.validation = validate_narrative(report, report.narrative_text)
        report.status = Report.DRAFT  # any edit requires a fresh review
        report.reviewed_by = report.reviewed_at = None
        report.version += 1
        report.save()
        audit.record(actor, "Narrative edited", "report", report, before={"text": before[:500]}, after={"text": report.narrative_text[:500], "valid": report.validation["ok"]})
        return report


def mark_reviewed(actor, report_id, expected_version, confirmed):
    with transaction.atomic():
        report = _lock_report(report_id)
        policy.require(policy.can_author_report(actor), "Only reporting officers can review narratives.", "review_report", report)
        if report.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "The narrative changed after you opened it. Review the latest text.")
        if not report.narrative_text:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "There is no narrative to review.")
        if not confirmed:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "Tick the confirmation that you checked every statement against the snapshot.", {"confirmed": "Required."})
        report.validation = validate_narrative(report, report.narrative_text)
        if not report.validation["ok"]:
            raise BusinessError(BusinessError.VALIDATION_ERROR, "The narrative contains figures not found in the snapshot (" + ", ".join(report.validation["unsupported_numbers"] + report.validation["unavailable_claimed"]) + "). Edit them before review.")
        report.status, report.reviewed_by, report.reviewed_at = Report.REVIEWED, actor, timezone.now()
        report.version += 1
        report.save()
        audit.record(actor, "Narrative reviewed", "report", report)
        return report


def publish(actor, report_id, expected_version):
    """Separate, explicit publication. Drafting never publishes (AI feature 5)."""
    with transaction.atomic():
        report = _lock_report(report_id)
        policy.require(policy.can_author_report(actor), "Only reporting officers can publish reports.", "publish_report", report)
        if report.version != V.version(expected_version):
            raise BusinessError(BusinessError.STALE_VERSION, "The report changed after you opened it. Reload and review it again.")
        if report.status != Report.REVIEWED:
            raise BusinessError(BusinessError.MISSING_REVIEW, "Review the current narrative before publishing.")
        report.status, report.published_by, report.published_at = Report.PUBLISHED, actor, timezone.now()
        report.version += 1
        report.save()
        audit.record(actor, "Report published", "report", report, after={"snapshot_hash": report.snapshot_hash[:12], "narrative_source": report.narrative_source})
        return report


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
