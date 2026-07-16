# Anime Series

## Overview

Anime Series groups individual anime titles that belong to the same franchise — seasons, sequels, prequels, and extras — into a single trackable entity. The grouping is derived automatically from MAL relation data (sequel/prequel chains). Episode-level tracking is provided for each individual anime in a group, with watch state stored per episode and progress synced back to the parent record.

Two data sources are used:

- **MAL API v2** — search, metadata, and relation graph
- **Simkl API** — episode metadata (title, description, air date, thumbnail)

---

## User Guide

### Enabling Anime Series

Anime Series is enabled by default for all users. When enabled, the home page shows AnimeSeries cards instead of individual Anime cards, and the media type is available in search and the library.

To disable it, go to **Profile Settings** and toggle the Anime Series option. When disabled, individual Anime entries appear on the home page and library as before.

### Searching and Adding Series

In the search bar, select **Anime Series** from the media type dropdown and type a query. Results are returned as franchise cards showing:

- Poster, title, year range
- Number of seasons badge (e.g. "3 seasons")
- Status badge if you are already tracking the series

Click a result to open the series detail page. Use the track button to start tracking the series. Tracking a series does not automatically track its individual seasons; each season must be tracked separately.

### Tracking Episodes

Open any individual anime page (a season within a series). The episode list loads automatically — no additional action is needed.

For authenticated users, each episode has a watched toggle button. Clicking it:

1. Marks the episode as watched (or unmarks it if already watched)
2. Updates the season's episode counter
3. If all non-special episodes are watched, automatically sets the season status to Completed

If you open an anime's episode page for the first time and the anime already has a numeric progress value (tracked before episode-level tracking was available), that progress is migrated: watched records are created for episodes 1 through the stored number.

If you are not currently tracking an anime and click a watched toggle, tracking is started automatically with status "In Progress".

**Spoiler obfuscation:** if the user setting "Obfuscate unseen episodes" is enabled, thumbnails, titles, and descriptions of unwatched episodes are blurred. Clicking any blurred element reveals it without marking it watched.

### Viewing Progress

Progress counters appear at two levels:

**Season level** — shown on the individual anime card and detail page. Displays watched episodes out of total (e.g. "12/25"). The total comes from MAL episode count, falling back to calendar events, and then to the count of stored episode records.

**Series level** — shown on the AnimeSeries card and detail page. Displays the sum of watched episodes across all non-extra seasons out of the total episode count for those seasons. Extras are excluded from both numerator and denominator.

### Series Page

The AnimeSeries detail page has three sections:

**Seasons** — all non-extra members ordered chronologically by start date. Each card shows poster, title, year, episode progress, and status badge.

**Extras** — side stories, summaries, and OVAs linked to the franchise, ordered by start date. These are tracked individually but do not count toward the series progress total.

**Related** — other AnimeSeries entries that share an alternative version, alternative setting, or adaptation relationship with this franchise. Each card shows poster, title, relation type, and season count.

A "Part of [Series Name]" link is shown above the episode list on each individual anime detail page and links back to the parent AnimeSeries page.

---

## Operator Guide

### Initial Setup

After running Django migrations (`python manage.py migrate`), run `build_anime_series` to create AnimeSeries groupings for all currently tracked anime:

```
python manage.py build_anime_series --dry-run
```

Review the output, then apply:

```
python manage.py build_anime_series
```

This is safe to run repeatedly — already-linked anime are skipped.

If `AnimeSeriesLink` rows exist from an earlier deployment without the `total_episodes` field populated, run:

```
python manage.py backfill_anime_episode_counts
```

### Management Commands

#### build_anime_series

```
python manage.py build_anime_series [--dry-run] [--user=USER_ID]
```

Scans all tracked anime that are not yet linked to an AnimeSeries. For each, performs a BFS traversal of the MAL relation graph over sequel/prequel edges to discover the full franchise chain, then creates:

