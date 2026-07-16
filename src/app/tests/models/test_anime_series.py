"""Tests for AnimeSeries model properties and builder guard logic."""
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import (
    Anime,
    AnimeEpisode,
    AnimeSeriesLink,
    AnimeSeriesRelation,
    AnimeSeries,
    Item,
    MediaManager,
    MediaTypes,
    Sources,
    Status,
)
from events.models import Event
from users.models import HomeSortChoices

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

    def test_current_season_returns_most_recently_progressed(self):
        """current_season prefers the season with the latest progressed_at."""
        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        self.assertEqual(series.current_season.pk, self.a2.pk)

    def test_current_season_none_when_no_seasons(self):
        """current_season is None when the series has no seasons at all."""
        empty_series_item = _make_series_item('Empty')
        anime_series = AnimeSeries.objects.create(
            item=empty_series_item, user=self.user, status=Status.PLANNING.value,
        )
        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=anime_series.pk,
        )
        self.assertIsNone(series.current_season)


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


class AnnotateAnimeSeriesProgressTest(TestCase):
    """Tests for MediaManager._annotate_animeseries_progress priority chain."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.series_item = _make_series_item('Series')
        self.s1 = _make_anime_item('101', 'Season 1')
        self.s2 = _make_anime_item('102', 'Season 2')
        self.s3 = _make_anime_item('103', 'Season 3')

        self.anime_series = AnimeSeries.objects.create(
            item=self.series_item, user=self.user, status=Status.IN_PROGRESS.value,
        )

    def _annotate(self):
        series_list = list(
            AnimeSeries.objects.filter(pk=self.anime_series.pk).select_related('item'),
        )
        MediaManager()._annotate_animeseries_progress(series_list)
        return series_list[0]

    def test_uses_total_episodes_from_link(self):
        """Priority 1: total_episodes stored in AnimeSeriesLink is used directly."""
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1,
            is_extra=False, total_episodes=25,
        )
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s2, order=2,
            is_extra=False, total_episodes=12,
        )
        series = self._annotate()
        self.assertEqual(series.max_progress, 37)

    def test_falls_back_to_events_when_no_link_total(self):
        """Priority 2: calendar events used when total_episodes is null."""
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1,
            is_extra=False, total_episodes=None,
        )
        Event.objects.create(
            item=self.s1,
            content_number=24,
            datetime=timezone.now() - timezone.timedelta(days=1),
            notification_sent=True,
        )
        # A later event — max should win
        Event.objects.create(
            item=self.s1,
            content_number=25,
            datetime=timezone.now() - timezone.timedelta(days=1),
            notification_sent=True,
        )
        series = self._annotate()
        self.assertEqual(series.max_progress, 25)

    def test_falls_back_to_anime_episodes_when_no_events(self):
        """Priority 3: AnimeEpisode count used when neither link total nor events exist."""
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1,
            is_extra=False, total_episodes=None,
        )
        for i in range(13):
            AnimeEpisode.objects.create(
                anime_item=self.s1, episode_number=i + 1, is_special=False,
            )
        # Specials must not count
        AnimeEpisode.objects.create(anime_item=self.s1, episode_number=0, is_special=True)
        series = self._annotate()
        self.assertEqual(series.max_progress, 13)

    def test_link_total_takes_priority_over_events(self):
        """total_episodes from link overrides calendar events."""
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1,
            is_extra=False, total_episodes=25,
        )
        Event.objects.create(
            item=self.s1,
            content_number=99,
            datetime=timezone.now() - timezone.timedelta(days=1),
            notification_sent=True,
        )
        series = self._annotate()
        self.assertEqual(series.max_progress, 25)

    def test_extra_links_excluded(self):
        """Extra AnimeSeriesLink members are not counted in max_progress."""
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s1, order=1,
            is_extra=False, total_episodes=24,
        )
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.s2, order=2,
            is_extra=True, total_episodes=4,
        )
        series = self._annotate()
        self.assertEqual(series.max_progress, 24)


class AnimeSeriesProgressDedupTest(TestCase):
    """Tests for AnimeSeries.progress deduplication by item_id."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.series_item = _make_series_item('Series')
        self.anime_item = _make_anime_item('200', 'Season 1')
        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.anime_item, order=1, is_extra=False,
        )
        self.anime_series = AnimeSeries.objects.create(
            item=self.series_item, user=self.user, status=Status.IN_PROGRESS.value,
        )

    def test_deduplicates_duplicate_anime_takes_max_progress(self):
        """When two Anime rows share the same item_id, progress uses the higher value."""
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            a1 = Anime.objects.create(
                item=self.anime_item, user=self.user,
                status=Status.IN_PROGRESS.value, related_series=self.anime_series,
            )
            # Bypass unique constraint by creating a second with a temp item then re-pointing
            dup_item = _make_anime_item('tmp-200', 'Season 1 dup')
            a2 = Anime.objects.create(
                item=dup_item, user=self.user,
                status=Status.IN_PROGRESS.value, related_series=self.anime_series,
            )

        # Simulate the duplicate-item scenario by forcing item_id equality via update
        Anime.objects.filter(pk=a1.pk).update(progress=5)
        Anime.objects.filter(pk=a2.pk).update(progress=10, item=self.anime_item)

        series = AnimeSeries.objects.prefetch_related('anime_seasons__item').get(
            pk=self.anime_series.pk,
        )
        # Only one item_id in main_ids → max(5, 10) = 10
        self.assertEqual(series.progress, 10)


