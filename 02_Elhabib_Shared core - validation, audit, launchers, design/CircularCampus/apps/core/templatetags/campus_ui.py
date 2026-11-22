"""Design-system helpers: icons, status badges, money and quantity formatting."""
from decimal import Decimal

from django import template
from django.utils.html import format_html

register = template.Library()

TONES = {
    # assets
    "DRAFT": "neutral", "AVAILABLE": "success", "RESERVED": "info", "TRANSFER_PENDING": "info", "IN_USE": "brand",
    "UNDER_INSPECTION": "warning", "IN_MAINTENANCE": "warning", "UNAVAILABLE": "danger", "DISPOSITION_PENDING": "warning",
    "RETIRED": "neutral", "COMPONENT_RECOVERED": "neutral", "DONATED": "brand", "RECYCLED": "brand", "DISPOSED": "neutral",
    # requests / reservations / proposals
    "SUBMITTED": "info", "UNDER_REVIEW": "info", "RETURNED": "warning", "APPROVED": "success", "PARTIALLY_FULFILLED": "brand",
    "FULFILLED": "success", "REJECTED": "danger", "CANCELLED": "neutral", "PENDING_APPROVAL": "info", "ALLOCATED": "success",
    "CONSUMED": "neutral", "EXPIRED": "neutral", "PENDING": "info", "SUPERSEDED": "neutral", "WITHDRAWN": "neutral",
    "NOT_APPLICABLE": "neutral",
    # transfers
    "SCHEDULED": "info", "RELEASED": "warning", "EXCEPTION": "danger", "COMPLETED": "success",
    # maintenance / disposition
    "REPORTED": "warning", "AWAITING_APPROVAL": "info", "IN_SERVICE": "warning", "AWAITING_VERIFICATION": "info", "UNREPAIRABLE": "danger",
    "PROPOSED": "info",
    # reports / metrics
    "PUBLISHED": "success", "REVIEWED": "info", "available": "success", "provisional": "warning", "unavailable": "neutral",
}
ICONS = {"success": "circle-check", "info": "circle-dot", "warning": "clock", "danger": "triangle-alert", "neutral": "circle-dot", "brand": "circle-dot", "ai": "sparkles"}


@register.simple_tag
def icon(name, size=18, cls=""):
    return format_html('<svg class="icon {}" width="{}" height="{}" aria-hidden="true" focusable="false"><use href="#i-{}"></use></svg>', cls, size, size, name)


@register.simple_tag
def badge(code, label=None):
    tone = TONES.get(str(code), "neutral")
    from django.utils.translation import gettext

    text = gettext(str(label or str(code).replace("_", " ").title()))
    return format_html('<span class="badge badge-{}"><svg class="icon" width="12" height="12" aria-hidden="true"><use href="#i-{}"></use></svg>{}</span>', tone, ICONS[tone], text)


@register.filter
def aed(value):
    if value is None or value == "":
        return "Unknown"
    try:
        return f"AED {Decimal(value):,.0f}" if Decimal(value) == Decimal(value).to_integral() else f"AED {Decimal(value):,.2f}"
    except Exception:
        return value


@register.filter
def unknown(value, label="Unknown"):
    return label if value in (None, "") else value


@register.filter
def pct(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "—"


@register.filter
def get(mapping, key):
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def widthpct(value):
    try:
        return f"{max(0.0, min(100.0, float(value))):.1f}"
    except (TypeError, ValueError):
        return "0"


import re as _re
from datetime import datetime as _dt

_ISO = _re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_UUID = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@register.filter
def readable_items(mapping):
    """Human-readable before/after pairs: hide internal IDs, format timestamps."""
    out = []
    for k, v in (mapping or {}).items():
        if k.endswith("_id") or k in ("department",) or (isinstance(v, str) and _UUID.match(v)):
            continue
        if isinstance(v, str) and _ISO.match(v):
            try:
                v = _dt.fromisoformat(v).strftime("%d %b %Y %H:%M")
            except ValueError:
                pass
        if v is True:
            v = "yes"
        elif v is False:
            v = "no"
        elif v is None or v == "":
            v = "—"
        out.append((k.replace("_", " "), v))
    return out


@register.filter
def installed(urlname):
    """True when a page is installed. Used to show a link only once its feature exists."""
    from django.urls import NoReverseMatch, reverse

    try:
        reverse(urlname)
        return True
    except NoReverseMatch:
        # Pages that need arguments: a NoReverseMatch with args still means the name exists.
        from django.urls import get_resolver

        ns, _, name = urlname.rpartition(":")
        resolver = get_resolver()
        try:
            for part in filter(None, ns.split(":")):
                resolver = resolver.namespace_dict[part][1]
        except KeyError:
            return False
        return name in resolver.reverse_dict
