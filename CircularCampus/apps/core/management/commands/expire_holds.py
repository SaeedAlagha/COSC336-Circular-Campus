"""Release unapproved holds whose 48-hour period ended (also runs automatically on stock reads)."""
from django.core.management.base import BaseCommand

from apps.exchange.services import expire_due_holds


class Command(BaseCommand):
    help = "Expire pending-approval holds past their expiry time (allocations never expire)."

    def handle(self, *args, **opts):
        n = expire_due_holds()
        self.stdout.write(self.style.SUCCESS(f"{n} hold(s) expired."))
