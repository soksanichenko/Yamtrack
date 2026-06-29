"""Tests for anime episode tracking (SimklMapping, AnimeEpisode, WatchedAnimeEpisode)."""
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Anime,
    AnimeEpisode,
    Item,
    MediaTypes,
    SimklMapping,
    Sources,
    Status,
    WatchedAnimeEpisode,
)
from app.providers.services import fetch_and_store_anime_episodes

_ANIME_META_STUB = {
    'max_progress': None,
    'episodes': [],
    'title': 'Stub',
    'image': '',
    'related': {},
}

_SIMKL_EPISODES = [
    {'episode': 1, 'title': 'Episode 1', 'description': 'Desc 1', 'date': '2013-04-06 00:00', 'img': 'abc_w', 'type': 'episode'},
    {'episode': 2, 'title': 'Episode 2', 'description': 'Desc 2', 'date': '2013-04-13 00:00', 'img': None, 'type': 'episode'},
    {'episode': 3, 'title': 'Episode 3', 'description': '', 'date': None, 'img': None, 'type': 'episode'},
    {'episode': 4, 'title': 'Special', 'description': '', 'date': None, 'img': None, 'type': 'special'},
]


def _make_anime_item(media_id: str = None, title: str = 'Anime') -> Item:
    return Item.objects.create(
        media_id=media_id or str(uuid.uuid4()),
        source=Sources.MAL.value,
        media_type=MediaTypes.ANIME.value,
        title=title,
        image='',
    )


class FetchAndStoreEpisodesTest(TestCase):
    """Tests for fetch_and_store_anime_episodes service function."""

    def setUp(self):
        self.item = _make_anime_item('12345', 'Attack on Titan')

    def test_creates_episodes_from_simkl(self):
        """Episodes are created from Simkl API response."""
        with patch('app.providers.simkl.get_simkl_id', return_value=999), \
             patch('app.providers.simkl.get_episodes', return_value=_SIMKL_EPISODES):
            fetch_and_store_anime_episodes(self.item)

        eps = list(AnimeEpisode.objects.filter(anime_item=self.item).order_by('episode_number'))
        self.assertEqual(len(eps), 4)
        self.assertEqual(eps[0].episode_number, 1)
        self.assertEqual(eps[0].title, 'Episode 1')
        self.assertIsNotNone(eps[0].img)
        self.assertIn('abc_w', eps[0].img)
        self.assertTrue(eps[3].is_special)
        self.assertFalse(eps[0].is_special)

    def test_creates_simkl_mapping(self):
        """SimklMapping is created on first fetch."""
        with patch('app.providers.simkl.get_simkl_id', return_value=999), \
             patch('app.providers.simkl.get_episodes', return_value=[]):
            fetch_and_store_anime_episodes(self.item)

        self.assertTrue(SimklMapping.objects.filter(item=self.item, simkl_id=999).exists())

    def test_no_op_when_episodes_exist(self):
        """Second call does not re-fetch from Simkl."""
        AnimeEpisode.objects.create(
            anime_item=self.item, episode_number=1, title='Existing',
        )
        with patch('app.providers.simkl.get_simkl_id') as mock_id:
            fetch_and_store_anime_episodes(self.item)
            mock_id.assert_not_called()

    def test_no_op_when_simkl_id_not_found(self):
        """No AnimeEpisode created when Simkl has no mapping."""
        with patch('app.providers.simkl.get_simkl_id', return_value=None):
            fetch_and_store_anime_episodes(self.item)

        self.assertFalse(AnimeEpisode.objects.filter(anime_item=self.item).exists())

    def test_parses_aired_datetime(self):
        """aired field is parsed from Simkl date string."""
        with patch('app.providers.simkl.get_simkl_id', return_value=999), \
             patch('app.providers.simkl.get_episodes', return_value=_SIMKL_EPISODES):
            fetch_and_store_anime_episodes(self.item)

        ep = AnimeEpisode.objects.get(anime_item=self.item, episode_number=1)
        self.assertIsNotNone(ep.aired)
        self.assertEqual(ep.aired.year, 2013)
        self.assertIsNone(
            AnimeEpisode.objects.get(anime_item=self.item, episode_number=3).aired,
        )


