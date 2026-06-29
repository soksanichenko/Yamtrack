"""Simkl API provider for fetching anime episode metadata."""
import logging

from django.conf import settings
from django.core.cache import cache

from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.simkl.com'
_HEADERS = {'simkl-api-key': settings.SIMKL_ID}

CACHE_TIMEOUT = 60 * 60 * 24 * 7  # 7 days — episode lists rarely change


def get_simkl_id(mal_id: str) -> int | None:
    """Return the Simkl ID for a given MAL anime ID, or None if not found."""
    cache_key = f'simkl_id_mal_{mal_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        results = services.api_request(
            'simkl',
            'GET',
            f'{BASE_URL}/search/id',
            params={'mal': mal_id},
            headers=_HEADERS,
        )
    except Exception:
        logger.warning('Simkl: could not resolve MAL id=%s', mal_id)
        return None

    if not results:
        return None

    simkl_id = results[0].get('ids', {}).get('simkl')
    if simkl_id:
        cache.set(cache_key, simkl_id, CACHE_TIMEOUT)
    return simkl_id


def get_episodes(simkl_id: int) -> list[dict]:
    """Return episode list for a Simkl anime ID.

    Each dict contains: episode (number), title, date, img, type.
    Returns an empty list on failure.
    """
    cache_key = f'simkl_episodes_{simkl_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        data = services.api_request(
            'simkl',
            'GET',
            f'{BASE_URL}/anime/episodes/{simkl_id}',
            headers=_HEADERS,
        )
    except Exception:
        logger.warning('Simkl: could not fetch episodes for simkl_id=%s', simkl_id)
        return []

    episodes = data if isinstance(data, list) else []
    cache.set(cache_key, episodes, CACHE_TIMEOUT)
    return episodes
