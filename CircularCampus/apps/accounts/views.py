from django.contrib import messages
from django.contrib.auth import login, logout
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.core import validators as V
from apps.core.errors import BusinessError
from apps.core.views import AppView, apply_errors
from apps.reporting import audit

from . import policy, services
from .forms import DepartmentForm, GrantForm, PasswordResetForm, SignInForm, UserCreateForm, UserEditForm
from .models import Department, User, UserRole


def demo_users():
    from django.conf import settings

    if not settings.CAMPUS["DEMO_MODE"]:
        return []
    rows = []
    from django.utils.translation import gettext as _

    for u in User.objects.filter(is_active=True).select_related("department").prefetch_related("role_grants__role", "role_grants__scope_department").order_by("campus_id")[:20]:
        # One line: "Computer Science · Requester". Scope is added only when it differs from the person's
        # own department, e.g. "Engineering · Assigned Approver (campus-wide)".
        roles = []
        for g in u.role_grants.all():
            if not g.active:
                continue
            if g.scope_department_id is None:
                roles.append(f"{_(g.role.name)} ({_('campus-wide')})")
            elif g.scope_department_id != u.department_id:
                roles.append(f"{_(g.role.name)} ({g.scope_department.name})")
            else:
                roles.append(_(g.role.name))
        rows.append((u.campus_id, u.full_name, f"{u.department.name} · {', '.join(sorted(roles)) or _('no roles yet')}"))
    return rows


class SignInView(View):
    def get(self, request):
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return render(request, "accounts/login.html", {"form": SignInForm(request), "demo_users": demo_users()})

    def post(self, request):
        form = SignInForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.user)
            audit.record(form.user, "Signed in", "account", form.user)
            nxt = request.POST.get("next") or request.GET.get("next")
            if nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}):
                return redirect(nxt)
            return redirect("core:dashboard")
        audit.record(None, "Sign-in failed", "access", outcome="DENIED", service_actor="anonymous-request", reason="Invalid credentials or inactive account")
        return render(request, "accounts/login.html", {"form": form, "demo_users": demo_users()}, status=200)


class SignOutView(View):
    def post(self, request):
        if request.user.is_authenticated:
            audit.record(request.user, "Signed out", "account", request.user)
        logout(request)
        messages.success(request, "You have signed out.")
        return redirect("accounts:login")


class AdminMixin(AppView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            policy.require(policy.can_admin_accounts(request.user), "Only system administrators can manage accounts.", "admin")
        return super().dispatch(request, *args, **kwargs)


class UserListView(AdminMixin):
    template_name = "accounts/users.html"
    nav = "users"

    def get(self, request):
        q = request.GET.get("q", "").strip()[:60]
        users = User.objects.select_related("department").prefetch_related("role_grants__role", "role_grants__scope_department")
        if q:
            users = users.filter(full_name__icontains=q) | users.filter(campus_id__icontains=q)
        return self.render(request, {"users": users.order_by("campus_id"), "q": q, "form": UserCreateForm()})

    def post(self, request):
        form = UserCreateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            try:
                user = services.create_user(request.user, d["campus_id"], d["full_name"], d["department"], d["email"], d["password1"], d["password2"])
                messages.success(request, f"Account {user.campus_id} created. Grant roles next.")
                return redirect("accounts:user", pk=user.pk)
            except BusinessError as exc:
                apply_errors(form, exc)
        users = User.objects.select_related("department").order_by("campus_id")
        return self.render(request, {"users": users, "form": form, "open_create": True})


class UserDetailView(AdminMixin):
    template_name = "accounts/user_detail.html"
    nav = "users"

    def ctx(self, user, **extra):
        grants = user.role_grants.select_related("role", "scope_department").order_by("-active", "role__name")
        base = {"subject": user, "grants": grants, "edit_form": UserEditForm(initial={"full_name": user.full_name, "email": user.email, "department": user.department}),
                "grant_form": GrantForm(), "pw_form": PasswordResetForm()}
        base.update(extra)
        return base

    def get(self, request, pk):
        return self.render(request, self.ctx(get_object_or_404(User, pk=pk)))

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        action = request.POST.get("action")
        try:
            if action == "edit":
                form = UserEditForm(request.POST)
                if not form.is_valid():
                    return self.render(request, self.ctx(user, edit_form=form))
                services.update_user(request.user, user, form.cleaned_data["full_name"], form.cleaned_data["email"], form.cleaned_data["department"])
                messages.success(request, "Account updated.")
            elif action == "grant":
                form = GrantForm(request.POST)
                if not form.is_valid():
                    return self.render(request, self.ctx(user, grant_form=form))
                services.grant_role(request.user, user, form.cleaned_data["role"], form.cleaned_data["scope_department"])
                messages.success(request, "Role granted.")
            elif action == "revoke":
                grant = get_object_or_404(UserRole, pk=V.uuid_or_404(request.POST.get("grant")), user=user)
                services.revoke_role(request.user, grant)
                messages.success(request, "Role revoked.")
            elif action in ("deactivate", "activate"):
                services.set_active(request.user, user, action == "activate", request.POST.get("reason", ""))
                messages.success(request, f"Account {'reactivated' if action == 'activate' else 'deactivated'}.")
            elif action == "password":
                form = PasswordResetForm(request.POST)
                if not form.is_valid():
                    return self.render(request, self.ctx(user, pw_form=form))
                services.reset_password(request.user, user, form.cleaned_data["password1"], form.cleaned_data["password2"])
                messages.success(request, "Password reset.")
            else:
                messages.error(request, "Unknown action.")
        except BusinessError as exc:
            if action == "password":
                return self.render(request, self.ctx(user, pw_form=apply_errors(PasswordResetForm(request.POST), exc)))
            if action == "edit":
                return self.render(request, self.ctx(user, edit_form=apply_errors(UserEditForm(request.POST), exc)))
            messages.error(request, exc.message)
        return redirect("accounts:user", pk=user.pk)


class DepartmentListView(AdminMixin):
    template_name = "accounts/departments.html"
    nav = "departments"

    def get(self, request):
        return self.render(request, {"departments": Department.objects.select_related("default_approver").order_by("name"), "form": DepartmentForm()})

    def post(self, request):
        action = request.POST.get("action", "create")
        try:
            if action in ("deactivate", "activate"):
                dept = get_object_or_404(Department, pk=V.uuid_or_404(request.POST.get("dept")))
                services.set_department_active(request.user, dept, action == "activate")
                messages.success(request, f"{dept.name} {'reactivated' if action == 'activate' else 'deactivated'}; its records are kept.")
                return redirect("accounts:departments")
            dept = get_object_or_404(Department, pk=V.uuid_or_404(request.POST.get("dept"))) if action == "update" else None
            form = DepartmentForm(request.POST)
            if form.is_valid():
                d = form.cleaned_data
                services.save_department(request.user, dept, d["code"], d["name"], d["campus"], d["building"], d["default_approver"])
                messages.success(request, "Department saved.")
                return redirect("accounts:departments")
        except BusinessError as exc:
            form = apply_errors(DepartmentForm(request.POST), exc)
        return self.render(request, {"departments": Department.objects.order_by("name"), "form": form, "open_create": True})
