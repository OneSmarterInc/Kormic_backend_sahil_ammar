from django.test import TestCase, override_settings
from django_api.tests import make_university_client
from django_api.models import UniversityKnowledgeEntry
from universities.information_costs import place_costs


def record(pk, name, kind, **details):
    return {'id': pk, 'topic': name, 'details': {'information_type': kind, 'name': name, **details}}


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class InformationCostTests(TestCase):
    def test_placement_keeps_costs_with_unique_owner_and_does_not_guess_ambiguous_tuition(self):
        rows = place_costs([
            record(1, 'Computer Science, MS', 'academics'),
            record(2, 'Computer Science, BS', 'academics'),
            record(3, 'The Woods', 'housing'),
            record(4, 'Graduate Fees — Computer Science, MS — Nonresident', 'fees', amount='$900'),
            record(5, 'Housing Costs — Woods Double — Fall semester', 'fees', amount='$2000'),
            record(6, 'Tuition — Computer Science — Resident', 'fees', amount='$800'),
            record(7, 'Additional Charges — Application Fee — All applicants', 'fees', amount='$40'),
        ])
        self.assertEqual(rows[3]['cost_placement'], {'scope': 'academics', 'target_id': 1})
        self.assertEqual(rows[4]['cost_placement'], {'scope': 'housing', 'target_id': 3})
        self.assertEqual(rows[5]['cost_placement']['scope'], 'review')
        self.assertEqual(rows[6]['cost_placement']['scope'], 'university')
        self.assertEqual(rows[3]['details']['amount'], '$900')

    def test_assignment_persists_updates_knowledge_and_rejects_foreign_targets(self):
        client, uid = make_university_client()
        target = UniversityKnowledgeEntry.objects.create(university_id=uid, topic='Computing, MS', content='Course', source_type='human_verified', details={'information_type': 'academics', 'name': 'Computing, MS'})
        payload = {'topic': 'International tuition', 'content': 'Tuition $500 per credit', 'category': 'fees',
                   'details': {'name': 'International tuition', 'information_type': 'fees', 'amount': '$500', 'billing_period': 'Per credit', 'academic_year': '2026–27', 'cost_owner': f'academics:{target.pk}'}}
        created = client.post('/api/university-admin/information/entities/', payload, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        fee = UniversityKnowledgeEntry.objects.get(pk=created.data['id'])
        self.assertIn('Computing, MS', fee.content)
        rows = client.get('/api/university-admin/information/entities/').data['knowledge']
        self.assertEqual(next(row for row in rows if row['id'] == fee.pk)['cost_placement']['target_id'], target.pk)
        foreign = UniversityKnowledgeEntry.objects.create(university_id='another-university', topic='Elsewhere', content='Course', details={'information_type': 'academics'})
        response = client.patch(f'/api/university-admin/knowledge/{fee.pk}/', {'expected_revision': created.data['revision'],
            'details': {**created.data['details'], 'cost_owner': f'academics:{foreign.pk}'}}, format='json')
        self.assertEqual(response.status_code, 400)
        fee.refresh_from_db()
        self.assertEqual(fee.details['cost_owner'], f'academics:{target.pk}')

    def test_explicit_review_overrides_automatic_matching(self):
        rows = place_costs([record(1, 'The Woods', 'housing'), record(2, 'Woods Double', 'fees', cost_owner='review')])
        self.assertEqual(rows[1]['cost_placement']['scope'], 'review')

    def test_school_wide_rate_does_not_match_one_degree_by_substring(self):
        rows = place_costs([record(1, 'Computer Science, MS', 'academics'), record(2,
            'Engineering and Computer Science M.S. Programs — Tuition — Per semester', 'fees')])
        self.assertEqual(rows[1]['cost_placement']['scope'], 'review')
