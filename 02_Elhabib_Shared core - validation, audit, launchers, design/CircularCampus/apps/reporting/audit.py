"""Append-only audit and in-app notification helpers (process 7.0, store D8).

Both are called inside the business transaction so the business change, its
audit event and its notices commit together (NFR-03). A notification failure
is isolated in a savepoint and never reverses a valid committed action (BR-16).
"""
import logging
from decimal import Decimal

from django.db import transaction

from .models import AuditEvent, Notification

log = logging.getLogger("campus.audit")


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in {"password", "password_hash", "api_key"}}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "pk"):
        return str(value.pk)
    return value


def record(actor, action, record_type, obj=None, *, before=None, after=None, outcome=AuditEvent.SUCCESS, reason="", service_actor="", label=""):
    record_id = ""
    if obj is not None:
        record_id = str(getattr(obj, "pk", obj))
        label = label or getattr(obj, "reference", "") or getattr(obj, "asset_tag", "") or getattr(obj, "campus_id", "") or getattr(obj, "code", "")
    return AuditEvent.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        service_actor=service_actor if not getattr(actor, "pk", None) else "",
        action=action,
        record_type=record_type,
        record_id=record_id,
        record_label=str(label)[:80],
        before=_clean(before or {}),
        after=_clean(after or {}),
        outcome=outcome,
        reason=reason[:2000],
    )


def denied(user, action, path="", reason=""):
    """FR-04: log denied attempts without secrets or request bodies."""
    try:
        with transaction.atomic():
            record(user if getattr(user, "is_authenticated", False) else None, f"DENIED {action}"[:60], "access",
                   outcome=AuditEvent.DENIED, reason=reason or f"Path {path[:150]}", service_actor="anonymous-request")
    except Exception:  # pragma: no cover - logging must never break the response
        log.exception("Could not record denied access")


def notify(recipients, code, title, body="", obj=None, link=""):
    seen = set()
    for user in recipients:
        if user is None or user.pk in seen or not user.is_active:
            continue
        seen.add(user.pk)
        try:
            with transaction.atomic():
                Notification.objects.create(
                    recipient=user, event_code=code, title=title[:160], body=body[:400],
                    record_type=obj.__class__.__name__ if obj is not None else "",
                    record_id=str(getattr(obj, "pk", "")), link=link[:200],
                )
        except Exception:  # BR-16: a failed notice never reverses the business change
            log.exception("Notification delivery failed for %s", code)
