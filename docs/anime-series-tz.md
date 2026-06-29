# Technical Specification: AnimeSeries for Yamtrack

## 1. Data Sources

| Source | Role |
|---|---|
| MAL API v2 | Search, metadata, relations between titles |
| Simkl API | Episode data (title, description, aired date, thumbnail) |

MAL → Simkl bridge: `GET /search/id?mal={id}` → simkl_id → `GET /anime/episodes/{simkl_id}`

---

## 2. MAL Relation Type Classification

| Category | Relation types | Usage |
|---|---|---|
| Main chain | `sequel`, `prequel` | AnimeSeries seasons (is_extra=False) |
| Extras (reached from main) | `side_story`, `summary` | Extras section (is_extra=True) |
| Main (reached from extra) | `parent_story`, `full_story` | Marks the source node as extra, traverses destination as main |
| Related | `alternative_version`, `alternative_setting`, `adaptation` | Related section (separate AnimeSeries) |
| Dubious | `alternative_version`, `spin_off` | Warning logged, not added to the group |
| Ignored | `character`, `other` | Not traversed |

Full list of MAL relation types (12 total): `sequel`, `prequel`, `adaptation`, `alternative_setting`, `alternative_version`, `side_story`, `parent_story`, `summary`, `full_story`, `spin_off`, `character`, `other`.

---

## 3. Data Models

### New tables

**`AnimeSeries`** (per-user, mirrors TV):
- `user` FK, `item` FK → Item(animeseries), `status`, `score`, `start_date`, `end_date`, `notes`
- `progress` — property: sum of `Anime.progress` across non-extra seasons (deduped by item_id)
- `last_watched` — property: title of the most recently progressed anime season

**`AnimeSeriesLink`** (global):
- `series_item` FK → Item(animeseries), `anime_item` FK → Item(anime)
- `order` PositiveIntegerField (chronological ordering)
- `is_extra` BooleanField (False = season, True = extra)
- `total_episodes` PositiveSmallIntegerField (nullable) — populated from MAL `num_episodes` at build time; used by `_annotate_animeseries_progress` as the primary episode-count source

**`AnimeSeriesRelation`** (global, depth-1 links between series):
- `from_series` FK → Item(animeseries), `to_series` FK → Item(animeseries)
- `relation_type` CharField (alternative_version / alternative_setting / adaptation)

**`SimklMapping`** (global):
- `item` FK → Item(anime, 1-to-1), `simkl_id` IntegerField, `cached_at` DateTimeField

**`AnimeEpisode`** (global, episode metadata from Simkl):
- `anime_item` FK → Item(anime)
- `episode_number` PositiveSmallIntegerField
- `title` CharField, `description` TextField
- `aired` DateTimeField (nullable), `img` URLField (nullable)
- `is_special` BooleanField (Simkl type=special)
- unique_together: `(anime_item, episode_number)`

**`WatchedAnimeEpisode`** (per-user):
- `user` FK, `anime` FK → Anime (per-user), `episode` FK → AnimeEpisode
- `watched_at` DateTimeField (auto_now_add)
- unique_together: `(anime, episode)`

### Modified existing models

**`MediaTypes`**: added `ANIME_SERIES = "animeseries"`, `max_length` bumped to 15 on `Item.media_type` and `User.last_search_type`.

**`Anime`** (per-user):
- Added `related_series` FK → AnimeSeries (nullable)
- `progress` remains a **DB field** (PositiveSmallIntegerField), synced from `WatchedAnimeEpisode` count via `_sync_progress()`. Not a property.
- `_sync_progress()` — recounts `WatchedAnimeEpisode`, updates `progress`, triggers auto-complete when all non-special episodes watched
- `_migrate_legacy_progress()` — on first episode page open, creates `WatchedAnimeEpisode` records for episodes 1..progress if none exist yet

**`User`**: added `animeseries_enabled`, `animeseries_layout`, `animeseries_sort`, `animeseries_status`.

---

## 4. Direction 1 — Search

### Request flow for `media_type=animeseries`

1. `GET /anime?q={query}&fields=media_type,start_date` — flat list from MAL
2. Split results into linked (already in AnimeSeriesLink) and unlinked
3. For unlinked: `GET /anime/{id}?fields=related_anime,start_date,num_episodes` per result (Redis-cached)
4. Union-Find grouping by sequel/prequel edges **within the result set**
5. **All UF groups** (including single-member) → lazy build: `lazy_build_from_search_results` creates AnimeSeriesLink + Item(animeseries) in DB
6. Singletons whose series chain extends off-page will be extended later by `build_anime_series`
7. Deduplicate: one result per franchise

### Result card
Poster, title, year range, season count badge ("N seasons"). Tracked titles show a status badge.

### Performance
- First search: N+1 MAL requests; results saved to DB + Redis
- Subsequent searches: O(1) DB lookups

### Lazy build scope during search
Only the sequel/prequel entries present on the current result page are grouped. BFS over the full MAL graph is **not run during search** — that is the job of `build_anime_series`.

---

## 5. Direction 2 — AnimeSeries Pages

### List page (`/@{username}/animeseries`)

Table layout identical to TV shows:

| Title | Score | Progress | Last Watched | Status | Start Date | End Date |
|---|---|---|---|---|---|---|

Progress = total watched episodes / total episodes across all non-extra seasons.
`max_progress` source priority: `AnimeSeriesLink.total_episodes` → calendar Events → `AnimeEpisode` count.

### AnimeSeries detail page

