"""Repair existing extraction artifacts without overwriting human edits."""
from django.core.management.base import BaseCommand
from django.db import transaction
from django_api.models import UniversityKnowledgeEntry
from universities.structured_information import is_program_navigation, merge_source_values


class Command(BaseCommand):
    help = 'Preview or apply removal of scraped navigation records and repair field provenance.'

    def add_arguments(self, parser):
        parser.add_argument('--university', help='Limit to one university UUID.')
        parser.add_argument('--apply', action='store_true', help='Apply changes; default is a dry run.')

    def handle(self, *args, **options):
        rows = UniversityKnowledgeEntry.objects.filter(source_type='scraped', details__has_key='_information_entity_key')
        if options['university']:
            rows = rows.filter(university_id=options['university'])
        removed = repaired = 0
        for pk in rows.values_list('pk', flat=True).iterator():
            with transaction.atomic():
                row = UniversityKnowledgeEntry.objects.select_for_update().filter(pk=pk, source_type='scraped').first()
                if row is None:
                    continue
                details = dict(row.details or {})
                kind = details.get('information_type')
                if kind == 'academics' and is_program_navigation(details.get('name', row.topic)):
                    removed += 1
                    if options['apply']:
                        row.delete()
                    continue
                snapshots = details.get('_information_source_values')
                if not snapshots:
                    continue
                values, sources, conflicts = merge_source_values(kind, snapshots,
                    details.get('_information_source_evidence'), details.get('_information_preferred_source'))
                updated_details = {**details, **values, '_information_field_sources': sources, '_information_conflicts': conflicts}
                updated_content = '\n'.join(f"{field.replace('_', ' ').title()}: {value}" for field, value in values.items() if value and field != 'information_type')
                if row.details == updated_details and row.content == updated_content:
                    continue
                repaired += 1
                if options['apply']:
                    row.details = updated_details
                    row.content = updated_content
                    row.save()
        self.stdout.write(f"{'Applied' if options['apply'] else 'Dry run'}: {removed} navigation records removed; {repaired} records reconciled. Human edits preserved.")