- A shared `Item` of type `animeseries`
- `AnimeSeriesLink` records for each chain member (main and extras)
- `AnimeSeriesRelation` records for alternative versions, alternative settings, and adaptations (depth-1 only)
- Per-user `AnimeSeries` tracking records for users who track at least one group member

**Options:**

| Flag | Description |
|---|---|
| `--dry-run` | Propose groups without writing to the database. Prints a report of what would be created. |
| `--user USER_ID` | Process only the specified user (integer primary key). Useful for testing or backfilling a single account. |

The command prints a report listing each group with its members, any warnings for ambiguous relation types, and a summary count.

#### backfill_anime_episode_counts

```
python manage.py backfill_anime_episode_counts
```

Fetches the `num_episodes` field from MAL for each `AnimeSeriesLink` row where `total_episodes` is null. Runs sequentially and respects the MAL API key configured in environment variables. Safe to re-run — rows already populated are skipped.

Use this after first deploying the `total_episodes` field if `AnimeSeriesLink` rows were created by an older version of `build_anime_series`.

#### backfill_anime_series_status

```
python manage.py backfill_anime_series_status
```

Recomputes `AnimeSeries.status` for every series from its seasons' current statuses (same aggregate rule as `Anime._sync_series_status`: one completed season plus another not yet started still counts as "in progress" overall). Series are normally kept in sync automatically whenever a season's status changes, but a series seeded before that logic existed, or before a bug fix to it, can be left with a stale status. Safe to re-run — only series whose computed status actually differs are updated.

Run this once after upgrading if you have existing Anime Series data from before this command was introduced.

Also available from the admin as a **Recompute Status** tool page at `/admin/app/animeseries/recompute-status/` (requires `ADMIN_ENABLED`, see below).

### Configuration

The following environment variables affect Anime Series behaviour:

| Variable | Description |
|---|---|
| `MAL_API` | MyAnimeList API v2 client ID. Required for search, relation fetching, and episode count backfill. |
| `SIMKL_API` | Simkl API key. Required for episode metadata fetch on first page open. |
| `ADMIN_ENABLED` | Set to `true` to enable the Django admin interface. The admin provides read-only views of `SimklMapping`, `AnimeEpisode`, and `WatchedAnimeEpisode`, allows manual adjustment of `AnimeSeriesLink` and `AnimeSeriesRelation` records, and exposes a **Build Series** tool page at `/admin/app/anime/build-series/` where you can run `build_anime_series` (with optional dry-run and per-user filter) directly from the browser, and a **Recompute Status** tool page at `/admin/app/animeseries/recompute-status/` for `backfill_anime_series_status`. |

Per-user settings stored in `User` model (configurable via Profile Settings):

| Field | Default | Description |
|---|---|---|
| `animeseries_enabled` | `True` | Show AnimeSeries on home page and library instead of individual Anime |
| `animeseries_layout` | `table` | Display layout for the AnimeSeries list page |
| `animeseries_sort` | `score` | Default sort order for the AnimeSeries list page |
| `animeseries_status` | `all` | Default status filter for the AnimeSeries list page |

### Search Bar Layout

The media type dropdown in the search bar now uses `whitespace-nowrap` to prevent multi-line labels (e.g. "Anime Series"), and the flex container uses `items-center` so the magnifying glass icon stays vertically centred regardless of the dropdown button height.

### Deploying a Database Dump

If you restore a database dump from another environment (e.g. dev to production), no migrations or manual data steps are required — the dump carries the full schema and all data. Redis cache is populated lazily on first request and does not need pre-warming.

The one exception is if the dump was taken before `backfill_anime_episode_counts` was run on the source environment, in which case some `AnimeSeriesLink.total_episodes` values may be null. Run the backfill after restoring the dump:

```
python manage.py backfill_anime_episode_counts
```

---

## Developer Reference

### Data Model

The Anime Series feature introduces six new models and modifies two existing ones.

---

**`AnimeSeriesLink`** (global, shared across users)

Maps a single anime `Item` to an AnimeSeries `Item`. The relationship is global — all users who track an anime share the same link record.