class LazyBuildFromSearchTest(TestCase):
    """Tests for anime_series_builder.lazy_build_from_search_results."""

    def test_creates_series_item_and_links_for_single_member(self):
        """Single-member group gets a series Item with one AnimeSeriesLink."""
        from app.anime_series_builder import lazy_build_from_search_results

        members = [{'media_id': '1001', 'title': 'My Anime', 'image': '', 'start_date': '2020-04-01'}]
        series_item = lazy_build_from_search_results(members, Sources.MAL.value)

        self.assertEqual(series_item.media_type, MediaTypes.ANIME_SERIES.value)
        self.assertEqual(AnimeSeriesLink.objects.filter(series_item=series_item).count(), 1)
        link = AnimeSeriesLink.objects.get(series_item=series_item)
        self.assertFalse(link.is_extra)
        self.assertEqual(link.anime_item.media_id, '1001')

    def test_members_sorted_by_start_date(self):
        """Members are linked in ascending start_date order."""
        from app.anime_series_builder import lazy_build_from_search_results

        members = [
            {'media_id': '2002', 'title': 'Season 2', 'image': '', 'start_date': '2022-01-01'},
            {'media_id': '2001', 'title': 'Season 1', 'image': '', 'start_date': '2020-01-01'},
        ]
        series_item = lazy_build_from_search_results(members, Sources.MAL.value)

        links = list(AnimeSeriesLink.objects.filter(series_item=series_item).order_by('order'))
        self.assertEqual(links[0].anime_item.media_id, '2001')
        self.assertEqual(links[1].anime_item.media_id, '2002')

    def test_idempotent_second_call_does_not_duplicate(self):
        """Calling twice with the same members returns the same series, no duplicate links."""
        from app.anime_series_builder import lazy_build_from_search_results

        members = [
            {'media_id': '3001', 'title': 'Anime A', 'image': '', 'start_date': '2021-01-01'},
            {'media_id': '3002', 'title': 'Anime B', 'image': '', 'start_date': '2022-01-01'},
        ]
        item1 = lazy_build_from_search_results(members, Sources.MAL.value)
        item2 = lazy_build_from_search_results(members, Sources.MAL.value)

        self.assertEqual(item1.pk, item2.pk)
        self.assertEqual(AnimeSeriesLink.objects.filter(series_item=item1).count(), 2)

    def test_extends_existing_series_when_member_already_linked(self):
        """If one member is already in a series, newcomers are added to that series."""
        from app.anime_series_builder import lazy_build_from_search_results

        existing_anime = Item.objects.create(
            media_id='4001', source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value, title='Existing', image='',
        )
        existing_series = Item.objects.create(
            media_id=str(uuid.uuid4()), source=Sources.MAL.value,
            media_type=MediaTypes.ANIME_SERIES.value, title='Existing Series', image='',
        )
        AnimeSeriesLink.objects.create(
            series_item=existing_series, anime_item=existing_anime, order=1, is_extra=False,
        )

        members = [
            {'media_id': '4001', 'title': 'Existing', 'image': '', 'start_date': '2020-01-01'},
            {'media_id': '4002', 'title': 'Sequel', 'image': '', 'start_date': '2022-01-01'},
        ]
        series_item = lazy_build_from_search_results(members, Sources.MAL.value)

        self.assertEqual(series_item.pk, existing_series.pk)
        self.assertEqual(AnimeSeriesLink.objects.filter(series_item=series_item).count(), 2)


class AnimeSeriesHomeBucketingTest(TestCase):
    """Tests for how get_home_status buckets anime grouped into a series."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='home_bucket', password='pw')

        self.series_item = _make_series_item('My Series')
        self.season1 = _make_anime_item('h1', 'Season 1')
        self.standalone_item = _make_anime_item('h2', 'Standalone Anime')

        AnimeSeriesLink.objects.create(
            series_item=self.series_item, anime_item=self.season1, order=1, is_extra=False,
        )

        self.anime_series = AnimeSeries.objects.create(
            item=self.series_item, user=self.user, status=Status.IN_PROGRESS.value,
        )

        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.season1_anime = Anime.objects.create(
                item=self.season1, user=self.user,
                status=Status.IN_PROGRESS.value, related_series=self.anime_series,
            )
            self.standalone = Anime.objects.create(
                item=self.standalone_item, user=self.user,
                status=Status.IN_PROGRESS.value,
            )

    def test_grouped_anime_excluded_from_flat_anime_list(self):
        """Anime linked to a series isn't also listed under the plain Anime bucket."""
        manager = MediaManager()
        home_status = manager.get_home_status(
            user=self.user, status=Status.IN_PROGRESS.value,
            sort_by=HomeSortChoices.UPCOMING, items_limit=14,
        )
        anime_items = home_status[MediaTypes.ANIME.value]['items']
        self.assertEqual([m.item_id for m in anime_items], [self.standalone.item_id])

    def test_series_bucketed_by_its_own_status_shows_current_season(self):
        """Bucketed by the series' status, showing its current season's own card."""
        manager = MediaManager()
        home_status = manager.get_home_status(
            user=self.user, status=Status.IN_PROGRESS.value,
            sort_by=HomeSortChoices.UPCOMING, items_limit=14,
        )
        series_section_items = home_status[MediaTypes.ANIME_SERIES.value]['items']
        self.assertEqual(
            [m.item_id for m in series_section_items],
            [self.season1_anime.item_id],
        )
