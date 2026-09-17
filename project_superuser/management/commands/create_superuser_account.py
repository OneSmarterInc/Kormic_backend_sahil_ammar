from __future__ import annotations

import getpass

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    """
    Bootstraps the first Account.Role.SUPERUSER login for /api/superuser/.

    There is no public endpoint that can create a superuser account (that's
    the point -- see accounts.serializers.RegisterSerializer), so the very
    first one has to come from here. Every superuser after that can be
    created via POST /api/superuser/users/create-superuser/ instead.
    """

    help = "Create a project_superuser account (Account.Role.SUPERUSER)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=False, help="Prompted for if omitted.")
        parser.add_argument("--name", default="")

    def handle(self, *args, **options):
        from accounts.models import Account

        email = options["email"].strip()
        name = (options["name"] or "").strip()
        password = options["password"]

        if not email:
            raise CommandError("--email cannot be blank.")

        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email {email} already exists.")

        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                raise CommandError("Passwords did not match.")

        try:
            validate_password(password)
        except DjangoValidationError as exc:
            raise CommandError("\n".join(exc.messages))

        with transaction.atomic():
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
                first_name=name[:150],
            )
            Account.objects.create(user=user, role=Account.Role.SUPERUSER)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created superuser account for {email}. Log in via /api/auth/login/ and "
                "complete TOTP enrollment before using /api/superuser/."
            )
        )
