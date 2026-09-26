import os
import time
from concurrent.futures import ThreadPoolExecutor
from django.core.management.base import BaseCommand, CommandError
from university_research.worker import work_once


class Command(BaseCommand):
    help = 'Run concurrent fair LangGraph university research workers (does not start Qwen).'

    def add_arguments(self, parser):
        parser.add_argument('--concurrency', type=int, default=int(os.getenv('UNIVERSITY_RESEARCH_CONCURRENCY', '2')))
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        count = options['concurrency']
        if count < 1 or count > 16:
            raise CommandError('Concurrency must be 1..16')
        if options['once']:
            work_once()
            return
        def loop():
            while True:
                try:
                    if not work_once():
                        time.sleep(2)
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Research worker retrying after database error')
                    time.sleep(3)
        self.stdout.write(f'University research worker started with {count} slots')
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(loop) for _ in range(count)]
            for future in futures:
                future.result()
