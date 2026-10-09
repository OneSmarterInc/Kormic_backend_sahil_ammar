from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from universities.models import University
from url_discovery.models import DiscoveryJob, DiscoveredUrl
from url_discovery.crawler import DirectUniversityCrawler
from url_discovery.services import run_auto_apply_and_scrape
from url_discovery.url_filter import hard_filter_url
from knowledge.scraper import page_chunks


class FullSiteTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name='Example', website_url='https://example.edu/')
        self.job = DiscoveryJob.objects.create(university=self.university, base_url=self.university.website_url,
            root_domain='example.edu', settings={'full_site': True, 'max_pages': 1, 'max_depth': 1, 'download_delay': 0})

    def test_full_site_visits_deep_unclassified_pages_beyond_normal_limits(self):
        def response(client, url, policy):
            index = int(url.rsplit('/', 1)[1] or 0)
            link = f'<a href="/{index+1}">Next page</a>' if index < 9 else ''
            return 200, {'content-type': 'text/html'}, f'<html><main>Page {index}{link}</main></html>'.encode(), url
        with patch.object(DirectUniversityCrawler, '_seed', lambda crawler: crawler._discover(
                self.university.website_url, None, '', 0, force_queue=True)), \
             patch.object(DirectUniversityCrawler, '_prepare_robots'), \
             patch('url_discovery.crawler.request_with_policy', side_effect=response):
            DirectUniversityCrawler(self.job.pk).run()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'completed')
        self.assertEqual(self.job.pages_crawled, 10)
        self.assertTrue(self.job.urls.filter(normalized_url='https://example.edu/9', crawled_at__isnull=False).exists())

    def test_full_site_applies_all_successful_pages_even_without_relevance(self):
        for index in range(55):
            DiscoveredUrl.objects.create(job=self.job, original_url=f'https://example.edu/{index}',
                normalized_url=f'https://example.edu/{index}', http_status=200, content_type='text/html', decision_status='excluded')
        with patch('universities.services.scrape_now', return_value={'total_facts_stored': 55}):
            run_auto_apply_and_scrape(self.job)
        self.university.refresh_from_db()
        self.assertEqual(len(self.university.scrape_urls), 55)


class WholeDocumentTests(SimpleTestCase):
    def test_every_part_of_long_document_reaches_extraction(self):
        text = 'Public university content. ' * 900 + 'Final scholarship eligibility criteria.'
        chunks = list(page_chunks(text))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 6000 for chunk in chunks))
        self.assertIn('Final scholarship eligibility criteria.', chunks[-1])
        for position in range(0, len(text), 73):
            self.assertTrue(any(text[position:position+50] in chunk for chunk in chunks))

    def test_public_administration_and_accountancy_are_not_login_paths(self):
        self.assertTrue(hard_filter_url('https://example.edu/administration')[0])
        self.assertTrue(hard_filter_url('https://example.edu/accountancy')[0])
        self.assertFalse(hard_filter_url('https://example.edu/admin/users')[0])
        self.assertFalse(hard_filter_url('https://example.edu/account/login')[0])
