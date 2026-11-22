from django.apps import AppConfig

# Apps whose free-text columns hold what people typed (AI outputs and hashes are excluded).
GUARDED_APPS = {"accounts", "catalog", "exchange", "workflow", "maintenance", "reporting"}
EXEMPT_FIELDS = {"password", "storage_key", "checksum_sha256", "snapshot_hash", "narrative_original", "link", "mime_type"}


def guard_text_fields(sender, instance, **kwargs):
    """Last line of defence (NFR-10): no stored text may contain control characters or HTML/script
    markup, and long text is capped — whichever code path wrote it."""
    from django.db import models

    from .errors import BusinessError
    from .validators import _CONTROL, _MARKUP, MAX_NARRATIVE

    for field in sender._meta.concrete_fields:
        if not isinstance(field, (models.CharField, models.TextField)) or field.name in EXEMPT_FIELDS:
            continue
        value = getattr(instance, field.attname, None)
        if not isinstance(value, str) or not value:
            continue
        label = str(field.verbose_name).capitalize()
        if _CONTROL.search(value):
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"{label} contains invisible control characters.", {field.name: "Remove control characters."})
        if _MARKUP.search(value):
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"{label} cannot contain HTML or script markup.", {field.name: "Remove the markup."})
        if isinstance(field, models.TextField) and len(value) > MAX_NARRATIVE:
            raise BusinessError(BusinessError.VALIDATION_ERROR, f"{label} is too long.", {field.name: f"At most {MAX_NARRATIVE} characters."})


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self):
        from django.apps import apps
        from django.db.models.signals import pre_save

        for model in apps.get_models():
            if model._meta.app_label in GUARDED_APPS:
                pre_save.connect(guard_text_fields, sender=model, dispatch_uid=f"guard-text-{model._meta.label}")
