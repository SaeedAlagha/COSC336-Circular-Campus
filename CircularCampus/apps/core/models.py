"""Shared infrastructure tables (not business entities)."""
import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class ReferenceCounter(models.Model):
    """Sequential human-readable references (R-0001, T-0001 ...) issued under a row lock."""

    name = models.CharField(max_length=20, primary_key=True)
    value = models.PositiveIntegerField(default=0)


class IdempotencyRecord(models.Model):
    """One row per (actor, key): a repeated submission returns the original result (NFR-03)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    key = models.UUIDField()
    command = models.CharField(max_length=60)
    payload_hash = models.CharField(max_length=64)
    response = models.JSONField(encoder=DjangoJSONEncoder, default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["actor", "key"], name="uniq_actor_idempotency_key")]
