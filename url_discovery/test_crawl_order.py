from unittest.mock import patch
from django.test import TestCase
from universities.models import University
from url_discovery.models import DiscoveryJob
from url_discovery.crawler import DirectUniversityCrawler


class CrawlOrderTests(TestCase):
    def setUp(self):
        university = University.objects.create(name='Crawl Order University', country='US')
        job = DiscoveryJob.objects.create(university=university, base_url='https://example.edu/', root_domain='example.edu')
        self.crawler = DirectUniversityCrawler(job.pk)

    @patch('url_discovery.crawler.crawl_priority', return_value=100)
    def test_deep_navigation_does_not_starve_shallow_sections_and_ties_stay_fifo(self, priority):
        for url, depth in [('admissions', 1), ('scholarships', 1), ('safety/news/archive', 4), ('tuition', 1)]:
            self.crawler._enqueue('https://example.edu/' + url, None, url, depth, depth, True)
        self.assertEqual([item[0].split('.edu/')[1] for item in self.crawler.queue],
                         ['admissions', 'scholarships', 'tuition', 'safety/news/archive'])

    @patch('url_discovery.crawler.crawl_priority', side_effect=[10, 100])
    def test_relevance_orders_same_depth_without_duplicates(self, priority):
        self.crawler._enqueue('https://example.edu/news', None, 'News', 1, 1, True)
        self.crawler._enqueue('https://example.edu/fees', None, 'Fees', 1, 1, True)
        self.crawler._enqueue('https://example.edu/fees', None, 'Fees', 1, 1, True)
        self.assertEqual([item[0] for item in self.crawler.queue], ['https://example.edu/fees', 'https://example.edu/news'])
