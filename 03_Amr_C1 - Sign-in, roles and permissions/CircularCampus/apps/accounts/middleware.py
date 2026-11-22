from django.contrib.auth import logout
from django.contrib import messages
from django.shortcuts import redirect


class ActiveAccountMiddleware:
    """Ends a session immediately when the account or its department is deactivated (UC-07 step 5)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and (not user.is_active or not user.department.active):
            logout(request)
            messages.error(request, "Your account is not active. Contact the system administrator.")
            return redirect("accounts:login")
        return self.get_response(request)
