from unittest.mock import patch, AsyncMock
from django.test import TestCase
from universities.models import University
from url_discovery.models import DiscoveryJob, DiscoveredUrl
from url_discovery.crawlee_runner import CrawleeUniversityCrawler
from knowledge.scraper import fetch_page


def document(url, links=''):
    return (200, {'content-type': 'text/html'},
            ('<html><head><title>Admissions</title></head><main>' +
             'Official admission requirements and qualifications. ' * 8 + links + '</main></html>').encode(), url)


class CrawleeRunnerTests(TestCase):
    def setUp(self):
        self.uni = University.objects.create(name='Example', website_url='https://example.edu/')
        self.job = DiscoveryJob.objects.create(university=self.uni, base_url=self.uni.website_url,
            root_domain='example.edu', settings={'max_pages': 3, 'max_sitemaps': 1,
                'download_delay': 0, 'respect_robots_txt': False, 'concurrency': 2})

    def run_crawl(self, response):
        with patch.object(CrawleeUniversityCrawler, '_fetch', side_effect=response):
            CrawleeUniversityCrawler(self.job.pk).run()
        self.job.refresh_from_db()

    def test_real_crawlee_scheduler_limits_and_deduplicates_pages(self):
        def response(url):
            return document(url, ''.join(f'<a href="/admissions/{i}">Admissions {i}</a>' for i in range(8)))
        self.run_crawl(response)
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.pages_crawled, 3)
        self.assertEqual(self.job.settings['coverage']['status'], 'partial')
        self.assertTrue(self.job.settings['coverage']['limit_reached'])
        self.assertTrue(self.job.urls.exclude(html_snapshot='').exists())

    def test_rendered_document_is_persisted_for_extraction(self):
        rendered = document('https://example.edu/', '<h2>Required documents</h2><p>Official transcript.</p>')
        with patch('url_discovery.crawlee_runner.render_html', new=AsyncMock(return_value=rendered)) as render:
            self.run_crawl(lambda url: (200, {'content-type': 'text/html'}, b'<html><script src="app.js"></script><main></main></html>', url))
        render.assert_awaited_once()
        record = self.job.urls.get(rendered=True)
        with patch('knowledge.scraper.request_with_policy', side_effect=AssertionError('Must reuse rendered snapshot')):
            text = fetch_page(record.normalized_url, snapshot=(200, {'content-type': 'text/html'}, record.html_snapshot.encode(), record.final_url))
        self.assertIn('Official transcript.', text)

    def test_cancelled_job_does_not_fetch_or_restart(self):
        self.job.status = 'stopped'
        self.job.save()
        with patch.object(CrawleeUniversityCrawler, '_fetch') as fetch:
            CrawleeUniversityCrawler(self.job.pk).run()
        fetch.assert_not_called()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'stopped')

    def test_separate_jobs_fetch_same_url_independently(self):
        self.run_crawl(lambda url: document(url))
        second = DiscoveryJob.objects.create(university=self.uni, base_url=self.uni.website_url,
            root_domain='example.edu', settings=self.job.settings)
        with patch.object(CrawleeUniversityCrawler, '_fetch', side_effect=lambda url: document(url)) as fetch:
            CrawleeUniversityCrawler(second.pk).run()
        fetch.assert_called_once_with(self.uni.website_url)
        second.refresh_from_db()
        self.assertEqual(second.pages_crawled, 1)

    def test_extraction_reuses_only_current_university_snapshot(self):
        from knowledge.scraper import scrape_university
        from unittest.mock import Mock
        self.run_crawl(lambda url: document(url))
        with patch('knowledge.scraper.request_with_policy', side_effect=AssertionError('Unexpected network')), \
             patch('knowledge.scraper._ingest_form_entities') as ingest, \
             patch('knowledge.scraper.extract_facts_from_page', return_value=[]), \
             patch('knowledge.scraper.time.sleep'):
            scrape_university(str(self.uni.uuid), [self.uni.website_url], self.uni.name, Mock())
        self.assertIn('Official admission requirements', str(ingest.call_args.args[1]))

    def test_exhausted_retries_are_reported_as_partial_coverage(self):
        import httpx
        with patch.object(CrawleeUniversityCrawler, '_fallback_seed'), \
             patch.object(CrawleeUniversityCrawler, '_fetch', side_effect=httpx.TransportError('Unavailable')) as fetch:
            CrawleeUniversityCrawler(self.job.pk).run()
        self.job.refresh_from_db()
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.job.failed_count, 1)
        self.assertEqual(self.job.settings['coverage']['status'], 'partial')

    def test_oversized_homepage_uses_bounded_fallback_seeds_without_retry(self):
        seen = []
        def response(url):
            seen.append(url)
            if url == self.uni.website_url:
                raise ValueError('Response exceeds byte limit')
            return document(url)
        self.run_crawl(response)
        self.assertEqual(seen.count(self.uni.website_url), 1)
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.job.pages_crawled, 2)
        self.assertEqual(self.job.failed_count, 1)

    def test_sitemap_partitions_are_bounded(self):
        crawler = CrawleeUniversityCrawler(self.job.pk)
        for index in range(100):
            crawler._discover(f'https://example.edu/sitemap.xml?page={index}', self.uni.website_url, 'Sitemap', 0, force_queue=True)
        self.assertEqual(self.job.urls.count(), 1)
        self.assertTrue(crawler.limited)

    def test_transient_failure_retries_without_duplicate_rows(self):
        import httpx
        responses = [httpx.TransportError('Temporary outage'), document('https://example.edu/')]
        with patch.object(CrawleeUniversityCrawler, '_fetch', side_effect=responses) as fetch:
            CrawleeUniversityCrawler(self.job.pk).run()
        self.job.refresh_from_db()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.job.pages_crawled, 1)
        self.assertEqual(self.job.failed_count, 0)

    def test_resume_reuses_saved_discovery_without_refetching_completed_page(self):
        from django.utils import timezone
        DiscoveredUrl.objects.create(job=self.job, original_url=self.uni.website_url,
            normalized_url=self.uni.website_url, crawled_at=timezone.now(), http_status=200)
        DiscoveredUrl.objects.create(job=self.job, original_url='https://example.edu/admissions',
            normalized_url='https://example.edu/admissions', crawl_depth=1)
        seen = []
        def response(url):
            seen.append(url)
            return document(url)
        self.run_crawl(response)
        self.assertEqual(seen, ['https://example.edu/admissions'])
