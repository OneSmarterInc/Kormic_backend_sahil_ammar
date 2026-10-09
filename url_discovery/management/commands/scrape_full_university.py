"""Run full-site discovery and the existing fact pipeline with persistent progress."""
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from universities.models import University
from universities.services import scrape_selected_urls
from url_discovery.crawler import DirectUniversityCrawler
from url_discovery.models import DiscoveryJob
from url_discovery.services import apply_selected_urls, start_discovery


class Command(BaseCommand):
    help = 'Crawl every reachable public university page and extract through the normal knowledge pipeline.'

    def add_arguments(self, parser):
        parser.add_argument('university_uuid')
        parser.add_argument('--resume', type=int, help='Resume an interrupted full-site job, retaining completed pages.')
        parser.add_argument('--reextract', action='store_true', help='Reprocess discovered pages after an extractor fix; keep manual edits.')

    def handle(self, *args, **options):
        university = University.objects.get(uuid=options['university_uuid'])
        if options['resume']:
            job = DiscoveryJob.objects.get(pk=options['resume'], university=university)
            if not job.settings.get('full_site'):
                raise CommandError('Only a full-site job can be resumed.')
            if job.status in DiscoveryJob.ACTIVE_STATUSES and (timezone.now() - job.updated_at).total_seconds() < 600:
                raise CommandError('This discovery job still has a recent worker heartbeat.')
            if job.status != 'completed':
                job.status, job.error_message = 'queued', ''
                job.save(update_fields=['status', 'error_message', 'updated_at'])
        else:
            job = start_discovery(university, full_site=True, auto_apply=False, dispatch=False)
        self.stdout.write(f'Full-site job {job.pk} for {university.name}')

        def crawl():
            try:
                DirectUniversityCrawler(job.pk).run()
            finally:
                close_old_connections()

        thread = threading.Thread(target=crawl, name=f'full-site-{job.pk}', daemon=True)
        if job.status != 'completed':
            thread.start()
        progress = dict(job.scrape_result or {})
        results = [] if options['reextract'] else list(progress.get('results', []))
        attempted = {item['url'] for item in results}
        try:
            while True:
                close_old_connections()
                job.refresh_from_db()
                if job.status in {'stopped', 'stop_requested'}:
                    break
                records = job.urls.filter(http_status__gte=200, http_status__lt=300,
                                          crawled_at__isnull=False).exclude(content_type__icontains='xml')
                pending = list(dict.fromkeys((r.final_url or r.normalized_url) for r in records
                    if (r.final_url or r.normalized_url) not in attempted))
                if pending:
                    batch = pending[:10]
                    apply_selected_urls(university, job, batch)
                    for url in batch:
                        if DiscoveryJob.objects.filter(pk=job.pk, status__in=['stopped', 'stop_requested']).exists():
                            break
                        result = scrape_selected_urls(university, [url], force_refresh=True)
                        results.extend(result['results'])
                        attempted.add(url)
                        progress = {'mode': 'full_site', 'status': 'extracting', 'pages_processed': len(attempted),
                            'total_facts_stored': sum(r.get('facts_stored', 0) for r in results), 'results': results}
                        DiscoveryJob.objects.filter(pk=job.pk).update(scrape_result=progress)
                        self.stdout.write(f"Extracted {len(attempted)} pages; {progress['total_facts_stored']} facts")
                    continue
                if not thread.is_alive():
                    break
                time.sleep(3)
            job.refresh_from_db()
            progress.update(mode='full_site', status='completed' if job.status == 'completed' else job.status,
                            pages_processed=len(attempted), results=results)
            DiscoveryJob.objects.filter(pk=job.pk).update(scrape_result=progress)
            self.stdout.write(f"Discovery {job.status}: {job.pages_crawled} pages crawled; {len(attempted)} extracted; {job.failed_count} fetch failures")
        except KeyboardInterrupt:
            self.stdout.write(f'Progress saved. Resume with --resume {job.pk} after the worker heartbeat expires.')
