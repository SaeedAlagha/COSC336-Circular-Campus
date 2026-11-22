import re

from django import forms
from django.contrib.auth import authenticate
from django.core.validators import RegexValidator

from apps.core import validators as V
from apps.core.validators import CleanTextMixin, form_validator as fv

from .models import Department, Role, User


class SignInForm(forms.Form):
    campus_id = forms.CharField(label="Campus ID", max_length=20, strip=True, widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True, "placeholder": "e.g. 100060412", "inputmode": "numeric", "maxlength": "9"}))
    password = forms.CharField(label="Password", max_length=128, strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean_campus_id(self):
        # Format check only; it never reveals whether an account exists (FR-01).
        try:
            return V.campus_id(self.cleaned_data.get("campus_id"))
        except Exception as exc:  # BusinessError
            raise forms.ValidationError(getattr(exc, "message", "Enter your 9-digit university ID."))

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        user = authenticate(self.request, username=re.sub(r"\s+", "", data["campus_id"]), password=data["password"])
        # One generic message: never reveal whether the ID exists or is inactive (FR-01).
        if user is None or not user.is_active or not user.department.active:
            raise forms.ValidationError("Sign-in failed. Check your campus ID and password, or contact the system administrator.")
        self.user = user
        return data


class UserCreateForm(CleanTextMixin, forms.Form):
    letter_optional = ("campus_id", "email")

    campus_id = forms.CharField(label="Campus ID", max_length=12, help_text="Unique 9-digit university ID starting with 1000, e.g. 100060412.",
                                validators=[fv(V.campus_id)], widget=forms.TextInput(attrs={"inputmode": "numeric", "placeholder": "100060412"}))
    full_name = forms.CharField(max_length=120, validators=[fv(V.person_name)], help_text="Letters, spaces, apostrophes, hyphens and dots.")
    email = forms.EmailField(required=False, max_length=254, help_text="Optional; must be a valid address when given.")
    department = forms.ModelChoiceField(queryset=Department.objects.filter(active=True))
    password1 = forms.CharField(label="Password", max_length=128, strip=False, widget=forms.PasswordInput, help_text="At least 12 characters; passphrases with spaces are welcome.")
    password2 = forms.CharField(label="Confirm password", max_length=128, strip=False, widget=forms.PasswordInput)


class UserEditForm(CleanTextMixin, forms.Form):
    letter_optional = ("email",)
    full_name = forms.CharField(max_length=120, validators=[fv(V.person_name)])
    email = forms.EmailField(required=False, max_length=254)
    department = forms.ModelChoiceField(queryset=Department.objects.all())


class GrantForm(forms.Form):
    role = forms.ChoiceField(choices=Role.CHOICES)
    scope_department = forms.ModelChoiceField(queryset=Department.objects.filter(active=True), required=False, empty_label="Campus-wide (explicit)",
                                              help_text="Leave empty only for an approved campus-wide grant.")


class PasswordResetForm(forms.Form):
    password1 = forms.CharField(label="New password", max_length=128, strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(label="Confirm password", max_length=128, strip=False, widget=forms.PasswordInput)


class DepartmentForm(CleanTextMixin, forms.Form):
    letter_optional = ("campus", "building")
    code = forms.CharField(max_length=12, help_text="2–12 capital letters, e.g. ENG.",
                           validators=[RegexValidator(r"^[A-Za-z]{2,12}$", "Use 2–12 letters only, e.g. ENG.")])
    name = forms.CharField(max_length=120, min_length=3)
    campus = forms.CharField(max_length=80, initial="Main Campus", validators=[fv(V.place, field="campus")])
    building = forms.CharField(max_length=80, required=False, validators=[fv(V.place, field="building", required=False)])
    default_approver = forms.ModelChoiceField(queryset=User.objects.filter(is_active=True, role_grants__role__code=Role.APPROVER, role_grants__active=True).distinct(), required=False)
