"""Backfill AnimeSeriesLink.total_episodes from MAL metadata."""
import logging

from django.core.management.base import BaseCommand

from app.models import AnimeSeriesLink, MediaTypes
from app import providers

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Populate total_episodes for existing AnimeSeriesLink rows."""

    help = 'Backfill AnimeSeriesLink.total_episodes from MAL metadata'

    def handle(self, *args, **options):
        """Run the backfill."""
        qs = AnimeSeriesLink.objects.filter(
            total_episodes__isnull=True,
        ).select_related('anime_item')

        total = qs.count()
        self.stdout.write(f'Backfilling {total} links...')

        updated = 0
        for link in qs.iterator():
            item = link.anime_item
            try:
                meta = providers.services.get_media_metadata(
                    MediaTypes.ANIME.value,
                    str(item.media_id),
                    item.source,
                )
            except Exception:
                logger.warning('Could not fetch metadata for anime %s', item.media_id)
                continue

            ep_count = meta.get('max_progress')
            if ep_count:
                AnimeSeriesLink.objects.filter(pk=link.pk).update(total_episodes=ep_count)
                updated += 1

        self.stdout.write(self.style.SUCCESS(f'Updated {updated}/{total} links'))