```
[Poster]     Title (year–year)
[Edit btn]   Genres · Studio
             Synopsis
             User note
             MAL score: X.X  |  My score: X.X

── Seasons ──────────────────────────────────
[card] [card] [card] ...

── Extras ───────────────────────────────────
[card] [card] ...

── Related ──────────────────────────────────
[card] [card] ...
```

**Seasons**: anime cards from AnimeSeriesLink (`is_extra=False`), sorted by start_date.
Each card: poster, title, year, episode progress (X/N), status badge.

**Extras**: flat list sorted by start_date. Cards from AnimeSeriesLink (`is_extra=True`). No consolidation.

**Related**: cards for other AnimeSeries entries from AnimeSeriesRelation.
Each card: poster, title, relation type badge ("Alternative Version"), season count.

### Related section — lazy build on page open

1. Check `AnimeSeriesRelation` for the current series
2. If no relations exist, scan each member's MAL `related_anime` for alternative_version / alternative_setting / adaptation links
3. For each such link: depth-1 BFS → find or create the related AnimeSeries → store AnimeSeriesRelation
4. Newly built related AnimeSeries do **not** build their own Related section — depth limit is strictly 1

---

## 6. Direction 3 — Episode Tracking

### Individual anime (season) detail page

Shows only the **Episodes block** — all other sections (Related, Series navigation) are hidden.
A "Part of [Series Name]" link above the episode list navigates to the AnimeSeries page.

Episodes are shown for **all visitors** (tracked and untracked). Watched toggles are visible to authenticated users only.

### Episodes block

- Episode count header (`N total · M watched`)
- Episode list: thumbnail (md:w-64 md:h-40), number, title, air date, collapsible synopsis, watched toggle button
- Unseen episodes blurred according to user `obfuscate_unseen_episodes` setting
- Watched toggle auto-creates an `Anime` tracking record (status=In Progress) if none exists

### Lazy episode fetch (first page open)

1. `Item` is get-or-created from MAL metadata (so episode fetch works for untracked anime too)
2. Check whether `AnimeEpisode` records exist for this Item
3. If not:
   - `GET /search/id?mal={id}` → store in `SimklMapping`
   - `GET /anime/episodes/{simkl_id}` → store all episodes in `AnimeEpisode`
4. Subsequent visits served from DB (no Simkl calls)

### Progress synchronisation

- Toggle episode watched → create/delete `WatchedAnimeEpisode` → `_sync_progress()` recounts and updates `Anime.progress`
- All non-special episodes watched → auto-set `Anime.status = Completed`
- `Anime.status` change → recalculate `AnimeSeries.status`

### Legacy progress migration

On first episode page open, if `Anime.progress > 0` and no `WatchedAnimeEpisode` exist: create records for episodes 1..progress (best-effort — count known, specific episodes not).

---

## 7. Builder: `build_anime_series`

```
python manage.py build_anime_series [--dry-run] [--user=USER_ID]
```

### Algorithm

1. Iterate tracked Anime with no `related_series`
2. BFS via MAL `related_anime` (sequel/prequel only) → main chain; side_story/summary → extras
3. Create Item(animeseries) + AnimeSeriesLink for all chain members including untracked ones
4. Store `total_episodes` (from `num_episodes` in MAL metadata) on each AnimeSeriesLink
5. Collect alternative_version / alternative_setting / adaptation seeds → depth-1 BFS → create related AnimeSeries + AnimeSeriesRelation records
6. Create per-user AnimeSeries records for users who track at least one group member

### Extras handling

`side_story`/`summary` entries reached from a main entry are included in AnimeSeriesLink with `is_extra=True`.
`parent_story`/`full_story` edges from an OVA point back to the main series — the OVA is retroactively marked `is_extra=True`.

### Backfill command

```
python manage.py backfill_anime_episode_counts
```

Populates `AnimeSeriesLink.total_episodes` for rows created before this field existed, by fetching `num_episodes` from MAL.

---

## 8. Consolidation Management via Admin

### Operations

| Action | Implementation |
|---|---|
| Move Seasons ↔ Extras | Toggle `AnimeSeriesLink.is_extra` |
| Remove from series | Delete `AnimeSeriesLink` record |
| Move to Related | Find/build AnimeSeries for the anime → create AnimeSeriesRelation → delete AnimeSeriesLink |
| Manage Related links | Add/delete AnimeSeriesRelation records, edit relation_type |

Admin registers `SimklMapping`, `AnimeEpisode`, `WatchedAnimeEpisode` as read-only special models (no status/score fields).

---

## 9. Migration

### Django migrations

1. `MediaTypes.ANIME_SERIES` + max_length bump on Item.media_type and User.last_search_type
2. New models: AnimeSeries, AnimeSeriesLink, AnimeSeriesRelation
3. `Anime.related_series` FK
4. `User.animeseries_*` preference fields
5. New models: SimklMapping, AnimeEpisode, WatchedAnimeEpisode
6. `AnimeSeriesLink.total_episodes` field

### Existing data

`build_anime_series --dry-run` runs as a check step; actual build runs as a separate deployment step (not via RunPython — too slow for large databases).

Run `backfill_anime_episode_counts` after deploying if AnimeSeriesLink rows already exist without `total_episodes`.

---

## 10. Out of Scope

- Jikan as an episode source (replaced by Simkl)
- AniList as an alternative anime source
- Episode tracking for Extras (main seasons only)
- Bulk episode selection ("Select All", range select)
- "Mark all as watched" shortcut
- User-facing UI for consolidation management (admin only)
- "Rebuild from MAL" button in admin UI
