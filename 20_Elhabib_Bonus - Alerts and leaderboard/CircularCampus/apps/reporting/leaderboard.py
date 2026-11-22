"""Bonus feature — department sustainability leaderboard and badges.

Every number comes from the same counted, evidence-linked SustainabilityImpact
records as the dashboard (never from AI), so a unit counted once there is
counted once here. Points use a published formula so anyone can recompute them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from apps.accounts.models import Department

from .models import AvoidedPurchaseReview, SustainabilityImpact

# Points per unit of evidence — shown on the page next to the table.
POINTS = {"given": 10, "received": 5, "diverted": 8, "repairs": 15}
POINTS_PER_AED = Decimal("0.01")  # 1 point per AED 100 of confirmed avoided purchase

FORMULA = ("Points = 10 × units given to other departments + 5 × units received and reused + "
           "8 × units diverted from landfill (donated, recycled, components) + 15 × repairs returned to service + "
           "1 per AED 100 of confirmed avoided purchases.")

PERIODS = {"90d": ("Last 90 days", 90), "year": ("Last 12 months", 365), "all": ("All time", None)}


@dataclass
class Badge:
    code: str
    name: str
    icon: str
    rule: str


BADGES = [
    Badge("FIRST_SHARE", "First share", "package", "Gave at least 1 unit to another department"),
    Badge("GENEROUS", "Generous giver", "award", "Gave 10 or more units to other departments"),
    Badge("CIRCULAR", "Circular champion", "recycle", "Received and reused 10 or more units"),
    Badge("DIVERTER", "Waste diverter", "leaf", "Diverted 5 or more units from landfill"),
    Badge("FIXER", "Fix-it team", "wrench", "Returned 2 or more repaired items to service"),
    Badge("SAVER", "Budget saver", "coins", "Confirmed AED 5,000 or more in avoided purchases"),
]


@dataclass
class Row:
    department: Department
    given: int = 0
    received: int = 0
    diverted: int = 0
    repairs: int = 0
    avoided_aed: Decimal = Decimal("0")
    points: int = 0
    rank: int = 0
    badges: list = field(default_factory=list)

    def compute(self):
        self.points = int(self.given * POINTS["given"] + self.received * POINTS["received"] + self.diverted * POINTS["diverted"]
                          + self.repairs * POINTS["repairs"] + int(self.avoided_aed * POINTS_PER_AED))
        earned = {
            "FIRST_SHARE": self.given >= 1, "GENEROUS": self.given >= 10, "CIRCULAR": self.received >= 10,
            "DIVERTER": self.diverted >= 5, "FIXER": self.repairs >= 2, "SAVER": self.avoided_aed >= 5000,
        }
        self.badges = [b for b in BADGES if earned[b.code]]
        return self


def build(period="90d", now=None):
    """Return (rows ranked by points, start datetime or None)."""
    now = now or timezone.now()
    days = PERIODS.get(period, PERIODS["90d"])[1]
    start = None
    if days:
        start = timezone.make_aware(datetime.combine(timezone.localdate(now) - timedelta(days=days - 1), time.min))
    impacts = SustainabilityImpact.objects.filter(counted=True)
    reviews = AvoidedPurchaseReview.objects.filter(confirmed=True)
    if start:
        impacts = impacts.filter(occurred_at__gte=start)
        reviews = reviews.filter(reviewed_at__gte=start)
    rows = {d.pk: Row(d) for d in Department.objects.filter(active=True)}

    def add(qs, key, attr, value="quantity"):
        for r in qs.values(key).annotate(n=Sum(value) if value else Count("id")):
            if r[key] in rows:
                setattr(rows[r[key]], attr, getattr(rows[r[key]], attr) + (r["n"] or 0))

    reuse = impacts.filter(indicator=SustainabilityImpact.REUSE)
    add(reuse.exclude(source_department__isnull=True), "source_department", "given")
    add(reuse, "department", "received")
    # Diversion records also exist for reuse and repair events (SUS-05); count only donation,
    # recycling and component recovery here so no unit earns points twice.
    other_events = SustainabilityImpact.objects.filter(indicator__in=[SustainabilityImpact.REUSE, SustainabilityImpact.REPAIR]).values("event_id")
    add(impacts.filter(indicator=SustainabilityImpact.DIVERSION).exclude(event_id__in=other_events), "department", "diverted")
    add(impacts.filter(indicator=SustainabilityImpact.REPAIR), "department", "repairs", value=None)
    for r in reviews.select_related("transfer__reservation__request"):
        dept_id = r.transfer.reservation.request.department_id
        if dept_id in rows and r.avoided_value is not None:
            rows[dept_id].avoided_aed += r.avoided_value
    ranked = sorted((r.compute() for r in rows.values()), key=lambda r: (-r.points, r.department.name))
    last_points, last_rank = None, 0
    for i, r in enumerate(ranked, start=1):
        if r.points != last_points:
            last_rank, last_points = i, r.points
        r.rank = last_rank
    return ranked, start
