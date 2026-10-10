from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from university_research.models import PublicUniversity, UniversityPage, UniversityCourse, UniversityIntake
from university_research.services import retrieve
from pure_multi_agent.answer_context import compact_evidence, partial_answer

@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class FetchAudit(TestCase):
    def test_requested_course_and_intake_are_ranked_before_limits(self):
        row=PublicUniversity.objects.create(identity_key='audit',name='Example',website='https://example.edu')
        page=UniversityPage.objects.create(university=row,url=row.website,content='Official courses',content_hash='audit',fetched_at=timezone.now(),provider='scraper_extracted')
        for i in range(101):
            name='Target Robotics MSc' if i==100 else f'Unrelated course {i}'
            UniversityCourse.objects.create(university=row,page=page,name=name,requirements='GPA 3.5',source_quote=name,fetched_at=timezone.now())
            UniversityIntake.objects.create(university=row,page=page,course_name=name,term='Fall',deadline='2027-01-01',source_quote=name,fetched_at=timezone.now())
        with patch('knowledge.vectors.enabled',return_value=False):
            result=retrieve(row,'Target Robotics MSc requirements and deadline')
        self.assertEqual(row.courses.filter(name='Target Robotics MSc').count(),1)
        self.assertEqual(len(result['courses']),100)
        self.assertEqual(result['courses'][0]['name'], 'Target Robotics MSc')
        self.assertEqual(result['intakes'][0]['course_name'], 'Target Robotics MSc')

    def test_single_long_relevant_fact_keeps_explicit_partial_evidence(self):
        result=compact_evidence({'facts':[{'topic':'Admissions requirements','content':'A'*13000}]},'Admissions requirements')
        self.assertEqual(len(result['facts']),1)
        self.assertTrue(result['facts'][0]['evidence_partial'])
        self.assertLess(len(result['facts'][0]['content']),3500)

    def test_admissions_excerpts_preserve_exemption_and_source(self):
        content = ('Campus news. ' * 1200) + '\n\nAdmission requirements: GRE is required for the Robotics MSc. Applicants with an approved waiver are exempt.\n\n' + ('Sports news. ' * 1200)
        evidence = compact_evidence({'facts':[{'topic':'Robotics MSc admission requirements', 'content':content, 'source_url':'https://example.edu/robotics'}]}, 'Robotics MSc admission requirements')
        fact = evidence['facts'][0]
        self.assertIn('approved waiver',fact['content'])
        self.assertEqual(fact['source_url'],'https://example.edu/robotics')
        reply = partial_answer('Robotics MSc admission requirements', evidence)
        self.assertIn('approved waiver', reply)
        self.assertIn('https://example.edu/robotics',reply)
        self.assertIn('partial',reply)

    def test_admission_fallback_does_not_answer_with_unrequested_tuition(self):
        reply=partial_answer('Admission requirement for Robotics MSc', {'courses':[{
            'name':'Robotics MSc','requirements':'A relevant bachelor degree.','tuition':'$99999',
            'page__url':'https://example.edu/robotics'}]})
        self.assertIn('relevant bachelor',reply)
        self.assertIn('https://example.edu/robotics',reply)
        self.assertNotIn('99999',reply)

    def test_admission_fact_is_ranked_over_recent_generic_university_news(self):
        from university_research.models import UniversityFact
        row=PublicUniversity.objects.create(identity_key='admissions',name='Example',website='https://example.edu')
        page=UniversityPage.objects.create(university=row,url=row.website,content='Admissions',content_hash='audit',fetched_at=timezone.now(),provider='scraper_extracted')
        UniversityFact.objects.create(university=row,page=page,topic='Admissions eligibility',content='A relevant bachelor degree is required.',source_quote='A relevant bachelor degree is required.',fetched_at=timezone.now())
        for i in range(15):
            UniversityFact.objects.create(university=row,page=page,topic='University news',content='The university has a new campus event.',source_quote='The university has a new campus event.',fetched_at=timezone.now())
        with patch('knowledge.vectors.enabled',return_value=False):
            result=retrieve(row,'What is the admission requirement of the university?')
        self.assertEqual(result['facts'][0]['topic'],'Admissions eligibility')
