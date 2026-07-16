"""Recompute AnimeSeries.status from its seasons' current statuses."""
from django.core.management.base import BaseCommand

from app.anime_series_builder import recompute_series_statuses


class Command(BaseCommand):
    """Recompute AnimeSeries.status for all existing series.

    AnimeSeries.status is normally kept in sync whenever a season's own
    status changes (see Anime._sync_series_status), but series seeded before
    that logic existed — or before it correctly handled all cases — can be
    left with a stale status. This recomputes every series from scratch.
    """

    help = "Recompute AnimeSeries.status from its seasons current statuses"

    def handle(self, *args, **options):
        """Run the recompute."""
        updated, total = recompute_series_statuses()
        self.stdout.write(self.style.SUCCESS(f"Updated {updated}/{total} series"))
