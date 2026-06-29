import contextlib

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered
from django.contrib.auth import get_user_model
from django.shortcuts import render
from django.urls import path

from app.anime_series_builder import build_anime_series
from app.models import (
    Anime,
    AnimeEpisode,
    AnimeSeriesLink,
    AnimeSeriesRelation,
    Episode,
    Item,
    SimklMapping,
    UserMessage,
    WatchedAnimeEpisode,
)

User = get_user_model()


class AnimeSeriesLinkInline(admin.TabularInline):
    """Inline editor for anime seasons/extras within an AnimeSeries Item."""

    model = AnimeSeriesLink
    fk_name = "series_item"
    fields = ["order", "anime_item", "is_extra"]
    extra = 0
    autocomplete_fields = ["anime_item"]
    ordering = ["order", "anime_item__title"]


# Custom ModelAdmin classes with search functionality
@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    """Custom admin for Item model with search and filter options."""

    search_fields = ["title", "media_id", "source"]
    list_display = [
        "title",
        "media_id",
        "season_number",
        "episode_number",
        "media_type",
        "source",
    ]
    list_filter = ["media_type", "source"]
    inlines_map = {"animeseries": [AnimeSeriesLinkInline]}

    def get_inlines(self, request, obj=None):
        """Show AnimeSeriesLink inline only for animeseries-type Items."""
        if obj and obj.media_type == "animeseries":
            return [AnimeSeriesLinkInline]
        return []


@admin.register(Episode)
class EpisodeAdmin(admin.ModelAdmin):
    """Custom admin for Episode model with search and filter options."""

    search_fields = ["item__title", "related_season__item__title"]
    list_display = ["__str__", "end_date"]


@admin.register(UserMessage)
class UserMessageAdmin(admin.ModelAdmin):
    """Custom admin for persistent user messages."""

    search_fields = ["user__username", "message"]
    list_display = ["message", "level", "user", "created_at", "shown_at"]
    list_filter = ["level", "shown_at"]


class MediaAdmin(admin.ModelAdmin):
    """Custom admin for regular media model with search and filter options."""

    search_fields = ["item__title", "user__username", "notes"]
    list_display = ["__str__", "status", "score", "user"]
    list_filter = ["status"]


@admin.register(AnimeSeriesLink)
class AnimeSeriesLinkAdmin(admin.ModelAdmin):
    """Admin for global anime–series mappings."""

    search_fields = ["anime_item__title", "series_item__title"]
    list_display = ["anime_item", "series_item", "order", "is_extra"]
    list_filter = ["is_extra"]
    autocomplete_fields = ["anime_item", "series_item"]
    actions = ["move_to_extras", "move_to_seasons", "remove_from_series", "move_to_related"]

    @admin.action(description="Mark selected as Extras")
    def move_to_extras(self, request, queryset):
        """Set is_extra=True for selected links."""
        updated = queryset.update(is_extra=True)
        self.message_user(request, f"Marked {updated} link(s) as extras.")

    @admin.action(description="Mark selected as Seasons")
    def move_to_seasons(self, request, queryset):
        """Set is_extra=False for selected links."""
        updated = queryset.update(is_extra=False)
        self.message_user(request, f"Marked {updated} link(s) as seasons.")

    @admin.action(description="Remove selected from their series")
    def remove_from_series(self, request, queryset):
        """Delete selected AnimeSeriesLink records."""
        count = queryset.count()
        queryset.delete()
        self.message_user(request, f"Removed {count} link(s) from their series.")

    @admin.action(description="Move selected to a new Related series")
    def move_to_related(self, request, queryset):
        """Detach selected anime from their series and link as an alternative/adaptation.

        For each selected link: removes the anime from the current series, builds a
        new AnimeSeries for it (depth-1 BFS), and creates a bidirectional
        AnimeSeriesRelation with relation_type='alternative_version'.
        Edit the relation_type in AnimeSeriesRelation admin afterwards if needed.
        """
        from app.anime_series_builder import _find_or_build_series_for_anime  # noqa: PLC0415

        created, skipped = 0, 0
        for link in queryset.select_related("series_item", "anime_item"):
            original_series_item = link.series_item
            anime_item = link.anime_item
            source = anime_item.source

            # Remove link first so _find_or_build_series_for_anime creates a fresh series
            link.delete()

            new_series_item = _find_or_build_series_for_anime(
                str(anime_item.media_id), source,
            )
            if new_series_item and new_series_item.pk != original_series_item.pk:
                AnimeSeriesRelation.objects.get_or_create(
                    from_series=original_series_item,
                    to_series=new_series_item,
                    defaults={"relation_type": "alternative_version"},
                )
                AnimeSeriesRelation.objects.get_or_create(
                    from_series=new_series_item,
                    to_series=original_series_item,
                    defaults={"relation_type": "alternative_version"},
                )
                created += 1
            else:
                skipped += 1

        if created:
            self.message_user(
                request,
                f"Moved {created} anime to Related series (relation_type=alternative_version). "
                "Edit AnimeSeriesRelation if a different type is needed.",
            )
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} link(s) — could not build a separate series.",
                level="WARNING",
            )


