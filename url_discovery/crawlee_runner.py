"""Crawlee scheduling with Kormic's existing URL policy, parser and database.

The SQL discovery rows are the durable checkpoint. Crawlee's queue is isolated
per run; resuming rebuilds it from unprocessed rows, avoiding cross-tenant state.
"""
import asyncio
from datetime import timedelta
from urllib.parse import urlsplit, urljoin
from uuid import uuid4

import httpx
from asgiref.sync import async_to_sync, sync_to_async
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import BasicCrawler
from crawlee.storage_clients import MemoryStorageClient
from crawlee.storages import RequestQueue
from django.db import transaction
from django.utils import timezone

from .browser_fetch import needs_rendering, render_html
from .crawler import DirectUniversityCrawler
from .models import DiscoveryJob, DiscoveredUrl
from .safe_fetch import request_with_policy


class CrawleeUniversityCrawler(DirectUniversityCrawler):
    def __init__(self, job_id):
        super().__init__(job_id)
        self.page_limit = max(1, min(int(self.config.get('max_pages', 150)), 2000))
        self.sitemap_limit = max(0, min(int(self.config.get('max_sitemaps', 3)), 20))
        self.candidate_limit = max(self.page_limit, min(int(self.config.get('max_candidates', 5000)), 20000))
        self.sitemaps = set()
        self.submitted = 0
        self.limited = False
        self.browser_enabled = self.config.get('render_javascript', True)
        self.max_response_bytes = max(1, min(int(self.config.get('max_response_mb', 10)), 16)) * 1024 * 1024
        self.fallback_seeded = False

    def _seed(self):
        # Navigation supplies the broad topic entry points. Guessed endpoints
        # and hundreds of sitemap partitions must not starve actual pages.
        self._discover(self.base_url, None, 'University homepage', 0,
                       force_queue=True, priority_queue=True, navigation_level=0)

    def _discover(self, url, parent_url, anchor_text, depth, **kwargs):
        path = urlsplit(url).path.lower()
        if path.endswith(('.xml', '.xml.gz')) or 'sitemap' in path:
            if url not in self.sitemaps and len(self.sitemaps) >= self.sitemap_limit:
                self.limited = True
                return
            self.sitemaps.add(url)
            # Deep sitemap partition trees are not needed for navigation crawl.
            if depth > 1:
                self.limited = True
                return
        if len(self.seen) >= self.candidate_limit and url not in self.seen:
            self.limited = True
            return
        return super()._discover(url, parent_url, anchor_text, depth, **kwargs)

    def _fallback_seed(self, failed_url):
        if failed_url.rstrip('/') != self.base_url.rstrip('/') or self.fallback_seeded:
            return
        self.fallback_seeded = True
        for path in ('/admissions/', '/academics/', '/programs/', '/tuition/', '/sitemap.xml'):
            self._discover(urljoin(self.base_url, path), self.base_url, path.strip('/'), 1, force_queue=True)

    def _enqueue(self, normalized, parent_url, anchor_text, depth, navigation_level, priority):
        # Use breadth/relevance ordering even for full-site mode.
        full = self.full_site
        self.full_site = False
        try:
            super()._enqueue(normalized, parent_url, anchor_text, depth, navigation_level, priority)
        finally:
            self.full_site = full

    def _initialize(self):
        self._mark_running()
        for record in self.job_records():
            self.seen.add(record.normalized_url)
            if record.crawled_at:
                self.fetched.add(record.normalized_url)
            else:
                path = urlsplit(record.normalized_url).path.lower()
                if path.endswith(('.xml', '.xml.gz')) or 'sitemap' in path:
                    if len(self.sitemaps) >= self.sitemap_limit or record.crawl_depth > 1:
                        self.limited = True
                        continue
                    self.sitemaps.add(record.normalized_url)
                self._enqueue(record.normalized_url, record.parent_url, record.anchor_text or '',
                              record.crawl_depth, None, False)
        self._seed()
        self.config['crawler'] = 'crawlee'
        DiscoveryJob.objects.filter(pk=self.job_id).update(settings=self.config, updated_at=timezone.now())

    def job_records(self):
        return DiscoveredUrl.objects.filter(job_id=self.job_id).order_by('crawl_depth', '-relevance_score', 'id')

    def _drain(self):
        requests = []
        # Small batches preserve breadth/relevance ordering as new links arrive.
        while self.queue and len(requests) < 12 and self.submitted < self.page_limit:
            url, parent, anchor, depth, navigation = self.queue.popleft()
            self.queued.discard(url)
            self.submitted += 1
            requests.append(Request.from_url(url, unique_key=url, user_data={
                'parent': parent, 'anchor': anchor, 'depth': depth, 'navigation': navigation}))
        if self.queue and self.submitted >= self.page_limit:
            self.limited = True
        return requests

    def _prepare_origin(self, url):
        origin = urlsplit(url).scheme + '://' + urlsplit(url).netloc
        if origin not in self.robot_parsers:
            with self._client() as client:
                self._prepare_robots(client, origin)
        parser = self.robot_parsers.get(origin)
        return not parser or parser.can_fetch(self.user_agent, url)

    def _client(self):
        return httpx.Client(headers={'User-Agent': self.user_agent},
                            timeout=httpx.Timeout(self.timeout), follow_redirects=False)

    def _fetch(self, url):
        with self._client() as client:
            response = request_with_policy(client, url, self.domain_policy, max_bytes=self.max_response_bytes)
        if response[0] == 429 or response[0] >= 500:
            raise httpx.TransportError(f'Temporary HTTP {response[0]} for {url}')
        return response

    @transaction.atomic
    def _persist(self, request, response, rendered):
        if self._job_status() in ('stopped', 'stop_requested'):
            return
        data = request.user_data
        self._parse_response(request.url, data.get('parent'), data.get('anchor', ''),
                             data.get('depth', 0), data.get('navigation'), response)
        if response[0] < 400 and 'html' in response[1].get('content-type', ''):
            DiscoveredUrl.objects.filter(job_id=self.job_id, normalized_url=request.url).update(
                html_snapshot=response[2].decode('utf-8', errors='replace'), rendered=rendered)
        self.fetched.add(request.url)

    def _complete(self):
        if self._job_status() in ('stopped', 'stop_requested'):
            self._finish('stopped')
            return
        job = DiscoveryJob.objects.get(pk=self.job_id)
        pending = job.urls.filter(crawled_at__isnull=True).count()
        self.config['coverage'] = {'status': 'partial' if self.limited or job.failed_count or pending else 'collected',
            'limit_reached': self.limited, 'page_limit': self.page_limit,
            'pending_urls': pending,
            'review_required': True}
        DiscoveryJob.objects.filter(pk=self.job_id).update(settings=self.config)
        self._finish('completed')

    def run(self):
        if self._job_status() != 'queued':
            return
        try:
            async_to_sync(self._run)()
        except Exception as exc:
            self._finish('failed', str(exc)[:2000])
            raise

    async def _run(self):
        await sync_to_async(self._initialize)()
        browser_slots = asyncio.Semaphore(1)
        concurrency = max(1, min(int(self.config.get('concurrency', 3)), 8))
        storage = MemoryStorageClient()
        queue = await RequestQueue.open(name=f'kormic-{self.job_id}-{uuid4().hex}', storage_client=storage)
        engine = BasicCrawler(storage_client=storage, request_manager=queue, use_session_pool=False,
            configure_logging=False, max_request_retries=2,
            request_handler_timeout=timedelta(seconds=180),
            concurrency_settings=ConcurrencySettings(min_concurrency=1, desired_concurrency=concurrency,
                max_concurrency=concurrency, max_tasks_per_minute=60))

        @engine.router.default_handler
        async def handle(context):
            if await sync_to_async(self._job_status)() in ('stopped', 'stop_requested'):
                engine.stop('Stopped by university administrator')
                return
            if not await sync_to_async(self._prepare_origin)(context.request.url):
                await sync_to_async(self._mark_fetch_failure)(context.request.url, 'Blocked by robots.txt', excluded=False)
            else:
                if self.delay:
                    await asyncio.sleep(self.delay)
                try:
                    response = await asyncio.to_thread(self._fetch, context.request.url)
                    rendered = False
                    if response[0] < 400 and self.browser_enabled and needs_rendering(response[2], response[1].get('content-type', '')):
                        async with browser_slots:
                            response = await render_html(response[3], self.domain_policy, timeout=self.timeout,
                                                         max_bytes=self.max_response_bytes)
                        rendered = True
                    await sync_to_async(self._persist)(context.request, response, rendered)
                    if response[0] >= 400:
                        await sync_to_async(self._fallback_seed)(context.request.url)
                except ValueError as error:
                    # A policy/size refusal is permanent; retrying cannot help.
                    await sync_to_async(self._mark_fetch_failure)(context.request.url, str(error))
                    await sync_to_async(self._fallback_seed)(context.request.url)
            additional = await sync_to_async(self._drain)()
            if additional:
                await context.add_requests(additional)

        @engine.failed_request_handler
        async def failed(context, error):
            if await sync_to_async(self._job_status)() in ('stopped', 'stop_requested'):
                engine.stop('Stopped by university administrator')
                return
            await sync_to_async(self._mark_fetch_failure)(context.request.url, str(error))
            await sync_to_async(self._fallback_seed)(context.request.url)
            additional = await sync_to_async(self._drain)()
            if additional:
                await engine.add_requests(additional)

        await engine.run(await sync_to_async(self._drain)())
        await sync_to_async(self._complete)()
