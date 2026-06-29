"""Tests for AnimeSeries model properties and builder guard logic."""
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Anime,
    AnimeSeriesLink,
    AnimeSeriesRelation,
    AnimeSeries,
    Item,
    MediaTypes,
    Sources,
    Status,
)

# Minimal metadata returned when Anime.save() calls process_progress / process_status
_ANIME_META_STUB = {
    'max_progress': None,
    'episodes': [],
    'title': 'Stub',
    'image': '',
    'related': {},
}


def _make_anime_item(media_id: str, title: str = 'Anime') -> Item:
    return Item.objects.create(
        media_id=media_id,
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        title=title,
        image='http://example.com/img.jpg',
    )


def _make_series_item(title: str = 'Series') -> Item:
    return Item.objects.create(
        media_id=str(uuid.uuid4()),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME_SERIES.value,
        title=title,
        image='http://example.com/img.jpg',
    )


class AnimeSeriesProgressTest(TestCase):
    """Tests for AnimeSeries.progress property."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')

        self.series_item = _make_series_item('My Series')
        self.season1 = _make_anime_item('1', 'Season 1')
        self.season2 = _make_anime_item('2', 'Season 2')
        self.extra = _make_anime_item('3', 'OVA')

        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.season1, order=1, is_extra=False,
        )
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.season2, order=2, is_extra=False,
        )
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.extra, order=3, is_extra=True,
        )

        self.anime_series = AnimeSeries.objects.create(
            item=self.series_item, user=self.user, status=Status.IN_PROGRESS.value,
        )

        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.a1 = Anime.objects.create(
                item=self.season1, user=self.user,
                status=Status.COMPLETED.value, related_series=self.anime_series,
            )
            self.a2 = Anime.objects.create(
                item=self.season2, user=self.user,
                status=Status.IN_PROGRESS.value, related_series=self.anime_series,
            )
            self.a_extra = Anime.objects.create(
                item=self.extra, user=self.user,
                status=Status.COMPLETED.value, related_series=self.anime_series,
            )

    def test_progress_sums_episodes_of_main_seasons(self):
        """progress = sum of episodes watched in non-extra seasons."""
        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        # a1, a2 created with progress=0
        self.assertEqual(series.progress, 0)

    def test_progress_sums_episodes_with_watched(self):
        """progress reflects actual episode counts across main seasons."""
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.a1.progress = 13
            self.a1.save(update_fields=['progress'])
            self.a2.progress = 5
            self.a2.save(update_fields=['progress'])

        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        self.assertEqual(series.progress, 18)

    def test_progress_with_injected_main_ids(self):
        """When _main_anime_ids is pre-set (batch annotation), progress uses it."""
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.a1.progress = 13
            self.a1.save(update_fields=['progress'])

        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        series._main_anime_ids = {self.season1.id, self.season2.id}
        self.assertEqual(series.progress, 13)

    def test_progress_extra_not_counted(self):
        """Episodes watched in extras do not count toward progress."""
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.a_extra.progress = 6
            self.a_extra.save(update_fields=['progress'])

        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        self.assertEqual(series.progress, 0)


class AnimeSeriesLastWatchedTest(TestCase):
    """Tests for AnimeSeries.last_watched property."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.series_item = _make_series_item()
        self.s1 = _make_anime_item('10', 'Season 1')
        self.s2 = _make_anime_item('11', 'Season 2')
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1, is_extra=False,
        )
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s2, order=2, is_extra=False,
        )
        self.anime_series = AnimeSeries.objects.create(
            item=self.series_item, user=self.user, status=Status.IN_PROGRESS.value,
        )
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.a1 = Anime.objects.create(
                item=self.s1, user=self.user,
                status=Status.COMPLETED.value, related_series=self.anime_series,
            )
            self.a2 = Anime.objects.create(
                item=self.s2, user=self.user,
                status=Status.IN_PROGRESS.value, related_series=self.anime_series,
            )
        # Set progressed_at directly via update to bypass save() hooks
        Anime.objects.filter(pk=self.a1.pk).update(
            progressed_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        Anime.objects.filter(pk=self.a2.pk).update(
            progressed_at=datetime(2024, 6, 1, tzinfo=UTC),
        )

    def test_last_watched_returns_most_recent_title(self):
        """last_watched returns the title of the season with the latest progressed_at."""
        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        self.assertEqual(series.last_watched, 'Season 2')

    def test_last_watched_empty_when_no_seasons(self):
        """last_watched is empty string when no seasons are tracked."""
        empty_series_item = _make_series_item('Empty')
        anime_series = AnimeSeries.objects.create(
            item=empty_series_item, user=self.user, status=Status.PLANNING.value,
        )
        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=anime_series.pk,
        )
        self.assertEqual(series.last_watched, '')


class BuildRelatedGuardTest(TestCase):
    """Tests for build_related_for_series guard logic."""

    def setUp(self):
        self.series_item = _make_series_item('Series A')
        self.other_item = _make_series_item('Series B')

    def test_guard_skips_when_from_series_relation_exists(self):
        """build_related_for_series is a no-op when a from_series relation exists."""
        AnimeSeriesRelation.objects.create(
            from_series=self.series_item,
            to_series=self.other_item,
            relation_type='alternative_version',
        )

        with patch('app.providers.services.get_media_metadata') as mock_meta:
            from app.anime_series_builder import build_related_for_series
            build_related_for_series(self.series_item, Sources.MAL.value)
            mock_meta.assert_not_called()

    def test_guard_skips_when_to_series_relation_exists(self):
        """build_related_for_series is a no-op when a to_series relation exists."""
        AnimeSeriesRelation.objects.create(
            from_series=self.other_item,
            to_series=self.series_item,
            relation_type='adaptation',
        )

        with patch('app.providers.services.get_media_metadata') as mock_meta:
            from app.anime_series_builder import build_related_for_series
            build_related_for_series(self.series_item, Sources.MAL.value)
            mock_meta.assert_not_called()

    def test_no_members_means_no_api_calls(self):
        """Series with no AnimeSeriesLink members makes no metadata requests."""
        with patch('app.providers.services.get_media_metadata') as mock_meta:
            from app.anime_series_builder import build_related_for_series
            build_related_for_series(self.series_item, Sources.MAL.value)
            mock_meta.assert_not_called()

    def test_creates_bidirectional_relations(self):
        """Relations are created in both directions for a found alternative link."""
        anime_item = _make_anime_item('42', 'Root Anime')
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=anime_item, order=1, is_extra=False,
        )

        related_anime_item = _make_anime_item('99', 'Related Anime')
        # Pre-create a series for the related anime (so no BFS needed)
        AnimeSeriesLink.objects.create(
            series_item=self.other_item, anime_item=related_anime_item, order=1, is_extra=False,
        )

        alt_meta = {
            'title': 'Root Anime', 'image': '',
            'related': {
                'related_anime': [
                    {'media_id': '99', 'relation_type': 'alternative_version', 'title': 'Related Anime'},
                ],
            },
        }

        with patch('app.providers.services.get_media_metadata', return_value=alt_meta):
            from app.anime_series_builder import build_related_for_series
            build_related_for_series(self.series_item, Sources.MAL.value)

        self.assertTrue(
            AnimeSeriesRelation.objects.filter(
                from_series=self.series_item, to_series=self.other_item,
            ).exists(),
        )
        self.assertTrue(
            AnimeSeriesRelation.objects.filter(
                from_series=self.other_item, to_series=self.series_item,
            ).exists(),
        )
