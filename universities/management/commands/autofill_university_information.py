"""Reprocess discovered website pages through the structured source adapter."""
import time
from django.core.management.base import BaseCommand
from universities.models import University
from url_discovery.models import DiscoveredUrl
from knowledge.scraper import fetch_page
from universities.structured_information import ingest_document


class Command(BaseCommand):
    help = 'Autofill fixed information forms from university website pages, preserving manual edits.'

    def add_arguments(self, parser):
        parser.add_argument('university_uuid')
        parser.add_argument('--watch', action='store_true', help='Also process pages discovered by the running full-site crawl.')

    def handle(self, *args, **options):
        university = University.objects.get(uuid=options['university_uuid'])
        seen = set()
        while True:
            urls = list(university.scrape_urls or [])
            urls += [row.final_url or row.normalized_url for row in DiscoveredUrl.objects.filter(
                job__university=university, http_status__gte=200, http_status__lt=300).exclude(content_type__icontains='xml')]
            pending = sorted(set(urls) - seen, key=lambda url: (0 if 'scholarship' in url else 1 if '/degrees-and-programs/profile/' in url else 2 if '/housing/' in url else 3, url))
            for url in pending:
                try:
                    saved = []
                    fetch_page(url, on_document=lambda document, source: saved.append(ingest_document(university.uuid, document, source)))
                    self.stdout.write(f'{url}: {sum(saved)} structured records')
                    seen.add(url)
                except Exception as exc:
                    self.stderr.write(f'{url}: {exc}')
                    seen.add(url)
            if not options['watch'] or not university.discovery_jobs.filter(status__in=['queued', 'running']).exists():
                break
            time.sleep(30)
