"""Oral-demo helper: print the database rows behind one request (request → hold → proposal/approvals → transfer → assets).

    python manage.py trace_request R-0004
Run it before and after each workflow step to show exactly which rows changed.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.exchange.models import ResourceRequest
from apps.workflow.models import Transfer


class Command(BaseCommand):
    help = "Show the database records for a request reference (e.g. R-0004)."

    def add_arguments(self, parser):
        parser.add_argument("reference")

    def line(self, table, **cols):
        self.stdout.write(f"  {table:<28} " + " | ".join(f"{k}={v}" for k, v in cols.items()))

    def handle(self, *args, **opts):
        try:
            r = ResourceRequest.objects.select_related("department", "requester").get(reference=opts["reference"].upper())
        except ResourceRequest.DoesNotExist:
            raise CommandError("No such request reference.")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Request {r.reference}"))
        self.line("exchange_resourcerequest", id=str(r.pk)[:8], status=r.status, version=r.version, quantity=r.quantity, received=r.received_quantity, department=r.department.code)
        for h in r.reservations.select_related("asset"):
            self.line("exchange_reservation", ref=h.reference, asset=h.asset.asset_tag, qty=h.quantity, remaining=h.remaining_quantity, status=h.status, expires=h.expires_at)
        for v in r.proposal_versions.order_by("version_no"):
            self.line("workflow_proposalversion", id=str(v.pk)[:8], kind=v.kind, v=v.version_no, status=v.status, hash=v.snapshot_hash[:10])
            for a in v.approvals.order_by("stage_order"):
                self.line("  workflow_approval", stage=a.stage, decision=a.decision, reviewer=a.reviewer.campus_id if a.reviewer else "-", decided=a.decided_at)
        for t in Transfer.objects.filter(reservation__request=r).select_related("source_asset__owner_department", "received_asset__owner_department", "releasing_user", "receiving_user"):
            self.line("workflow_transfer", ref=t.reference, status=t.status, qty=t.quantity, released=t.released_at, received=t.received_at, received_qty=t.received_quantity)
            s = t.source_asset
            self.line("  catalog_asset (source)", tag=s.asset_tag, qty=s.quantity, status=s.status, dept=s.owner_department.code, custodian=s.custodian.campus_id, room=f"{s.building}/{s.room}")
            if t.received_asset:
                d = t.received_asset
                self.line("  catalog_asset (received)", tag=d.asset_tag, qty=d.quantity, status=d.status, dept=d.owner_department.code, custodian=d.custodian.campus_id, room=f"{d.building}/{d.room}")
