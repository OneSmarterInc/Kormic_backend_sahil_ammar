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