@admin.register(AnimeSeriesRelation)
class AnimeSeriesRelationAdmin(admin.ModelAdmin):
    """Admin for depth-1 relations between AnimeSeries."""

    search_fields = ["from_series__title", "to_series__title"]
    list_display = ["from_series", "to_series", "relation_type"]
    list_filter = ["relation_type"]
    autocomplete_fields = ["from_series", "to_series"]


@admin.register(Anime)
class AnimeAdmin(MediaAdmin):
    """Admin for Anime with a series builder tool."""

    list_display = ["__str__", "status", "score", "user", "related_series"]

    def get_urls(self):
        """Append the build-series tool URL."""
        urls = super().get_urls()
        extra = [
            path(
                'build-series/',
                self.admin_site.admin_view(self.build_series_view),
                name='app_anime_build_series',
            ),
        ]
        return extra + urls

    def build_series_view(self, request):
        """Render the anime series builder tool page."""
        report = None
        dry_run = True
        selected_user_id = request.POST.get('user_id', '')

        if request.method == 'POST':
            action = request.POST.get('action', 'dry_run')
            dry_run = action == 'dry_run'

            users = None
            if selected_user_id:
                try:
                    users = [User.objects.get(pk=selected_user_id)]
                except User.DoesNotExist:
                    users = None

            report = build_anime_series(users=users, dry_run=dry_run)

        context = {
            **self.admin_site.each_context(request),
            'report': report,
            'dry_run': dry_run,
            'all_users': User.objects.order_by('username'),
            'selected_user_id': selected_user_id,
            'title': 'Build Anime Series',
        }
        return render(request, 'admin/app/anime/build_series.html', context)


@admin.register(SimklMapping)
class SimklMappingAdmin(admin.ModelAdmin):
    """Admin for MAL → Simkl ID mappings."""

    search_fields = ['item__title', 'simkl_id']
    list_display = ['item', 'simkl_id', 'cached_at']
    autocomplete_fields = ['item']


@admin.register(AnimeEpisode)
class AnimeEpisodeAdmin(admin.ModelAdmin):
    """Admin for global anime episode metadata."""

    search_fields = ['anime_item__title', 'title']
    list_display = ['anime_item', 'episode_number', 'title', 'aired', 'is_special']
    list_filter = ['is_special']
    autocomplete_fields = ['anime_item']


@admin.register(WatchedAnimeEpisode)
class WatchedAnimeEpisodeAdmin(admin.ModelAdmin):
    """Admin for per-user watched episode records."""

    search_fields = ['user__username', 'episode__title', 'anime__item__title']
    list_display = ['user', 'anime', 'episode', 'watched_at']
    list_filter = ['watched_at']


# Auto-register remaining models
app_models = apps.get_app_config("app").get_models()
SpecialModels = [
    "Item", "Episode", "BasicMedia", "UserMessage",
    "AnimeSeriesLink", "AnimeSeriesRelation", "Anime",
    "SimklMapping", "AnimeEpisode", "WatchedAnimeEpisode",
]
for model in app_models:
    if (
        not model.__name__.startswith("Historical")
        and model.__name__ not in SpecialModels
    ):
        with contextlib.suppress(AlreadyRegistered):
            admin.site.register(model, MediaAdmin)