class SyncProgressTest(TestCase):
    """Tests for Anime._sync_progress()."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.item = _make_anime_item('99', 'Test Anime')
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.anime = Anime.objects.create(
                item=self.item, user=self.user, status=Status.IN_PROGRESS.value,
            )
        for n in range(1, 5):
            AnimeEpisode.objects.create(anime_item=self.item, episode_number=n, title=f'Ep {n}')

    def _ep(self, n):
        return AnimeEpisode.objects.get(anime_item=self.item, episode_number=n)

    def test_progress_reflects_watched_count(self):
        """progress equals number of WatchedAnimeEpisode records."""
        WatchedAnimeEpisode.objects.create(user=self.user, anime=self.anime, episode=self._ep(1))
        WatchedAnimeEpisode.objects.create(user=self.user, anime=self.anime, episode=self._ep(2))
        self.anime._sync_progress()
        self.anime.refresh_from_db()
        self.assertEqual(self.anime.progress, 2)

    def test_auto_complete_when_all_watched(self):
        """Anime status becomes Completed when all non-special episodes watched."""
        for n in range(1, 5):
            WatchedAnimeEpisode.objects.create(user=self.user, anime=self.anime, episode=self._ep(n))
        self.anime._sync_progress()
        self.anime.refresh_from_db()
        self.assertEqual(self.anime.status, Status.COMPLETED.value)

    def test_no_auto_complete_when_episodes_unknown(self):
        """Auto-complete does not trigger when AnimeEpisode count is 0."""
        AnimeEpisode.objects.filter(anime_item=self.item).delete()
        self.anime._sync_progress()
        self.anime.refresh_from_db()
        self.assertNotEqual(self.anime.status, Status.COMPLETED.value)


class MigrateLegacyProgressTest(TestCase):
    """Tests for Anime._migrate_legacy_progress()."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.item = _make_anime_item('77', 'Old Anime')
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.anime = Anime.objects.create(
                item=self.item, user=self.user, status=Status.IN_PROGRESS.value,
            )
        Anime.objects.filter(pk=self.anime.pk).update(progress=2)
        self.anime.refresh_from_db()
        for n in range(1, 5):
            AnimeEpisode.objects.create(anime_item=self.item, episode_number=n, title=f'Ep {n}')

    def test_creates_watched_records_up_to_progress(self):
        """WatchedAnimeEpisode records are created for episodes 1..progress."""
        self.anime._migrate_legacy_progress()
        watched = WatchedAnimeEpisode.objects.filter(anime=self.anime)
        self.assertEqual(watched.count(), 2)
        ep_nums = set(w.episode.episode_number for w in watched)
        self.assertEqual(ep_nums, {1, 2})

    def test_no_op_when_already_migrated(self):
        """Migration is a no-op when WatchedAnimeEpisode records already exist."""
        ep1 = AnimeEpisode.objects.get(anime_item=self.item, episode_number=1)
        WatchedAnimeEpisode.objects.create(user=self.user, anime=self.anime, episode=ep1)
        self.anime._migrate_legacy_progress()
        # Should still be 1 (migration skipped)
        self.assertEqual(WatchedAnimeEpisode.objects.filter(anime=self.anime).count(), 1)

    def test_no_op_when_progress_zero(self):
        """Migration is a no-op when progress is 0."""
        Anime.objects.filter(pk=self.anime.pk).update(progress=0)
        self.anime.refresh_from_db()
        self.anime._migrate_legacy_progress()
        self.assertFalse(WatchedAnimeEpisode.objects.filter(anime=self.anime).exists())


class AnimeEpisodeToggleViewTest(TestCase):
    """Tests for anime_episode_toggle view."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username='test', password='pw')
        self.client.force_login(self.user)
        self.item = _make_anime_item('55', 'Test Anime')
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self.anime = Anime.objects.create(
                item=self.item, user=self.user, status=Status.IN_PROGRESS.value,
            )
        self.episode = AnimeEpisode.objects.create(
            anime_item=self.item, episode_number=1, title='Ep 1',
        )

    def _toggle(self):
        return self.client.post('/anime_episode_toggle', {
            'item_pk': self.item.pk,
            'episode_pk': self.episode.pk,
            'next': '/',
        })

    def test_marks_episode_watched(self):
        """POST creates a WatchedAnimeEpisode record."""
        self._toggle()
        self.assertTrue(
            WatchedAnimeEpisode.objects.filter(anime=self.anime, episode=self.episode).exists(),
        )

    def test_unmarks_episode_on_second_toggle(self):
        """Second POST removes the WatchedAnimeEpisode record."""
        WatchedAnimeEpisode.objects.create(user=self.user, anime=self.anime, episode=self.episode)
        self._toggle()
        self.assertFalse(
            WatchedAnimeEpisode.objects.filter(anime=self.anime, episode=self.episode).exists(),
        )

    def test_auto_creates_anime_when_not_tracked(self):
        """Toggling an episode on an untracked anime creates an Anime record."""
        self.anime.delete()
        with patch('app.models.providers.services.get_media_metadata', return_value=_ANIME_META_STUB):
            self._toggle()
        self.assertTrue(
            Anime.objects.filter(item=self.item, user=self.user).exists(),
        )
        self.assertTrue(
            WatchedAnimeEpisode.objects.filter(episode=self.episode).exists(),
        )

    def test_unauthenticated_redirects_to_login(self):
        """Unauthenticated POST is redirected to login by authentication middleware."""
        self.client.logout()
        resp = self._toggle()
        self.assertEqual(resp.status_code, 302)
