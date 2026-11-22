"""D1 Identities and departments (design 5.3.1, Figure 20).

Department 1--0..* User; User 1--0..* UserRole; Role 1--0..* UserRole;
UserRole.scope_department is optional — NULL means an explicitly granted
campus-wide scope, never an accidental default.
"""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Department(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=12, unique=True, validators=[RegexValidator(r"^[A-Z]{2,12}$", "Use 2–12 capital letters, e.g. ENG.")])
    name = models.CharField(max_length=120, unique=True)
    campus = models.CharField(max_length=80, default="Main Campus")
    building = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)
    # [UML: Association — Department 0..* — 0..1 User as default approver (design 4.5 assigned approver)]
    default_approver = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Assigned approver for this department's proposals (BR-02).",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Role(models.Model):
    REQUESTER = "REQUESTER"
    DEPT_REP = "DEPT_REP"
    CUSTODIAN = "CUSTODIAN"
    APPROVER = "APPROVER"
    MAINTENANCE = "MAINTENANCE"
    PROCUREMENT = "PROCUREMENT"
    FINANCE = "FINANCE"
    SUSTAINABILITY = "SUSTAINABILITY"
    SYSADMIN = "SYSADMIN"
    AUDITOR = "AUDITOR"
    CHOICES = [
        (REQUESTER, "Requester"),
        (DEPT_REP, "Department Representative"),
        (CUSTODIAN, "Asset Custodian"),
        (APPROVER, "Assigned Approver"),
        (MAINTENANCE, "Maintenance Staff"),
        (PROCUREMENT, "Procurement Officer"),
        (FINANCE, "Finance Officer"),
        (SUSTAINABILITY, "Sustainability Officer"),
        (SYSADMIN, "System Administrator"),
        (AUDITOR, "Management / Auditor"),
    ]
    REPORTING = {PROCUREMENT, FINANCE, SUSTAINABILITY}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=20, unique=True, choices=CHOICES)
    name = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, campus_id, full_name, department, password=None, **extra):
        if not campus_id:
            raise ValueError("A campus ID is required.")
        user = self.model(campus_id=campus_id.strip().upper(), full_name=full_name.strip(), department=department, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, campus_id):
        return self.get(campus_id__iexact=campus_id.strip())


# [UML: Inheritance — User is-a Django AbstractBaseUser (salted password hashing)]
class User(AbstractBaseUser):
    """A campus account. Passwords are stored only as salted hashes (NFR-19)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campus_id = models.CharField(
        "campus ID", max_length=20, unique=True,
        validators=[RegexValidator(r"^1000\d{5}$", "Use the 9-digit university ID, e.g. 100060412.")],
    )
    full_name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    # [UML: Aggregation — Department 1 ◇—— 0..* User "has" (Figure 20); users are not deleted with a department]
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="members")
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "campus_id"
    REQUIRED_FIELDS = ["full_name"]
    objects = UserManager()

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.campus_id})"

    @property
    def first_name(self):
        parts = [p for p in self.full_name.replace("Dr.", "").split() if p]
        return parts[0] if parts else self.full_name

    @property
    def initials(self):
        parts = [p for p in self.full_name.replace("Dr.", "").split() if p and p[0].isalpha()]
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper() if parts else "?"

    # Django's auth framework expects these; no implicit superuser exists.
    is_staff = False
    is_superuser = False

    def has_perm(self, perm, obj=None):  # pragma: no cover - Django admin is not mounted
        return False

    def has_module_perms(self, app_label):  # pragma: no cover
        return False

    # --- Class-diagram operation (Figure 18): User.may(action, record) ---
    def may(self, action, record=None):
        """True if the deny-by-default policy allows ``action`` on ``record``."""
        from . import policy

        checks = {
            "view_asset": lambda: policy.can_view_asset(self, record),
            "manage_asset": lambda: policy.can_manage_asset(self, record),
            "create_request": lambda: policy.can_create_request(self),
            "view_request": lambda: policy.can_view_request(self, record),
            "view_transfer": lambda: policy.can_view_transfer(self, record),
            "admin_accounts": lambda: policy.can_admin_accounts(self),
            "author_report": lambda: policy.can_author_report(self),
        }
        return bool(checks[action]()) if action in checks else False


class UserRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # [UML: Association — User 1 —— 0..* UserRole "receives" (Figure 20)]
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="role_grants")
    # [UML: Association — Role 1 —— 0..* UserRole "defines" (Figure 20)]
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="grants")
    # [UML: Association — Department 0..1 —— 0..* UserRole "scopes"; NULL = explicit campus-wide grant (Figure 20)]
    scope_department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.PROTECT, related_name="scoped_grants",
        help_text="Empty = explicitly approved campus-wide grant.",
    )
    active = models.BooleanField(default=True)
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "role", "scope_department"], name="uniq_user_role_scope"),
            models.UniqueConstraint(fields=["user", "role"], condition=Q(scope_department__isnull=True), name="uniq_user_role_campus"),
        ]

    def __str__(self):
        scope = self.scope_department.code if self.scope_department_id else "Campus-wide"
        return f"{self.role.name} · {scope}"
