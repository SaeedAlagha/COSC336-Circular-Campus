"""Bonus feature — QR labels. Generated on the server with segno (BSD licence); no external service.

The code holds a link such as https://campus.example/q/A-101/ — any phone camera opens the
asset page (after sign-in and the normal permission check). The in-app scanner reads the same
link to confirm the right batch at handover.
"""
from __future__ import annotations

import segno
from django.urls import reverse
from django.utils.safestring import mark_safe


def label_url(request, asset):
    return request.build_absolute_uri(reverse("catalog:qr_lookup", args=[asset.asset_tag]))


def svg(request, asset, scale=4):
    code = segno.make(label_url(request, asset), error="m")
    # Server-generated markup from a fixed library; the only variable part is our own URL.
    return mark_safe(code.svg_inline(scale=scale, dark="#0b1f1c", light="#ffffff", title=f"QR label for {asset.asset_tag}"))
