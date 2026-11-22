from django import forms

from apps.accounts.models import Department, Role, User
from apps.core import validators as V
from apps.core.validators import CleanTextMixin, form_validator as fv

from .models import TAG_VALIDATOR, AssetStatus, Category, Condition

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class CategoryChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(obj)


class AssetForm(CleanTextMixin, forms.Form):
    multiline_fields = ("description", "specifications", "reason")
    letter_optional = ("asset_tag", "specifications", "tags", "campus", "building", "room", "serial_number", "ports", "dimensions", "ai_choice", "ai_suggested")

    asset_tag = forms.CharField(label="Asset ID", max_length=24, validators=[TAG_VALIDATOR], help_text="Unique, e.g. A-104.")
    name = forms.CharField(max_length=160, min_length=3)
    description = forms.CharField(min_length=5, max_length=V.MAX_LONG, widget=forms.Textarea(attrs={"rows": 3}))
    specifications = forms.CharField(max_length=V.MAX_LONG, widget=forms.Textarea(attrs={"rows": 2}), help_text="Required (FR-05). Separate with semicolons, e.g. 24-inch; HDMI.")
    tags = forms.CharField(required=False, max_length=300, validators=[fv(V.keywords)], help_text="Comma-separated keywords, e.g. monitor, HDMI, 24-inch.")
    category = CategoryChoiceField(queryset=Category.objects.filter(active=True, parent__isnull=False).select_related("parent").order_by("parent__name", "name"))
    quantity = forms.IntegerField(min_value=1, max_value=V.MAX_ASSET_QTY, help_text="Whole units in this batch (1–10,000).")
    owner_department = forms.ModelChoiceField(queryset=Department.objects.none(), label="Owner department")
    custodian = forms.ModelChoiceField(queryset=User.objects.none(), required=False, help_text="Defaults to you.")
    campus = forms.CharField(max_length=80, initial="Main Campus", validators=[fv(V.place, field="campus")])
    building = forms.CharField(max_length=80, validators=[fv(V.place, field="building")])
    room = forms.CharField(max_length=40, validators=[fv(V.place, field="room")], help_text="e.g. 2.14 or Lab B")
    condition = forms.ChoiceField(choices=Condition.CHOICES)
    purchase_date = forms.DateField(required=False, widget=DATE, help_text="Leave empty if unknown — it stays “Unknown”.")
    unit_value_aed = forms.DecimalField(required=False, min_value=0, max_value=V.MAX_MONEY, decimal_places=2, max_digits=12, label="Current value per unit (AED)", help_text="Empty = unknown.")
    replacement_value_aed = forms.DecimalField(required=False, min_value=0, max_value=V.MAX_MONEY, decimal_places=2, max_digits=12, label="New-purchase price per unit (AED)")
    estimated_transfer_cost_aed = forms.DecimalField(required=False, min_value=0, max_value=V.MAX_MONEY, decimal_places=2, max_digits=10, label="Estimated transfer cost (AED)")
    unit_weight_kg = forms.DecimalField(required=False, min_value=0.01, max_value=V.MAX_WEIGHT_KG, decimal_places=2, max_digits=8, label="Weight per unit (kg)")
    available_from = forms.DateField(required=False, widget=DATE, label="Ready for reuse from")
    expected_retirement_date = forms.DateField(required=False, widget=DATE, label="Expected retirement (baseline)")
    serial_number = forms.CharField(required=False, max_length=60, validators=[fv(V.serial_number)], help_text="IT equipment only, e.g. SN-48213-B.")
    ports = forms.CharField(required=False, max_length=120, validators=[fv(V.ports)], help_text="IT equipment only. Comma-separated from: HDMI, DisplayPort, VGA, DVI, USB-A, USB-C, Thunderbolt, Ethernet, Audio jack, SD card, Power (e.g. 2x USB-C, HDMI).")
    material = forms.CharField(required=False, max_length=80, validators=[fv(V.material)], help_text="Furniture only, e.g. Oak, Steel.")
    dimensions = forms.CharField(required=False, max_length=60, validators=[fv(V.dimensions)], help_text="Furniture only, e.g. 140 x 70 cm or 120 x 60 x 75 cm.")
    reason = forms.CharField(required=False, max_length=300, label="Reason for correction", help_text="Required when correcting condition, quantity or location of a published asset.")
    ai_result = forms.UUIDField(required=False, widget=forms.HiddenInput)
    ai_choice = forms.CharField(required=False, widget=forms.HiddenInput)
    ai_suggested = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, user=None, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts import policy

        scopes = policy.scoped_departments(user, Role.CUSTODIAN) if user else set()
        depts = Department.objects.filter(active=True) if scopes is None else Department.objects.filter(pk__in=scopes or [], active=True)
        self.fields["owner_department"].queryset = depts
        self.fields["custodian"].queryset = User.objects.filter(is_active=True, role_grants__role__code=Role.CUSTODIAN, role_grants__active=True).distinct()
        if editing:
            for f in ("asset_tag", "owner_department", "custodian"):
                self.fields.pop(f)
        else:
            self.fields.pop("reason")

    def clean(self):
        data = super().clean()
        if data.get("purchase_date") and data.get("expected_retirement_date") and data["expected_retirement_date"] < data["purchase_date"]:
            self.add_error("expected_retirement_date", "Retirement cannot be before the purchase date.")
        from datetime import date

        if data.get("purchase_date") and data["purchase_date"] > date.today():
            self.add_error("purchase_date", "Purchase date cannot be in the future.")
        if data.get("purchase_date") and data["purchase_date"].year < 1970:
            self.add_error("purchase_date", "Purchase date looks wrong (before 1970).")
        if data.get("available_from"):
            if data.get("purchase_date") and data["available_from"] < data["purchase_date"]:
                self.add_error("available_from", "An item cannot be ready for reuse before it was purchased.")
            elif data["available_from"] > V.years_from_today(2):
                self.add_error("available_from", "Choose a date within the next two years.")
        if data.get("expected_retirement_date") and data["expected_retirement_date"] > V.years_from_today(30):
            self.add_error("expected_retirement_date", "Expected retirement must be within 30 years.")
        if data.get("unit_value_aed") is not None and data.get("replacement_value_aed") is not None and data["unit_value_aed"] > data["replacement_value_aed"]:
            self.add_error("unit_value_aed", "Current value cannot exceed the new-purchase price.")
        return self.cleaned_data

    def service_data(self):
        d = dict(self.cleaned_data)
        d["type_metadata"] = {k: d.pop(k, "") for k in ("serial_number", "ports", "material", "dimensions")}
        for k in ("ai_result", "ai_choice", "ai_suggested", "reason"):
            d.pop(k, None)
        return d


class AttachmentForm(forms.Form):
    file = forms.FileField(help_text="JPEG, PNG or PDF, up to 10 MB.")

