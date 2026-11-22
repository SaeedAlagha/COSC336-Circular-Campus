"""Start a REAL (empty) installation: reference data + the first System Administrator.

    python manage.py init_system
Creates the 10 roles, the standard category tree and one administrator account.
Refuses to run if any account already exists. No demo people or assets are created.
After this, the administrator signs in and creates departments, accounts and role grants.
"""
from getpass import getpass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Department, Role, User, UserRole
from apps.catalog.models import Category
from apps.core.management.commands.seed_demo import CATEGORIES


class Command(BaseCommand):
    help = "Initialise an empty installation with roles, categories and the first administrator."

    def add_arguments(self, parser):
        parser.add_argument("--campus-id")
        parser.add_argument("--name")
        parser.add_argument("--department-code")
        parser.add_argument("--department-name")
        parser.add_argument("--password", help="Avoid on shared machines; prompted securely if omitted.")

    def ask(self, opts, key, prompt):
        return (opts.get(key) or input(prompt)).strip()

    def handle(self, *args, **opts):
        if User.objects.exists():
            raise CommandError("Accounts already exist — this installation is already initialised. Nothing was changed.")
        self.stdout.write("Initialise Circular Campus (real data, no demo records).")
        campus_id = self.ask(opts, "campus_id", "Administrator campus ID (9 digits, e.g. 100060001): ")
        name = self.ask(opts, "name", "Administrator full name: ")
        dcode = self.ask(opts, "department_code", "Administrator's department code (e.g. IT): ").upper()
        dname = self.ask(opts, "department_name", "Department name (e.g. Information Technology): ")
        password = opts.get("password")
        if not password:
            password = getpass("Password (min 12 characters): ")
            if getpass("Confirm password: ") != password:
                raise CommandError("Passwords do not match.")
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        from apps.core import validators as V
        from apps.core.errors import BusinessError

        try:
            campus_id = V.campus_id(campus_id)
            name = V.person_name(name)
            dcode = V.text(dcode, "code", label="Department code", required=True, max_len=12).upper()
            if not V.CODE.match(dcode):
                raise BusinessError(BusinessError.VALIDATION_ERROR, "Department code: 2–12 capital letters, e.g. IT.")
            dname = V.text(dname, "name", label="Department name", required=True, min_len=3, max_len=120)
            validate_password(password)
        except BusinessError as exc:
            raise CommandError(exc.message)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
        with transaction.atomic():
            for code, label in Role.CHOICES:
                Role.objects.get_or_create(code=code, defaults={"name": label})
            if not Category.objects.exists():
                for (root, kind), children in CATEGORIES.items():
                    parent = Category.objects.create(name=root, kind=kind)
                    for cname, kw in children:
                        Category.objects.create(name=cname, parent=parent, keywords=kw)
            dept = Department.objects.create(code=dcode, name=dname)
            user = User(campus_id=campus_id, full_name=name, department=dept)
            user.full_clean(exclude=["password"])
            user.set_password(password)
            user.save()
            UserRole.objects.create(user=user, role=Role.objects.get(code=Role.SYSADMIN), scope_department=None)
        self.stdout.write(self.style.SUCCESS(
            f"Done. Sign in as {campus_id}. Next: Departments → add departments; Accounts → create staff and grant roles "
            "(custodians, approvers, requesters…). The administrator has no approval authority by design."))
