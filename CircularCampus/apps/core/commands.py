"""Command execution helper: one transaction, idempotent retries (design 5.9.1/5.9.2).

Business state, audit events and notifications are written inside the same
``transaction.atomic`` block, so they commit together or not at all (NFR-03).
"""
import hashlib
import json
import uuid

from django.db import transaction

from .errors import BusinessError
from .models import IdempotencyRecord


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def parse_key(raw):
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        raise BusinessError(BusinessError.VALIDATION_ERROR, "The form submission key is invalid. Reload the page and try again.")


def execute(actor, command, key, payload, fn):
    """Run ``fn()`` atomically. Returns (response_dict, replayed: bool).

    ``fn`` must return a JSON-serialisable dict describing the committed result.
    A retry with the same actor+key+payload returns the stored response without
    repeating the effect; the same key with a different payload is rejected.
    """
    key = parse_key(key)
    digest = payload_hash(payload)
    with transaction.atomic():
        if key is not None:
            existing = IdempotencyRecord.objects.select_for_update().filter(actor=actor, key=key).first()
            if existing:
                if existing.payload_hash != digest or existing.command != command:
                    raise BusinessError(BusinessError.IDEMPOTENCY_CONFLICT, "This form was already used for a different action. Reload the page to get a fresh form.")
                return existing.response, True
        response = fn() or {}
        if key is not None:
            IdempotencyRecord.objects.create(actor=actor, key=key, command=command, payload_hash=digest, response=response)
        return response, False
