import logging
import time

import requests
from defusedxml import ElementTree
from django.conf import settings
from pyrate_limiter import RedisBucket
from redis import Redis
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter, LimiterSession

from django.db import models

from django.utils import timezone

from app.helpers import format_search_response
from app.models import (
    AnimeEpisode,
    AnimeSeriesLink,
    AnimeSeriesRelation,
    Item,
    MediaTypes,
    SimklMapping,
    Sources,
)
from app.providers import (
    bgg,
    comicvine,
    hardcover,
    igdb,
    mal,
    mangaupdates,
    manual,
    openlibrary,
    tmdb,
)

logger = logging.getLogger(__name__)


def get_redis_client():
    """Return a Redis client."""
    if settings.TESTING:
        import fakeredis  # noqa: PLC0415

        return fakeredis.FakeRedis()
    return Redis.from_url(settings.REDIS_URL)


redis_db = get_redis_client()
bucket_key = f"{settings.REDIS_PREFIX}_api" if settings.REDIS_PREFIX else "api"

session = LimiterSession(
    per_second=5,
    bucket_class=RedisBucket,
    bucket_kwargs={"redis": redis_db, "bucket_key": bucket_key},
)

session.mount("http://", HTTPAdapter(max_retries=3))
session.mount("https://", HTTPAdapter(max_retries=3))

session.mount(
    "https://api.myanimelist.net/v2",
    LimiterAdapter(per_minute=30),
)
session.mount(
    "https://graphql.anilist.co",
    LimiterAdapter(per_minute=85),
)
session.mount(
    "https://api.igdb.com/v4",
    LimiterAdapter(per_second=3),
)
session.mount(
    "https://api.tvmaze.com",
    LimiterAdapter(per_second=2),
)
session.mount(
    "https://comicvine.gamespot.com/api",
    LimiterAdapter(per_hour=190),
)
session.mount(
    "https://openlibrary.org",
    LimiterAdapter(per_minute=20),
)
session.mount(
    "https://api.hardcover.app/v1/graphql",
    LimiterAdapter(per_minute=50),
)
session.mount(
    "https://boardgamegeek.com/xmlapi2",
    LimiterAdapter(per_second=2),
)
session.mount(
    "https://api.simkl.com",
    LimiterAdapter(per_second=1),
)


class ProviderAPIError(Exception):
    """Exception raised when a provider API fails to respond."""

    def __init__(self, provider, error, details=None):
        """Initialize the exception with the provider name."""
        self.provider = provider
        response = getattr(error, "response", None)
        self.status_code = getattr(response, "status_code", None)
        try:
            provider_label = Sources(provider).label
        except ValueError:
            provider_label = provider.title()

        error_text = getattr(response, "text", str(error))
        logger.error("%s error: %s", provider_label, error_text)

        message = f"There was an error contacting the {provider_label} API"
        if self.status_code is None:
            message += " (network error)"
        else:
            message += f" (HTTP {self.status_code})"
        if details:
            message += f": {details}"
        message += ". Check the logs for more details."
        super().__init__(message)


def raise_not_found_error(provider, media_id, media_type="item"):
    """
    Raise a 404 ProviderAPIError for when a media item is not found.

    Args:
        provider: The provider source value (e.g., Sources.COMICVINE.value)
        media_id: The media ID that was not found
        media_type: The type of media (e.g., "comic", "game", "book")
    """
    error_msg = f"{media_type.capitalize()} with ID {media_id} not found"
    logger.error("%s: %s", provider, error_msg)

    # Create a mock 404 error response
    mock_response = type(
        "obj",
        (object,),
        {
            "status_code": 404,
            "text": error_msg,
        },
    )()
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    raise ProviderAPIError(provider, mock_error, error_msg)