| Field | Type | Notes |
|---|---|---|
| `series_item` | FK → Item(animeseries) | The owning series |
| `anime_item` | FK → Item(anime) | The member anime |
| `order` | PositiveIntegerField, nullable | Chronological position within the series |
| `is_extra` | BooleanField | `False` = main season, `True` = extra (OVA, special, summary) |
| `total_episodes` | PositiveSmallIntegerField, nullable | Populated from MAL `num_episodes` at build time. Primary source for `_annotate_animeseries_progress`. |

Unique constraint on `(series_item, anime_item)`.

---

**`AnimeSeriesRelation`** (global)

Depth-1 cross-franchise relation between two AnimeSeries items. Stored bidirectionally: both `(A→B)` and `(B→A)` are created when a relation is found.

| Field | Type | Notes |
|---|---|---|
| `from_series` | FK → Item(animeseries) | Source series |
| `to_series` | FK → Item(animeseries) | Target series |
| `relation_type` | CharField | One of: `alternative_version`, `alternative_setting`, `adaptation` |

---

**`AnimeSeries`** (per-user)

Per-user tracking record for a franchise, mirroring how `TV` works. Inherits `status`, `score`, `start_date`, `end_date`, `notes` from `Media`.

`progress` is a **property** (not a DB field). It sums `Anime.progress` across all non-extra linked seasons for this user, deduplicating by `item_id` and taking the highest value per item. This guards against legacy duplicate `Anime` rows.

`last_watched` is a property returning the title of the season with the most recent `progressed_at` timestamp.

`max_progress` is set at query time by `_annotate_animeseries_progress` (see Key Functions).

---

**`SimklMapping`** (global)

Caches the MAL → Simkl ID mapping for each anime. Created on first episode page open.

| Field | Type | Notes |
|---|---|---|
| `item` | OneToOneField → Item(anime) | |
| `simkl_id` | IntegerField | Simkl's numeric ID for this title |
| `cached_at` | DateTimeField | Set at creation (`auto_now_add`) |

---

**`AnimeEpisode`** (global)

Episode metadata fetched from Simkl. Shared across all users.

| Field | Type | Notes |
|---|---|---|
| `anime_item` | FK → Item(anime) | |
| `episode_number` | PositiveSmallIntegerField | |
| `title` | CharField(500) | May be empty for unreleased episodes |
| `description` | TextField | Episode synopsis |
| `aired` | DateTimeField, nullable | UTC datetime parsed from Simkl ISO 8601 string |
| `img` | URLField, nullable | Thumbnail at `https://simkl.net/episodes/{key}_w.jpg` |
| `is_special` | BooleanField | Specials excluded from progress counting and auto-complete |

Unique constraint on `(anime_item, episode_number)`.

---

**`WatchedAnimeEpisode`** (per-user)

Records that a specific user has watched a specific episode.

| Field | Type | Notes |
|---|---|---|
| `user` | FK → User | |
| `anime` | FK → Anime | The user's per-user Anime tracking record |
| `episode` | FK → AnimeEpisode | |
| `watched_at` | DateTimeField | Set at creation (`auto_now_add`) |

Unique constraint on `(anime, episode)`.

---

**`Anime`** (modified)

`related_series` FK → AnimeSeries (nullable) links an `Anime` tracking record to its parent series for the user. `progress` is a **DB field** (`PositiveSmallIntegerField`), not a property. It is kept in sync with the count of `WatchedAnimeEpisode` records by `_sync_progress()`.

---

### Key Functions

#### `_annotate_animeseries_progress(series_list)`

Location: `app/models.py`, `MediaManager`

Called during list and home page queries to set `max_progress` and `_main_anime_ids` on each `AnimeSeries` instance. Uses a three-tier priority chain per member anime:

1. `AnimeSeriesLink.total_episodes` — populated from MAL at build time; most reliable
2. Calendar `Event.content_number` max — falls back to this when `total_episodes` is null
3. `AnimeEpisode` count — last resort when neither of the above exists

