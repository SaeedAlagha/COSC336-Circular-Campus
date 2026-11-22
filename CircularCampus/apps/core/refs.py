from .models import ReferenceCounter


def next_reference(prefix, width=4):
    """Return the next reference for ``prefix``; call inside a transaction."""
    counter, _ = ReferenceCounter.objects.select_for_update().get_or_create(name=prefix)
    counter.value += 1
    counter.save(update_fields=["value"])
    return f"{prefix}-{counter.value:0{width}d}"
