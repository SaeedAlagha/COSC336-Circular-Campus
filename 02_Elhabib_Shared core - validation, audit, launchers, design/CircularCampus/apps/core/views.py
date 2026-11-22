"""Presentation-layer base classes and the role-aware dashboard.

Views stay thin: they parse forms, call one application service, and translate
BusinessError codes into field errors / messages. No business rule lives here.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from urllib.parse import urlparse

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponseRedirect
from django.shortcuts import render
from apps.core.links import reverse  # safe: "" if a page is not installed
from django.utils import timezone
from django.views import View

from apps.accounts import policy
from apps.accounts.models import Role

from .commands import execute
from .errors import BusinessError


# [UML: Inheritance — every screen class inherits AppView (login + command handling)]
class AppView(LoginRequiredMixin, View):
    """Authenticated view; subclasses must also call policy checks for records."""

    template_name = None
    nav = ""

    def render(self, request, context=None, status=200):
        ctx = {"nav": self.nav, "form_key": uuid.uuid4()}
        ctx.update(context or {})
        return render(request, self.template_name, ctx, status=status)

    def command(self, request, name, payload, fn):
        """Run an idempotent business command keyed by the form's hidden ``idem`` field."""
        response, replayed = execute(request.user, name, request.POST.get("idem"), payload, fn)
        if replayed:
            messages.info(request, "This form was already submitted — showing the original result (no duplicate effect).")
        return response

    @staticmethod
    def back(request, fallback):
        ref = request.META.get("HTTP_REFERER", "")
        if ref and urlparse(ref).netloc in ("", request.get_host()):
            return HttpResponseRedirect(ref)
        return HttpResponseRedirect(fallback)


def apply_errors(form, exc: BusinessError):
    """Attach a BusinessError's field errors to a bound Django form; the rest goes to non-field errors."""
    placed = False
    for field, msg in exc.field_errors.items():
        if form is not None and field in form.fields:
            form.add_error(field, msg)
            placed = True
    if form is not None and (not placed or not exc.field_errors):
        form.add_error(None, exc.message)
    return form


def fail(request, exc: BusinessError):
    messages.error(request, exc.message)


def error_403(request, exception=None):
    message = getattr(exception, "safe_message", "") or "You do not have permission to view this page or perform this action."
    return render(request, "errors/403.html", {"message": message}, status=403)


def error_404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)
