from bs4 import BeautifulSoup
from django.test import TestCase, override_settings
from django_api.tests import make_university_client
from django_api.models import UniversityKnowledgeEntry
from universities.structured_information import extract_document, save_entities


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class StructuredInformationTests(TestCase):
    def test_program_navigation_is_not_a_program(self):
        rows = self.extract('<a href="/degrees/bachelors/nursing-rn-bsn">See the RN to BSN program page &gt;</a>', 'degrees')
        self.assertEqual(rows, [])

    def test_program_qualifications_are_preserved(self):
        rows = self.extract('<a href="/degrees/bachelors/nursing">Nursing (RN-BSN)</a><a href="/degrees/masters/business">Business Administration (MBA)</a>', 'degrees')
        self.assertEqual([r['name'] for r in rows], ['Nursing (RN-BSN)', 'Business Administration (MBA)'])
        self.assertNotIn('program_code', rows[0]['values'])

    def test_detail_page_title_replaces_directory_wording_at_same_destination(self):
        links = self.extract('<a href="/degrees/bachelors/nursing-rn-bsn#overview">Nursing, BSN</a>', 'degrees')
        save_entities(self.uid, links)
        detail = self.extract('<h1>B.S. in Nursing (RN-BSN)</h1>', 'degrees/bachelors/nursing-rn-bsn')
        save_entities(self.uid, detail)
        save_entities(self.uid, links)
        rows = UniversityKnowledgeEntry.objects.filter(university_id=self.uid)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().details['name'], 'B.S. in Nursing (RN-BSN)')
        self.assertEqual(rows.get().details['level'], 'Undergraduate')

    def test_conflicting_values_keep_their_actual_evidence(self):
        def entity(url, level):
            return {'kind':'academics', 'name':'Nursing', 'source_url':url,
                    'values':{'name':'Nursing','level':level}, 'evidence':{'level':level}}
        save_entities(self.uid, [entity('https://example.edu/first', 'Undergraduate')])
        save_entities(self.uid, [entity('https://example.edu/second', 'Graduate')])
        details = UniversityKnowledgeEntry.objects.get(university_id=self.uid).details
        self.assertEqual(details['level'], 'Undergraduate')
        self.assertEqual(details['_information_field_sources']['level']['quote'], 'Undergraduate')
        self.assertEqual(details['_information_conflicts']['level'][0]['value'], 'Graduate')

    def test_shared_catalog_page_does_not_merge_distinct_programs(self):
        records = self.extract('<a href="/catalog/programs">B.S. Accounting</a><a href="/catalog/programs">B.S. Nursing</a>', 'degrees')
        save_entities(self.uid, records)
        save_entities(self.uid, records)
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(university_id=self.uid).count(), 2)

    def test_merged_fields_keep_all_contributing_sources(self):
        for suffix in ('first-year', 'transfer'):
            save_entities(self.uid, self.extract(f'<h2>Alpha Scholarship</h2><p>Applicants must be {suffix} students.</p>', suffix))
        details = UniversityKnowledgeEntry.objects.get(university_id=self.uid).details
        self.assertEqual(len(details['_information_field_sources']['eligibility']['additional_sources']), 1)

    def test_existing_data_repair_is_dry_run_by_default_and_preserves_manual_edits(self):
        from django.core.management import call_command
        from io import StringIO
        bad = UniversityKnowledgeEntry.objects.create(university_id=self.uid,
            topic='See the RN to BSN program page >', source_type='scraped', content='navigation',
            details={'information_type':'academics','name':'See the RN to BSN program page >','_information_entity_key':'legacy'})
        human = UniversityKnowledgeEntry.objects.create(university_id=self.uid,
            topic='Human wording', source_type='manual', content='Preserve me',
            details={'information_type':'academics','name':'View my degree','_information_entity_key':'human'})
        call_command('repair_information_entities', university=self.uid, stdout=StringIO())
        self.assertTrue(UniversityKnowledgeEntry.objects.filter(pk=bad.pk).exists())
        call_command('repair_information_entities', university=self.uid, apply=True, stdout=StringIO())
        self.assertFalse(UniversityKnowledgeEntry.objects.filter(pk=bad.pk).exists())
        human.refresh_from_db()
        self.assertEqual(human.content, 'Preserve me')
        call_command('repair_information_entities', university=self.uid, apply=True, stdout=StringIO())
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(university_id=self.uid).count(), 1)

    def setUp(self):
        self.client, self.uid = make_university_client()

    def extract(self, html, path='scholarships'):
        return extract_document(BeautifulSoup(html, 'html.parser'), f'https://example.edu/{path}')

    def test_awards_keep_separate_eligibility_and_renewal(self):
        records = self.extract('''<h1>Scholarships</h1><h2>Alpha Scholarship</h2>
          <p>$6,000 per academic year for 8 semesters.</p><p>Applicants must be first-year students from Vietnam.</p>
          <h3>Eligibility</h3><p>A GPA of 3.0 is required at admission.</p>
          <h3>Renewal</h3><p>Maintain a GPA of 2.5 to renew.</p>
          <h2>Beta Scholarship</h2><p>$2,000 per year for 4 semesters.</p>
          <p>Applicants must be transfer students.</p><h2>Scholarship Eligibility</h2><p>General notes.</p>''')
        self.assertEqual(len(records), 2)
        a, b = records
        self.assertEqual(a['values']['duration'], '8 semesters')
        self.assertEqual(a['values']['minimum_gpa'], '3.0')
        self.assertNotIn('transfer', a['values']['eligibility'])
        self.assertNotIn('Vietnam', b['values']['eligibility'])
        self.assertIn('2.5', a['values']['renewal_criteria'])
        self.assertEqual(b['values']['amount'], '$2,000 per year for 4 semesters.')

    def test_generic_scholarship_subsections_are_not_awards(self):
        records = self.extract('<h2>Scholarship Details:</h2><p>Applicants must meet requirements.</p><h2>Scholarship Notification</h2><p>Check email.</p>')
        self.assertEqual(records, [])

    def test_courses_use_named_qualifications_not_generic_headings(self):
        records = self.extract('''<h1>Computer Science</h1><h2>Courses</h2><h3>Undergraduate</h3>
            <a href="https://catalog.example.edu/preview_program.php?id=1">Computer Science, BSCS (CS-BS)</a>
            <h3>Graduate</h3><a href="https://catalog.example.edu/preview_program.php?id=2">Computer Science, MS (CS-MS)</a>''', 'degrees-and-programs/profile/computer-science')
        self.assertEqual([r['name'] for r in records], ['Computer Science, BSCS', 'Computer Science, MS'])
        self.assertEqual(records[1]['values']['level'], 'Graduate')
        self.assertNotIn('duration', records[0]['values'])

    def test_program_offerings_can_link_to_department_pages(self):
        records = self.extract('''<div class="field--name-field-program-dept"><a>Business</a></div>
            <div class="program-level-section"><h3>Graduate</h3><a href="https://business.example.edu/accountancy">Accountancy, MAcc (ACC-MACC)</a></div>''', 'degrees-and-programs/profile/accountancy')
        self.assertEqual(records[0]['values']['name'], 'Accountancy, MAcc')
        self.assertEqual(records[0]['values']['department'], 'Business')

    def test_repeat_scrape_deduplicates_and_preserves_verified_edits(self):
        records = self.extract('<h2>Alpha Scholarship</h2><p>Applicants must have GPA 3.0.</p><p>$5,000 per year.</p>')
        save_entities(self.uid, records)
        save_entities(self.uid, records)
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(university_id=self.uid).count(), 1)
        listed = self.client.get('/api/university-admin/information/entities/').data['knowledge'][0]
        response = self.client.patch(f"/api/university-admin/knowledge/{listed['id']}/", {
            'expected_revision': listed['revision'], 'content': 'Alpha Scholarship: GPA 3.8',
            'details': {**listed['details'], 'eligibility': 'GPA 3.8'},
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        save_entities(self.uid, records)
        row = UniversityKnowledgeEntry.objects.get(pk=listed['id'])
        self.assertEqual(row.details['eligibility'], 'GPA 3.8')
        self.assertIn('3.8', row.content)

    def test_raw_scrape_topics_are_not_form_entities(self):
        UniversityKnowledgeEntry.objects.create(university_id=self.uid, topic='Courses', content='Raw source material', source_type='scraped')
        self.assertEqual(self.client.get('/api/university-admin/information/entities/').data['knowledge'], [])

    def test_manually_cleared_contact_is_not_refilled(self):
        from universities.models import University
        University.objects.filter(uuid=self.uid).update(contact_phone='')
        save_entities(self.uid, [{'kind': 'overview', 'name': 'University contact information',
            'values': {'contact_phone': '12345'}, 'source_url': 'https://example.edu/contact', 'evidence': {}}])
        url = '/api/university-admin/information/overview/'
        current = self.client.get(url).data
        self.assertEqual(current['values']['contact_phone'], '12345')
        response = self.client.patch(url, {'values': {**current['values'], 'contact_phone': ''},
            'expected_revision': current['revision']}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(url).data['values']['contact_phone'], '')

    def test_same_award_keeps_populations_across_repeated_refreshes(self):
        first = self.extract('<h2>Alpha Scholarship</h2><p>Applicants must be first-year students.</p>', 'first-year-scholarships')
        transfer = self.extract('<h2>Alpha Scholarship</h2><p>Applicants must be transfer students.</p>', 'transfer-scholarships')
        for records in [first, transfer, first, transfer]:
            save_entities(self.uid, records)
        row = UniversityKnowledgeEntry.objects.get(university_id=self.uid)
        self.assertEqual(row.details['eligibility'].count('first-year'), 1)
        self.assertEqual(row.details['eligibility'].count('transfer'), 1)

    def test_manual_entity_creation_rejects_generic_names_and_duplicates(self):
        url = '/api/university-admin/information/entities/'
        payload = {'topic': 'Alpha Scholarship', 'content': 'Alpha Scholarship eligibility: GPA 3.8',
                   'category': 'scholarships', 'details': {'name': 'Alpha Scholarship', 'information_type': 'scholarships', 'eligibility': 'GPA 3.8'}}
        self.assertEqual(self.client.post(url, payload, format='json').status_code, 201)
        self.assertEqual(self.client.post(url, payload, format='json').status_code, 409)
        records = self.extract('<h2>Alpha Scholarship</h2><p>Applicants must have GPA 2.0.</p>')
        save_entities(self.uid, records)
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(university_id=self.uid).count(), 1)
        payload['details']['name'] = 'Scholarship eligibility'
        self.assertEqual(self.client.post(url, payload, format='json').status_code, 400)
