"""Service for building AnimeSeries groupings from tracked anime.

Used by both the management command and the admin view.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import transaction

from django.db.models import Q

from app import providers
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

logger = logging.getLogger(__name__)

User = get_user_model()

# Main series entries — sequel/prequel chain
_MAIN_RELATION_TYPES = frozenset({'sequel', 'prequel'})
# Extra content reached FROM a main entry (OVA, specials, compilations)
_EXTRA_RELATION_TYPES = frozenset({'side_story', 'summary'})
# Main content reached FROM an extra entry (OVA points back to its parent)
_PARENT_RELATION_TYPES = frozenset({'parent_story', 'full_story'})
# Separate adaptations — traverse into dubious warning, not into the group
_DUBIOUS_RELATION_TYPES = frozenset({'alternative_version', 'spin_off'})
# Alternative/adaptation links that become AnimeSeriesRelation entries
_ALTERNATIVE_RELATION_TYPES = frozenset({'alternative_version', 'alternative_setting', 'adaptation'})


@dataclass
class GroupMember:
    """A single anime that belongs to a proposed series group."""

    media_id: str
    title: str
    image: str
    is_extra: bool = False
    start_date: str | None = None
    total_episodes: int | None = None


@dataclass
class ProposedGroup:
    """A proposed AnimeSeries grouping discovered via BFS traversal."""

    members: list[GroupMember] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_dubious(self) -> bool:
        """Return True if the group contains uncertain relation types."""
        return bool(self.warnings)

    @property
    def root_title(self) -> str:
        """Return the title of the first discovered member."""
        return self.members[0].title if self.members else 'Unknown'


@dataclass
class BuildReport:
    """Result of a build_anime_series run."""

    groups: list[ProposedGroup] = field(default_factory=list)
    already_linked: list[str] = field(default_factory=list)
    standalone: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@transaction.atomic
def lazy_build_from_search_results(members: list[dict], source: str) -> Item:
    """Create or extend an AnimeSeries from a partial set of members found via search.

    Members are anime dicts from MAL search results (media_id, title, image, start_date).
    All members must be main entries connected via sequel/prequel — is_extra is False for all.
    Does not do a full BFS; the series may be extended later by build_anime_series or Anime.save().
    """
    sorted_members = sorted(
        members,
        key=lambda m: (m.get('start_date') is None, m.get('start_date') or ''),
    )
    member_ids = [str(m['media_id']) for m in sorted_members]

    # If any member is already linked, add newcomers to the existing series
    existing_link = AnimeSeriesLink.objects.filter(
        anime_item__media_id__in=member_ids,
        anime_item__source=source,
    ).select_related('series_item').first()

    series_item = existing_link.series_item if existing_link else Item.objects.create(
        media_id=str(uuid.uuid4()),
        source=source,
        media_type=MediaTypes.ANIME_SERIES.value,
        title=sorted_members[0]['title'],
        image=sorted_members[0]['image'],
    )

    existing_count = AnimeSeriesLink.objects.filter(series_item=series_item).count()
    for order, member in enumerate(sorted_members, start=existing_count + 1):
        anime_item, _ = Item.objects.get_or_create(
            media_id=str(member['media_id']),
            media_type=MediaTypes.ANIME.value,
            source=source,
            defaults={'title': member['title'], 'image': member['image']},
        )
        AnimeSeriesLink.objects.get_or_create(
            series_item=series_item,
            anime_item=anime_item,
            defaults={
                'is_extra': False,
                'order': order,
                'total_episodes': member.get('total_episodes'),
            },
        )

    return series_item


def build_anime_series(
    users=None,
    dry_run: bool = True,
) -> BuildReport:
    """Build AnimeSeries groupings for all tracked anime.

    Args:
        users: Iterable of User instances to process. None means all users.
        dry_run: If True, only propose groups without writing to the database.

    Returns:
        BuildReport with proposed/applied groupings and any warnings.
    """
    report = BuildReport()
    global_visited: set[str] = set()
    created_series: list[Item] = []

    # Items tracked by the target users that have no series link yet
    qs = Item.objects.filter(
        media_type=MediaTypes.ANIME.value,
        series_memberships__isnull=True,
    )
    if users is not None:
        qs = qs.filter(anime__user__in=users)
    qs = qs.select_related().distinct()

    for item in qs:
        if item.media_id in global_visited:
            continue

        if AnimeSeriesLink.objects.filter(anime_item=item).exists():
            report.already_linked.append(item.title)
            global_visited.add(item.media_id)
            continue

        members, warnings, errors = _discover_group(
            item.media_id, item.source, global_visited,
        )
        report.errors.extend(errors)

        if len(members) <= 1:
            report.standalone.append(item.title)
            continue

        group = ProposedGroup(
            members=[
                GroupMember(
                    mid,
                    info['title'],
                    info['image'],
                    info.get('is_extra', False),
                    info.get('start_date'),
                    info.get('total_episodes'),
                )
                for mid, info in members.items()
            ],
            warnings=warnings,
        )
        report.groups.append(group)

        if not dry_run:
            try:
                series_item = _apply_group(group, item.source, users)
                created_series.append(series_item)
            except Exception:
                logger.exception('Failed to apply group for %s', group.root_title)
                report.errors.append(f'DB error applying group for {group.root_title!r}')

    # Step 4: build AnimeSeriesRelation for all newly created series
    if not dry_run:
        for series_item in created_series:
            try:
                build_related_for_series(series_item, series_item.source)
            except Exception:
                logger.exception(
                    'Failed to build related series for %s', series_item.title,
                )

    return report


def _discover_group(
    start_media_id: str,
    source: str,
    global_visited: set[str],
) -> tuple[dict, list[str], list[str]]:
    """BFS traversal from start_media_id through the full relation graph.

    Traverses sequel/prequel (main), side_story/summary (extras reached from
    main), and parent_story/full_story (main reached from an OVA/extra).
    Marks each member with is_extra based on the incoming edge type, with
    retroactive correction when parent_story edges reveal the source is extra.

    Returns:
        members: {media_id: {'title': str, 'image': str, 'is_extra': bool}}
        warnings: dubious relation messages
        errors: API fetch errors
    """
    members: dict[str, dict] = {}
    warnings: list[str] = []
    errors: list[str] = []

    # Queue entries: (media_id, is_extra)
    local_visited: set[str] = {start_media_id}
    queue: list[tuple[str, bool]] = [(start_media_id, False)]

    while queue:
        current_id, current_is_extra = queue.pop(0)
        try:
            metadata = providers.services.get_media_metadata(
                MediaTypes.ANIME.value,
                current_id,
                source,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f'API error for id={current_id}: {exc}')
            continue

        members[current_id] = {
            'title': metadata.get('title', f'Unknown ({current_id})'),
            'image': metadata.get('image', ''),
            'is_extra': current_is_extra,
            'start_date': metadata.get('details', {}).get('start_date'),
            'total_episodes': metadata.get('max_progress'),
        }

        for related in metadata.get('related', {}).get('related_anime', []):
            rel_id = str(related['media_id'])
            rel_type = related.get('relation_type', '')

            if rel_type in _DUBIOUS_RELATION_TYPES:
                warnings.append(
                    f'{related["title"]!r} via {rel_type!r} — '
                    'may be a separate adaptation, verify manually',
                )
                continue

            if rel_type in _MAIN_RELATION_TYPES:
                # Sequel/prequel: destination is a main entry
                if rel_id not in local_visited and rel_id not in global_visited:
                    local_visited.add(rel_id)
                    queue.append((rel_id, False))

            elif rel_type in _EXTRA_RELATION_TYPES:
                # side_story/summary reached from a main entry: destination is extra
                if rel_id not in local_visited and rel_id not in global_visited:
                    local_visited.add(rel_id)
                    queue.append((rel_id, True))

            elif rel_type in _PARENT_RELATION_TYPES:
                # parent_story/full_story: current node is an OVA/extra,
                # destination is the main series entry
                members.setdefault(current_id, {})['is_extra'] = True
                if rel_id not in local_visited and rel_id not in global_visited:
                    local_visited.add(rel_id)
                    queue.append((rel_id, False))

    global_visited.update(local_visited)
    return members, warnings, errors


def _sort_key(m: GroupMember) -> tuple:
    """Sort key: main entries first, then extras; chronological within each group."""
    return (m.is_extra, m.start_date is None, m.start_date or '')


@transaction.atomic
def _apply_group(
    group: ProposedGroup,
    source: str,
    target_users=None,
) -> Item:
    """Create AnimeSeriesLink and per-user AnimeSeries for a proposed group."""
    # Sort chronologically: main entries first, extras after, None dates last.
    # This order drives the `order` field on AnimeSeriesLink.
    ordered_members = sorted(group.members, key=_sort_key)

    # Use the earliest main entry as the series title/image.
    first_main = next((m for m in ordered_members if not m.is_extra), ordered_members[0])

    series_item = Item.objects.create(
        media_id=str(uuid.uuid4()),
        source=source,
        media_type=MediaTypes.ANIME_SERIES.value,
        title=first_main.title,
        image=first_main.image,
    )

    # Global links for all members — create Item records for any that don't exist yet.
    # BFS has already fetched title/image for every member, so we have what we need.
    member_ids = [m.media_id for m in ordered_members]
    for order, member in enumerate(ordered_members, start=1):
        anime_item, _ = Item.objects.get_or_create(
            media_id=member.media_id,
            media_type=MediaTypes.ANIME.value,
            source=source,
            defaults={'title': member.title, 'image': member.image},
        )
        AnimeSeriesLink.objects.get_or_create(
            series_item=series_item,
            anime_item=anime_item,
            defaults={
                'is_extra': member.is_extra,
                'order': order,
                'total_episodes': member.total_episodes,
            },
        )

    # Per-user AnimeSeries tracking for users who track any group member
    tracked_qs = Anime.objects.filter(
        item__media_id__in=member_ids,
        item__media_type=MediaTypes.ANIME.value,
        item__source=source,
        related_series__isnull=True,
    ).select_related('user', 'item')
    if target_users is not None:
        tracked_qs = tracked_qs.filter(user__in=target_users)

    user_series: dict = {}
    to_update: list[Anime] = []

    for anime in tracked_qs:
        user = anime.user
        if user not in user_series:
            series, _ = AnimeSeries.objects.get_or_create(
                user=user,
                item=series_item,
                defaults={'status': anime.status},
            )
            user_series[user] = series
        anime.related_series = user_series[user]
        to_update.append(anime)

    if to_update:
        Anime.objects.bulk_update(to_update, ['related_series'])

    return series_item


def build_related_for_series(series_item: Item, source: str) -> None:
    """Lazy-build AnimeSeriesRelation records for alternative/adaptation links.

    Scans MAL related_anime for each member of the series. For any
    alternative_version / alternative_setting / adaptation link, finds or
    creates the target AnimeSeries (depth-1 BFS), then stores bidirectional
    AnimeSeriesRelation records. Skips if any relation already exists (either
    direction) — prevents re-discovery and stops depth recursion at 1.
    """
    if AnimeSeriesRelation.objects.filter(
        Q(from_series=series_item) | Q(to_series=series_item)
    ).exists():
        return

    member_ids = list(
        AnimeSeriesLink.objects.filter(series_item=series_item)
        .values_list('anime_item__media_id', flat=True)
    )
    member_id_set = set(str(m) for m in member_ids)

    # Collect alternative/adaptation seeds from member anime
    seeds: dict[str, str] = {}  # related_mal_id → relation_type
    for mal_id in member_ids:
        try:
            meta = providers.services.get_media_metadata(
                MediaTypes.ANIME.value, str(mal_id), source,
            )
        except Exception:
            continue
        for rel in meta.get('related', {}).get('related_anime', []):
            rel_type = rel.get('relation_type', '')
            rel_id = str(rel.get('media_id', ''))
            if rel_type in _ALTERNATIVE_RELATION_TYPES and rel_id not in member_id_set:
                seeds.setdefault(rel_id, rel_type)

    for rel_mal_id, rel_type in seeds.items():
        related_series_item = _find_or_build_series_for_anime(rel_mal_id, source)
        if related_series_item is None or related_series_item.pk == series_item.pk:
            continue
        AnimeSeriesRelation.objects.get_or_create(
            from_series=series_item,
            to_series=related_series_item,
            defaults={'relation_type': rel_type},
        )
        AnimeSeriesRelation.objects.get_or_create(
            from_series=related_series_item,
            to_series=series_item,
            defaults={'relation_type': rel_type},
        )


def _find_or_build_series_for_anime(mal_id: str, source: str) -> Item | None:
    """Return the AnimeSeries Item for a given anime MAL id, creating it if needed.

    Does a depth-1 BFS (sequel/prequel only) to find the series root.
    Creates a new AnimeSeries if the anime is not yet linked to one.
    """
    existing = (
        AnimeSeriesLink.objects.filter(
            anime_item__media_id=mal_id,
            anime_item__source=source,
        )
        .select_related('series_item')
        .first()
    )
    if existing:
        return existing.series_item

    members, _, errors = _discover_group(mal_id, source, global_visited=set())
    if errors or not members:
        return None

    group = ProposedGroup(
        members=[
            GroupMember(
                mid,
                info['title'],
                info['image'],
                info.get('is_extra', False),
                info.get('start_date'),
                info.get('total_episodes'),
            )
            for mid, info in members.items()
        ],
    )
    try:
        _apply_group(group, source, target_users=None)
    except Exception:
        logger.exception('Failed to build related series for anime %s', mal_id)
        return None

    link = (
        AnimeSeriesLink.objects.filter(
            anime_item__media_id=mal_id,
            anime_item__source=source,
        )
        .select_related('series_item')
        .first()
    )
    return link.series_item if link else None


def _track_next_season_if_completed(series: AnimeSeries) -> None:
    """Start tracking the next main season if the last tracked one is done.

    Catches up series whose last completion predates automatic next-season
    tracking (Anime._track_next_season), which only fires on new saves.
    """
    links = list(
        AnimeSeriesLink.objects.filter(
            series_item=series.item,
            is_extra=False,
        ).order_by('order')
    )
    if not links:
        return
    item_ids = [lnk.anime_item_id for lnk in links]

    tracked_statuses = dict(
        Anime.objects.filter(
            user=series.user,
            item_id__in=item_ids,
        ).values_list('item_id', 'status')
    )
    last_tracked_index = None
    for index, item_id in enumerate(item_ids):
        if item_id in tracked_statuses:
            last_tracked_index = index

    if last_tracked_index is None:
        return
    if tracked_statuses[item_ids[last_tracked_index]] != Status.COMPLETED.value:
        return

    Anime._track_season_after(
        series, series.user, links, last_tracked_index,
    )


def recompute_series_statuses() -> tuple[int, int]:
    """Recompute AnimeSeries.status for every series from its seasons.

    Also starts tracking the next main season if the last tracked one is
    completed (see _track_next_season_if_completed). Returns (updated,
    total). See AnimeSeries.status_for for the aggregation rule; used by
    both the management command and the admin view.
    """
    updated = 0
    total = 0
    for series in AnimeSeries.objects.all():
        total += 1
        original_status = series.status

        try:
            _track_next_season_if_completed(series)
        except Exception:
            logger.warning(
                'Could not auto-track next season for series %s', series.pk,
            )
        series.refresh_from_db()

        statuses = set(series.anime_seasons.values_list('status', flat=True))
        new_status = AnimeSeries.status_for(statuses)
        if new_status and new_status != series.status:
            AnimeSeries.objects.filter(pk=series.pk).update(status=new_status)
            series.status = new_status

        if series.status != original_status:
            updated += 1
    return updated, total