def api_request(
    provider,
    method,
    url,
    params=None,
    data=None,
    headers=None,
    response_format="json",
):
    """Make a request to the API and return the response.

    Args:
        provider: Provider identifier for error messages
        method: HTTP method ("GET" or "POST")
        url: Request URL
        params: Query params for GET, JSON body for POST
        data: Raw data for POST
        headers: Request headers
        response_format: "json" (default) or "xml" for XML parsing

    Returns:
        Parsed JSON dict or ElementTree for XML
    """
    try:
        request_kwargs = {
            "url": url,
            "headers": headers,
            "timeout": settings.REQUEST_TIMEOUT,
        }

        if method == "GET":
            request_kwargs["params"] = params
            request_func = session.get
        elif method == "POST":
            request_kwargs["data"] = data
            request_kwargs["json"] = params
            request_func = session.post

        response = request_func(**request_kwargs)
        response.raise_for_status()

        if response_format == "xml":
            return ElementTree.fromstring(response.text)
        return response.json()

    except requests.exceptions.HTTPError as error:
        error_resp = error.response
        status_code = error_resp.status_code

        # handle rate limiting
        if status_code == requests.codes.too_many_requests:
            seconds_to_wait = int(error_resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, waiting %s seconds", seconds_to_wait)
            time.sleep(seconds_to_wait + 3)
            logger.info("Retrying request")
            return api_request(
                provider,
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                response_format=response_format,
            )

        raise error from None


def fetch_and_store_anime_episodes(item: Item) -> None:
    """Lazy-fetch episode list from Simkl and persist to AnimeEpisode.

    No-op if AnimeEpisode records already exist for this item.
    Populates SimklMapping on first call.
    """
    if AnimeEpisode.objects.filter(anime_item=item).exists():
        return

    from app.providers import simkl  # noqa: PLC0415

    simkl_id = simkl.get_simkl_id(str(item.media_id))
    if not simkl_id:
        logger.info('No Simkl mapping found for anime %s', item.media_id)
        return

    SimklMapping.objects.update_or_create(item=item, defaults={'simkl_id': simkl_id})

    episodes_raw = simkl.get_episodes(simkl_id)
    to_create = []
    for ep in episodes_raw:
        ep_num = ep.get('episode')
        if not ep_num:
            continue
        img_key = ep.get('img')
        img_url = f'https://simkl.net/episodes/{img_key}_w.jpg' if img_key else None
        aired = None
        date_str = ep.get('date')
        if date_str:
            try:
                from django.utils.dateparse import parse_datetime  # noqa: PLC0415
                aired = parse_datetime(date_str)
                if aired is None:
                    # Fallback: plain "YYYY-MM-DD HH:MM" without timezone
                    from datetime import datetime  # noqa: PLC0415
                    from datetime import timezone as dt_tz  # noqa: PLC0415
                    aired = datetime.strptime(date_str[:16], '%Y-%m-%d %H:%M').replace(
                        tzinfo=dt_tz.utc,
                    )
            except (ValueError, TypeError):
                pass
        to_create.append(AnimeEpisode(
            anime_item=item,
            episode_number=ep_num,
            title=ep.get('title') or '',
            description=ep.get('description') or '',
            aired=aired,
            img=img_url,
            is_special=ep.get('type') == 'special',
        ))

    if to_create:
        AnimeEpisode.objects.bulk_create(to_create, ignore_conflicts=True)


def _anime_series_metadata(media_id: str, source: str) -> dict:
    """Build metadata for AnimeSeries from DB, including linked seasons and extras."""
    try:
        item = Item.objects.get(
            media_id=media_id,
            media_type=MediaTypes.ANIME_SERIES.value,
            source=source,
        )
    except Item.DoesNotExist:
        raise ProviderAPIError(source, None, f'AnimeSeries {media_id} not found')

    links = (
        AnimeSeriesLink.objects.filter(series_item=item)
        .select_related('anime_item')
        .order_by('order', 'anime_item__title')
    )

    seasons = []
    extras = []
    root_anime_id = None

    for link in links:
        entry = {
            'media_id': str(link.anime_item.media_id),
            'source': source,
            'media_type': MediaTypes.ANIME.value,
            'title': link.anime_item.title,
            'image': link.anime_item.image,
        }
        if link.is_extra:
            extras.append(entry)
        else:
            seasons.append(entry)
            if root_anime_id is None:
                root_anime_id = str(link.anime_item.media_id)

    synopsis = ''
    genres = []
    score = None
    score_count = None
    details = {}

    if root_anime_id:
        try:
            root_meta = get_media_metadata(MediaTypes.ANIME.value, root_anime_id, source)
            synopsis = root_meta.get('synopsis', '')
            genres = root_meta.get('genres', [])
            score = root_meta.get('score')
            score_count = root_meta.get('score_count')
            root_details = root_meta.get('details', {})
            details = {
                k: root_details[k]
                for k in ('start_date', 'studios', 'status')
                if root_details.get(k)
            }
        except Exception:
            logger.warning('Could not fetch metadata for AnimeSeries root %s', media_id)

    # Lazy-build AnimeSeriesRelation records on first page view (no-op if already done)
    try:
        from app.anime_series_builder import build_related_for_series  # noqa: PLC0415
        build_related_for_series(item, source)
    except Exception:
        logger.warning('Could not build related series for AnimeSeries %s', media_id)

    relations = list(
        AnimeSeriesRelation.objects.filter(from_series=item).select_related('to_series')
    )
    if relations:
        related_item_ids = [rel.to_series_id for rel in relations]
        counts_qs = (
            AnimeSeriesLink.objects.filter(
                series_item_id__in=related_item_ids, is_extra=False,
            )
            .values('series_item_id')
            .annotate(n=models.Count('id'))
        )
        related_season_counts = {row['series_item_id']: row['n'] for row in counts_qs}
    else:
        related_season_counts = {}

    related_series = [
        {
            'media_id': str(rel.to_series.media_id),
            'source': source,
            'media_type': MediaTypes.ANIME_SERIES.value,
            'title': rel.to_series.title,
            'image': rel.to_series.image,
            'relation_type': rel.relation_type,
            'season_count': related_season_counts.get(rel.to_series_id, 0),
        }
        for rel in relations
    ]

    related = {}
    if seasons:
        related['seasons'] = seasons
    if extras:
        related['extras'] = extras
    if related_series:
        related['related_series'] = related_series

    return {
        'title': item.title,
        'image': item.image,
        'media_type': MediaTypes.ANIME_SERIES.value,
        'media_id': str(item.media_id),
        'source': source,
        'source_url': '',
        'synopsis': synopsis,
        'genres': genres,
        'details': details,
        'related': related,
        'episodes': [],
        'cast': [],
        'score': score,
        'score_count': score_count,
        'season_count': len(seasons),
    }


def _search_anime_series(query: str, page: int, source: str) -> dict:
    """Search for AnimeSeries by querying MAL anime with related_anime data.

    Results already in AnimeSeriesLink are collapsed to their AnimeSeries.
    Unlinked results are grouped on-the-fly via Union-Find over sequel/prequel edges,
    and new groups are lazily saved to DB so future searches are instant.
    Truly standalone results (no groupable neighbours in the page) fall back to anime.
    """
    from app.anime_series_builder import lazy_build_from_search_results

    per_page = settings.PER_PAGE
    raw_results = mal.search_animeseries(query, page)

    if not raw_results:
        return format_search_response(page, per_page, 0, [])

    # Check which results already have an AnimeSeries link in DB
    result_ids = [str(r['media_id']) for r in raw_results]
    links = AnimeSeriesLink.objects.filter(
        anime_item__media_id__in=result_ids,
        anime_item__source=source,
    ).select_related('series_item')
    anime_to_series = {str(lnk.anime_item.media_id): lnk.series_item for lnk in links}

    # For unlinked results, fetch related_anime + start_date individually (cached).
    # MAL search endpoint doesn't return related_anime even when requested via fields.
    unlinked_by_id: dict[str, dict] = {}
    for r in raw_results:
        mid = str(r['media_id'])
        if mid in anime_to_series:
            continue
        try:
            rel_data = mal.get_relations_and_date(mid)
            unlinked_by_id[mid] = {
                **r,
                'start_date': r.get('start_date') or rel_data.get('start_date'),
                'total_episodes': rel_data.get('num_episodes'),
                'related_anime': rel_data.get('related_anime', []),
            }
        except Exception:
            logger.warning('Could not fetch relations for anime id=%s', mid)
            unlinked_by_id[mid] = {**r, 'related_anime': []}

    # Union-Find over unlinked results using sequel/prequel edges
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    for mid, result in unlinked_by_id.items():
        for rel in result.get('related_anime', []):
            rel_id = str(rel['media_id'])
            if rel.get('relation_type') in ('sequel', 'prequel') and rel_id in unlinked_by_id:
                union(mid, rel_id)

    # Collect groups keyed by their Union-Find root
    uf_groups: dict[str, list[dict]] = {}
    for mid, result in unlinked_by_id.items():
        uf_groups.setdefault(find(mid), []).append(result)

    # Lazy-build DB entries for all UF groups (single- and multi-member).
    # Singletons get a one-member series now; build_anime_series fills in sequels later.
    new_series: dict[str, Item] = {}  # uf_root → series Item
    for uf_root, members in uf_groups.items():
        try:
            new_series[uf_root] = lazy_build_from_search_results(members, source)
        except Exception:
            logger.exception('Lazy build failed for uf_root=%s', uf_root)

    # Batch-query season counts for all series items (linked + newly built)
    all_series_items = list(anime_to_series.values()) + list(new_series.values())
    if all_series_items:
        counts_qs = (
            AnimeSeriesLink.objects.filter(
                series_item_id__in=[s.pk for s in all_series_items], is_extra=False,
            )
            .values('series_item_id')
            .annotate(n=models.Count('id'))
        )
        pk_to_season_count = {row['series_item_id']: row['n'] for row in counts_qs}
    else:
        pk_to_season_count = {}

    # Build final results preserving original search order with deduplication
    seen_series_ids: set[str] = set()
    final_results: list[dict] = []

    for result in raw_results:
        mid = str(result['media_id'])

        if mid in anime_to_series:
            series = anime_to_series[mid]
        elif mid in unlinked_by_id:
            uf_root = find(mid)
            series = new_series.get(uf_root)
        else:
            series = None

        if series:
            sid = str(series.media_id)
            if sid not in seen_series_ids:
                seen_series_ids.add(sid)
                final_results.append({
                    'media_id': sid,
                    'source': source,
                    'media_type': MediaTypes.ANIME_SERIES.value,
                    'title': series.title,
                    'image': series.image,
                    'season_count': pk_to_season_count.get(series.pk, 0),
                })

    return format_search_response(page, per_page, len(final_results), final_results)


def get_media_metadata(
    media_type,
    media_id,
    source,
    season_numbers=None,
    episode_number=None,
):
    """Return the metadata for the selected media."""
    if source == Sources.MANUAL.value:
        if media_type == MediaTypes.SEASON.value:
            return manual.season(media_id, season_numbers[0])
        if media_type == MediaTypes.EPISODE.value:
            return manual.episode(media_id, season_numbers[0], episode_number)
        if media_type == "tv_with_seasons":
            media_type = MediaTypes.TV.value
        return manual.metadata(media_id, media_type)

    if media_type == MediaTypes.ANIME_SERIES.value:
        return _anime_series_metadata(media_id, source)

    metadata_retrievers = {
        MediaTypes.ANIME.value: lambda: mal.anime(media_id),
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.manga(media_id)
            if source == Sources.MANGAUPDATES.value
            else mal.manga(media_id)
        ),
        MediaTypes.TV.value: lambda: tmdb.tv(media_id),
        "tv_with_seasons": lambda: tmdb.tv_with_seasons(media_id, season_numbers),
        MediaTypes.SEASON.value: lambda: tmdb.tv_with_seasons(media_id, season_numbers)[
            f"season/{season_numbers[0]}"
        ],
        MediaTypes.EPISODE.value: lambda: tmdb.episode(
            media_id,
            season_numbers[0],
            episode_number,
        ),
        MediaTypes.MOVIE.value: lambda: tmdb.movie(media_id),
        MediaTypes.GAME.value: lambda: igdb.game(media_id),
        MediaTypes.BOOK.value: lambda: (
            hardcover.book(media_id)
            if source == Sources.HARDCOVER.value
            else openlibrary.book(media_id)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.comic(media_id),
        MediaTypes.BOARDGAME.value: lambda: bgg.boardgame(media_id),
    }
    return metadata_retrievers[media_type]()


def search(media_type, query, page, source=None):
    """Search for media based on the query and return the results."""
    search_handlers = {
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.search(query, page)
            if source == Sources.MANGAUPDATES.value
            else mal.search(media_type, query, page)
        ),
        MediaTypes.ANIME.value: lambda: mal.search(media_type, query, page),
        MediaTypes.ANIME_SERIES.value: lambda: _search_anime_series(query, page, source),
        MediaTypes.TV.value: lambda: tmdb.search(media_type, query, page),
        MediaTypes.MOVIE.value: lambda: tmdb.search(media_type, query, page),
        MediaTypes.SEASON.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
        MediaTypes.EPISODE.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
        MediaTypes.GAME.value: lambda: igdb.search(query, page),
        MediaTypes.BOOK.value: lambda: (
            openlibrary.search(query, page)
            if source == Sources.OPENLIBRARY.value
            else hardcover.search(query, page)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.search(query, page),
        MediaTypes.BOARDGAME.value: lambda: bgg.search(query, page),
    }
    return search_handlers[media_type]()
