class SecurityHeadersMiddleware:
    """Adds a restrictive Content-Security-Policy: all styles, scripts and fonts are local."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        )
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response


class DeniedAccessAuditMiddleware:
    """FR-04: log every denied protected request (direct URL or POST included)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.core.exceptions import PermissionDenied

        if isinstance(exception, PermissionDenied):
            from apps.reporting.audit import denied

            denied(getattr(request, "user", None), getattr(exception, "action", "") or request.method, request.path, getattr(exception, "safe_message", ""))
        return None


class BadInputMiddleware:
    """Safety net for NFR-10: a malformed value that slips past a form (e.g. a hand-edited
    hidden field) becomes a friendly 400/404 page, never a server error. Nothing is changed,
    because every command runs inside a transaction that is rolled back on the exception."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.core.exceptions import ObjectDoesNotExist, ValidationError
        from django.http import Http404
        from django.shortcuts import render

        from .errors import BusinessError

        if isinstance(exception, ObjectDoesNotExist):
            return render(request, "errors/404.html", status=404)
        if isinstance(exception, ValidationError):
            return render(request, "errors/400.html", status=400)
        if isinstance(exception, BusinessError):
            return render(request, "errors/400.html", {"message": exception.message}, status=400)
        if isinstance(exception, Http404):
            return None
        return None
