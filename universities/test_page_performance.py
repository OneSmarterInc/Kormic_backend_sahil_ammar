from django.test import TestCase, override_settings
from django_api.tests import make_university_client
from django_api.models import UniversityKnowledgeEntry


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class KnowledgePaginationTests(TestCase):
    def setUp(self):
        self.client, self.uid = make_university_client()
        UniversityKnowledgeEntry.objects.filter(university_id=self.uid).delete()
        UniversityKnowledgeEntry.objects.bulk_create([
            UniversityKnowledgeEntry(university_id=self.uid, topic=f'Program {i}', content=f'Description {i}',
                source_type='manual', details={'information_type': 'academics'}) for i in range(55)
        ])
        UniversityKnowledgeEntry.objects.bulk_create([
            UniversityKnowledgeEntry(university_id='another-university', topic='Private program', content='Secret')
        ])

    def test_database_pagination_search_and_tenant_isolation(self):
        path = '/api/university-admin/knowledge/'
        first = self.client.get(path, {'page': 1}).data
        second = self.client.get(path, {'page': 2}).data
        self.assertEqual(first['count'], 55)
        self.assertEqual(len(first['knowledge']), 50)
        self.assertEqual(len(second['knowledge']), 5)
        self.assertFalse({r['id'] for r in first['knowledge']} & {r['id'] for r in second['knowledge']})
        self.assertEqual(self.client.get(path, {'page': 1, 'search': 'Private'}).data['count'], 0)
        self.assertEqual(self.client.get(path, {'page': 1, 'search': 'Description 54'}).data['count'], 1)
        self.assertEqual(self.client.get(path, {'page': 0}).status_code, 400)
        self.assertEqual(len(self.client.get(path).data['knowledge']), 55)

    def test_source_information_pagination_keeps_search_and_counts(self):
        response = self.client.get('/api/university-admin/information/', {'page': 1, 'category_group': 'categories'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 55)
        self.assertEqual(len(response.data['knowledge']), 50)
        response = self.client.get('/api/university-admin/information/', {'page': 1, 'search': 'Description 54'})
        self.assertEqual(response.data['count'], 1)
