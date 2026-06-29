"""Management command for building AnimeSeries groupings."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from app.anime_series_builder import BuildReport, build_anime_series

User = get_user_model()


class Command(BaseCommand):
    """Build AnimeSeries groupings from tracked anime via MAL prequel/sequel chains."""

    help = 'Build AnimeSeries groupings for tracked anime'

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show proposed changes without writing to the database',
        )
        parser.add_argument(
            '--user',
            type=int,
            metavar='USER_ID',
            help='Process only this user (by primary key)',
        )

    def handle(self, *args, **options):
        """Execute the command."""
        users = None
        if options['user']:
            try:
                users = [User.objects.get(pk=options['user'])]
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'User {options["user"]} not found.'))
                return

        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be written\n'))

        report = build_anime_series(users=users, dry_run=dry_run)
        self._print_report(report, dry_run)

    def _print_report(self, report: BuildReport, dry_run: bool) -> None:
        """Print the build report to stdout."""
        action = 'Proposed' if dry_run else 'Applied'

        for group in report.groups:
            count = len(group.members)
            if group.is_dubious:
                header = f'[⚠] {group.root_title} ({count} members, {len(group.warnings)} warning(s))'
                self.stdout.write(self.style.WARNING(header))
            else:
                header = f'[✓] {group.root_title} ({count} members)'
                self.stdout.write(self.style.SUCCESS(header))

            for member in group.members:
                extra_tag = ' [extra]' if member.is_extra else ''
                date_tag = f' {member.start_date}' if member.start_date else ''
                self.stdout.write(f'      - {member.title}{date_tag} (id={member.media_id}){extra_tag}')

            for warning in group.warnings:
                self.stdout.write(self.style.WARNING(f'      ⚠  {warning}'))

        for error in report.errors:
            self.stdout.write(self.style.ERROR(f'[!] {error}'))

        if report.already_linked:
            titles = ', '.join(report.already_linked[:5])
            suffix = f' (+{len(report.already_linked) - 5} more)' if len(report.already_linked) > 5 else ''
            self.stdout.write(f'\n[ℹ] Already in a series ({len(report.already_linked)}): {titles}{suffix}')

        if report.standalone:
            titles = ', '.join(report.standalone[:5])
            suffix = f' (+{len(report.standalone) - 5} more)' if len(report.standalone) > 5 else ''
            self.stdout.write(f'[ℹ] Standalone — no series ({len(report.standalone)}): {titles}{suffix}')

        self.stdout.write(
            f'\n{action}: {len(report.groups)} group(s), '
            f'{len(report.errors)} error(s), '
            f'{len(report.standalone)} standalone, '
            f'{len(report.already_linked)} already linked',
        )
