"""Evidence-linked sustainability impacts, written inside the completing transaction.

Each impact names its asset batch and life-cycle event (SUS-08/SUS-09). Repeat
transfers of units that were already counted are stored with ``counted=False``
so they stay traceable but never inflate a numerator.
"""
from decimal import Decimal

from .models import SustainabilityImpact


def months_between(start, end):
    """Whole months from start to end (positive difference only)."""
    import calendar

    months = (end.year - start.year) * 12 + (end.month - start.month)
    end_is_month_end = end.day == calendar.monthrange(end.year, end.month)[1]
    if end.day < start.day and not end_is_month_end:
        months -= 1
    return months


def record_reuse(received_asset, event, quantity, department, *, source_counted, cohort_entered_at, source_department=None):
    counted = not source_counted
    for indicator in (SustainabilityImpact.REUSE, SustainabilityImpact.DIVERSION):
        SustainabilityImpact.objects.create(
            asset=received_asset, event=event, indicator=indicator, quantity=quantity, unit="units", department=department, source_department=source_department,
            cohort_entered_at=cohort_entered_at, counted=counted, occurred_at=event.occurred_at,
        )


def record_diversion(asset, event, quantity):
    """Donation, recycling or component recovery of cohort units (SUS-05)."""
    counted = not asset.reuse_counted
    SustainabilityImpact.objects.create(
        asset=asset, event=event, indicator=SustainabilityImpact.DIVERSION, quantity=quantity, unit="units",
        department=asset.owner_department, source_department=asset.owner_department, cohort_entered_at=asset.cohort_entered_at, counted=counted, occurred_at=event.occurred_at,
    )
    if not asset.reuse_counted:
        asset.reuse_counted = True
        asset.save(update_fields=["reuse_counted"])


def record_repair(asset, event, record):
    """SUS-02; a repaired unit from the unwanted cohort also counts once toward diversion (SUS-05)."""
    if asset.cohort_entered_at is not None:
        record_diversion(asset, event, asset.quantity)
    SustainabilityImpact.objects.create(
        asset=asset, event=event, indicator=SustainabilityImpact.REPAIR, quantity=asset.quantity,
        value=record.actual_cost_aed, unit="units", department=asset.owner_department, occurred_at=event.occurred_at,
        status="VERIFIED" if record.actual_cost_aed is not None else "PROVISIONAL",
    )


def record_life_extension(asset, event):
    """SUS-06: positive months between supported revised date and the baseline; no overlap per asset."""
    if not asset.expected_retirement_date or not asset.revised_usable_until:
        return None
    months = months_between(asset.expected_retirement_date, asset.revised_usable_until)
    if months <= 0:
        return None
    SustainabilityImpact.objects.filter(asset=asset, indicator=SustainabilityImpact.LIFE_EXTENSION, counted=True).update(counted=False)
    return SustainabilityImpact.objects.create(
        asset=asset, event=event, indicator=SustainabilityImpact.LIFE_EXTENSION, quantity=asset.quantity, value=Decimal(months),
        unit="months", department=asset.owner_department, occurred_at=event.occurred_at, status="PROVISIONAL",
    )
