from django.test import TestCase, override_settings
from django_api.models import UniversityKnowledgeEntry
from knowledge.university_kb import UniversityKnowledgeBase
from universities.models import University


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class KeywordRetrievalTests(TestCase):
    def setUp(self):
        self.uni = University.objects.create(name="Own University")
        self.other = University.objects.create(name="Other University")
        self.fact = UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid),
            topic="Graduate Scholarships", content="Apply online by February 1 for merit scholarships.", source_type="manual")
        for i in range(14):
            UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid),
                topic=f"Per hour tuition {i}", content="Four hours of laboratory tuition.")
        UniversityKnowledgeEntry.objects.create(university_id=str(self.other.uuid),
            topic="Scholarship policies", content="OTHER_UNIVERSITY_FACT")
        self.kb = UniversityKnowledgeBase(str(self.uni.uuid))

    def test_screenshot_typo_retrieves_scholarships_not_hour(self):
        results = self.kb.search("what are our policies related to scholarsif")
        self.assertEqual([e.db_id for e in results], [self.fact.pk])

    def test_singular_and_plural_match(self):
        for question in ("scholarship", "scholarships", "scholarshp"):
            self.assertEqual(self.kb.search(question)[0].db_id, self.fact.pk)

    def test_short_words_do_not_match_inside_other_words(self):
        self.assertEqual(self.kb.search("our"), [])
        self.assertEqual(self.kb.search("aid"), [])

    def test_reload_finds_newly_added_knowledge(self):
        fact = UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid),
            topic="Housing", content="Dormitory applications open in May.")
        self.kb.reload()
        self.assertEqual(self.kb.search("housing")[0].db_id, fact.pk)

    def test_policy_source_survives_programme_navigation_noise_and_candidate_limit(self):
        UniversityKnowledgeEntry.objects.bulk_create([
            UniversityKnowledgeEntry(university_id=str(self.uni.uuid),
                topic='Admissions Requirements', source_type='scraped', confidence=0.55,
                source_url=f'https://example.edu/degrees/programme-{index}',
                content='International student orientation admission requirements. ' + 'Programme modules and coursework. ' * 100)
            for index in range(150)
        ])
        orientation = UniversityKnowledgeEntry.objects.create(
            university_id=str(self.uni.uuid), topic='Admissions Requirements',
            source_type='scraped', confidence=0.55,
            source_url='https://example.edu/international-student-orientation',
            content='International Student Orientation is a one-day series of interactive online sessions. Immigration check-in requires your passport and visa.')
        result = UniversityKnowledgeBase(str(self.uni.uuid), lazy=True).search(
            'How is International Student Orientation delivered?', limit=5)
        self.assertEqual(result[0].db_id, orientation.pk)

    def test_housing_policy_beats_fee_rows_sharing_applicant_context(self):
        UniversityKnowledgeEntry.objects.bulk_create([
            UniversityKnowledgeEntry(
                university_id=str(self.uni.uuid), source_type='scraped', confidence=0.95,
                topic=f'International students enrollment fee {index}',
                content='International students pay an enrollment fee.',
                source_url='https://example.edu/tuition-fees')
            for index in range(10)
        ])
        housing = UniversityKnowledgeEntry.objects.create(
            university_id=str(self.uni.uuid), topic='Admissions Requirements',
            source_type='scraped', confidence=0.55,
            source_url='https://example.edu/international-students/getting-started',
            content='OISP helps newly admitted international students find housing.')
        for lazy in (False, True):
            with self.subTest(lazy=lazy):
                results = UniversityKnowledgeBase(str(self.uni.uuid), lazy=lazy).search(
                    'Who helps newly admitted international students find housing?', limit=5)
                self.assertEqual(results[0].db_id, housing.pk)

    def test_effective_tuition_policy_beats_generic_programme_guarantee(self):
        for i in range(12):
            UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid),
                topic='Master tuition', source_type='manual', confidence=1,
                content='Master tuition Fall 2026 tuition guarantee locked through graduation. ' * 4,
                source_url=f'https://example.edu/programme/{i}')
        policy = UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid),
            topic='Tuition changes', source_type='scraped', confidence=0.55,
            content='Tuition changes effective Fall 2026. Master tuition $690 per credit. '
                    'Tuition guarantee for master students lasts three years (9 terms).',
            source_url='https://example.edu/tuition-changes-fall-2026')
        self.kb.reload()
        results = self.kb.search('What is master tuition for Fall 2026, including changes?', limit=5)
        self.assertEqual(results[0].db_id, policy.pk)
        # A dated policy must not be promoted for a different requested year.
        self.assertNotEqual(self.kb.search('master tuition Fall 2027', limit=5)[0].db_id, policy.pk)
