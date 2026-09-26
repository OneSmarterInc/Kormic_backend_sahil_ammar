"""Concurrent shared-queue workers, each advancing one fair agent slice."""
import time
from concurrent.futures import ThreadPoolExecutor
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connections, OperationalError
from github_profiles.scheduling import claim, recover_legacy_runs
from github_profiles.runner import execute_slice


def work_once():
    close_old_connections()
    try:
        run = claim()
        if run:
            execute_slice(run)
        return bool(run)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = 'Run concurrent GitHub LangGraph agents with shared leases and model limits.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Advance at most one queued agent slice and exit.')
        parser.add_argument('--concurrency', type=int, default=settings.GITHUB_WORKER_CONCURRENCY)

    def handle(self, *args, **options):
        concurrency = options['concurrency']
        if not 1 <= concurrency <= 32:
            raise CommandError('Concurrency must be between 1 and 32.')
        recover_legacy_runs()
        if options['once']:
            work_once()
            return
        self.stdout.write(f'GitHub LangGraph worker ready: {concurrency} concurrent slices. Shared queue and model limits enabled.')
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='github-agent') as pool:
            futures = set()
            try:
                while True:
                    finished = {f for f in futures if f.done()}
                    for future in finished:
                        try:
                            future.result()
                        except OperationalError:
                            self.stderr.write('Database temporarily busy; leases preserve unfinished work.')
                        except Exception as exc:
                            self.stderr.write(f'Worker slice failed ({type(exc).__name__}); unfinished leases will be recovered.')
                    futures -= finished
                    while len(futures) < concurrency:
                        futures.add(pool.submit(work_once))
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.stdout.write('Stopping after active slices finish; queued work remains saved.')
