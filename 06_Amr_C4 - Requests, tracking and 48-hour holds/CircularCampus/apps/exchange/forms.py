from datetime import date

from django import forms

from apps.accounts.models import Role, User
from apps.catalog.forms import CategoryChoiceField
from apps.catalog.models import Category, Condition
from apps.core import validators as V
from apps.core.validators import CleanTextMixin, form_validator as fv

from .models import Urgency

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class RequestForm(CleanTextMixin, forms.Form):
    multiline_fields = ("purpose", "specifications")
    letter_optional = ("specifications", "mandatory_specs", "destination_campus", "destination_building", "destination_room")

    title = forms.CharField(max_length=160, min_length=3, help_text="A short name for the need, e.g. “Monitors for the robotics lab”.")
    category = CategoryChoiceField(queryset=Category.objects.filter(active=True, parent__isnull=False).select_related("parent").order_by("parent__name", "name"))
    purpose = forms.CharField(min_length=5, max_length=V.MAX_LONG, widget=forms.Textarea(attrs={"rows": 3}), help_text="What the items will be used for — used for semantic matching.")
    specifications = forms.CharField(required=False, max_length=V.MAX_LONG, widget=forms.Textarea(attrs={"rows": 2}), help_text="Desired specifications.")
    mandatory_specs = forms.CharField(required=False, max_length=300, validators=[fv(V.keywords, field="mandatory_specs", label="Mandatory specifications", max_items=10)], label="Mandatory specifications", help_text="Comma-separated terms every candidate must state, e.g. HDMI. Safety and mandatory specs outrank a high score.")
    quantity = forms.IntegerField(min_value=1, max_value=V.MAX_REQUEST_QTY, help_text="Whole number from 1 to 1,000.")
    preferred_condition = forms.ChoiceField(choices=Condition.CHOICES, initial=Condition.GOOD, label="Minimum condition")
    urgency = forms.ChoiceField(choices=Urgency.CHOICES, initial=Urgency.NORMAL)
    required_date = forms.DateField(widget=DATE, help_text="From today up to two years ahead.")
    destination_campus = forms.CharField(max_length=80, initial="Main Campus", label="Destination campus", validators=[fv(V.place, field="destination_campus")])
    destination_building = forms.CharField(max_length=80, label="Building", validators=[fv(V.place, field="destination_building")])
    destination_room = forms.CharField(max_length=40, label="Room", validators=[fv(V.place, field="destination_room")])
    receiving_custodian = forms.ModelChoiceField(queryset=User.objects.none(), help_text="Custodian in your department who will confirm receipt.")
    allow_partial = forms.BooleanField(required=False, label="Accept partial fulfilment", help_text="Allow smaller batches; each part gets its own approval and transfer.")

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts import policy

        users = User.objects.filter(is_active=True, role_grants__role__code=Role.CUSTODIAN, role_grants__active=True).distinct()
        self.fields["receiving_custodian"].queryset = User.objects.filter(pk__in=[u.pk for u in users if department and policy.has_role(u, Role.CUSTODIAN, department.pk)])

    def clean_required_date(self):
        d = self.cleaned_data["required_date"]
        if d < date.today():
            raise forms.ValidationError("The required date cannot be before today.")
        if d > V.years_from_today(2):
            raise forms.ValidationError("Choose a date within the next two years.")
        return d


class SubmitForm(forms.Form):
    asset = forms.UUIDField(required=False)
    quantity = forms.IntegerField(required=False, min_value=1, max_value=V.MAX_REQUEST_QTY)
    match = forms.UUIDField(required=False)


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), min_length=5, max_length=V.MAX_SHORT, validators=[fv(V.reason)])