Extra members (`is_extra=True`) are excluded from both `max_progress` and `_main_anime_ids`. The pre-set `_main_anime_ids` attribute is then used by `AnimeSeries.progress` to avoid a second database query.

---

#### `lazy_build_from_search_results(members, source)`

Location: `app/anime_series_builder.py`

Creates or extends an `AnimeSeries` from a list of member dicts returned by MAL search. Used during search for both multi-member groups (connected by sequel/prequel edges in the result set) and single-member groups (isolated titles).

Behaviour:
- Members are sorted by `start_date` before linking, so `order` values are chronological
- If any member is already in an `AnimeSeriesLink`, the existing series item is reused and newcomers are appended
- `AnimeSeriesLink.get_or_create` makes the function idempotent: calling it twice with the same members produces the same DB state
- Does not perform BFS — only links the titles present in the current result page. Full franchise chains are built later by `build_anime_series`

---

#### `fetch_and_store_anime_episodes(item)`

Location: `app/providers/services.py`

Lazy-fetches episode data from Simkl for a given anime `Item`. No-op if `AnimeEpisode` records already exist for the item. On first call:

1. Calls `simkl.get_simkl_id(mal_id)` to resolve the MAL → Simkl mapping; stores in `SimklMapping`
2. Calls `simkl.get_episodes(simkl_id)` to retrieve the full episode list
3. Bulk-creates `AnimeEpisode` records with `ignore_conflicts=True`

Aired dates are parsed with `django.utils.dateparse.parse_datetime` (handles ISO 8601 with timezone), with a fallback strptime for `YYYY-MM-DD HH:MM` strings without a timezone offset.

---

#### `Anime._migrate_legacy_progress()`

Location: `app/models.py`

Runs once per `Anime` record on the first episode page open. If `Anime.progress > 0` and no `WatchedAnimeEpisode` records exist yet, creates watch records for episodes 1 through `progress`. This converts the old numeric progress (entered manually before episode tracking existed) into the new per-episode format.

Subsequent calls are a no-op because `WatchedAnimeEpisode` records now exist.

---

#### `Anime._sync_progress()`

Location: `app/models.py`

Recounts `WatchedAnimeEpisode` for the `Anime` instance and updates the `progress` DB field. Called after every episode toggle. Also handles auto-completion:

- If `count >= total_non_special_episodes` and status is not already Completed, sets `status = Completed` and `end_date = now`
- If the anime has a `related_series`, propagates status recalculation to the parent `AnimeSeries`

Uses `update()` rather than `save()` to avoid triggering unrelated model hooks.

---

### Episode Data Flow

**First page open for an untracked anime:**

1. `media_details` view calls `_enrich_anime_episodes(media_metadata, current_instance=None)`
2. `_enrich_anime_episodes` calls `Item.objects.get_or_create` using the MAL media ID so episode fetch works even without a user tracking record
3. `fetch_and_store_anime_episodes(item)` is called; it resolves the Simkl ID (one API request), fetches episodes (one API request), and bulk-inserts `AnimeEpisode` rows
4. Episodes are read from DB and attached to `media_metadata['episodes']` with `watched=False` for all
5. Template renders the episode list with toggle buttons visible to authenticated users

**Subsequent page opens:**

`fetch_and_store_anime_episodes` is a no-op (episodes already exist). Episodes are read from DB. For tracked users, `WatchedAnimeEpisode` records are fetched and their PKs are mapped onto the episode list.

**Episode toggle (watched → unwatched or unwatched → watched):**

1. `POST /anime-episode-toggle` handled by `anime_episode_toggle` view
2. View looks up `Item` by `item_pk` and `AnimeEpisode` by `episode_pk`
3. `Anime.objects.get_or_create` ensures a tracking record exists (creates one with `IN_PROGRESS` if not)
4. `WatchedAnimeEpisode` is created or deleted depending on current state
5. `anime._sync_progress()` recounts watched episodes, updates `Anime.progress`, and triggers auto-complete if all episodes are watched
6. Response redirects back to the same page (standard `redirect_back` helper)
