"""Offline regressions for the production student-to-university path.

These controlled fixtures verify routing and provenance, not current tuition.
The production demo still needs a fresh run against its imported source records.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from langchain_core.messages import AIMessage, HumanMessage

from django_api.models import StudentProfile, UniversityKnowledgeEntry
from universities.models import University, KnowledgeGroup
from knowledge.university_kb import UniversityKnowledgeBase
from pure_multi_agent.turn_policy import classify
from pure_multi_agent.registered_adviser import _consult


QUESTIONS = [
    ('What is the standard minimum GPA for master’s admission?', 'Graduate GPA', 'Standard master’s admission requires GPA 2.50; Nursing requires 3.00.', 'admissions/masters-students'),
    ('Can a domestic master’s applicant with a 2.30 GPA receive conditional admission?', 'Conditional admission GPA', 'Domestic applicants with GPA between 2.250 and 2.499 can receive conditional admission if they meet the other admission requirements.', 'admissions/masters-students'),
    ('Can an international applicant email their own transcript and have it count as official?', 'Official international transcripts', 'Applicant-submitted transcripts are not official; the evaluator must send the evaluation directly.', 'admissions/international-students'),
    ('Is Franklin currently admitting F-1 undergraduate applicants?', 'F-1 undergraduate admissions policy', 'Franklin is not currently admitting F-1 undergraduate applicants.', 'admissions/international-students'),
    ('What is standard master’s tuition per credit for 2026–2027 including changes effective Fall 2026?', 'Standard graduate tuition', 'Standard master’s tuition for 2026–2027 is $690 per credit.', 'tuition-financial-aid/tuition-fees'),
    ('What is the F-1 new-student deposit, and is it applied toward tuition?', 'F-1 tuition deposit', 'The new-student tuition deposit is applied toward tuition.', 'admissions/international-students'),
    ('Does Franklin offer on-campus housing?', 'Campus housing', 'Franklin does not offer on-campus housing.', 'current-students/international-students/getting-started'),
    ('Who helps newly admitted international students find housing?', 'Housing assistance', 'OISP helps newly admitted international students find housing.', 'current-students/international-students/getting-started'),
    ('How is International Student Orientation delivered?', 'International Student Orientation', 'International Student Orientation is a one-day series of interactive online sessions.', 'current-students/international-students/international-student-orientation'),
    ('What must students bring to Immigration Check-In?', 'Immigration Check-In', 'Bring passport, visa, I-20, I-94, local address, U.S. phone number and myFranklin login information.', 'current-students/international-students/international-student-orientation'),
]


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class StudentGateTwoTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Demo')
        self.school = University.objects.create(name='Franklin University', website_url='https://www.franklin.edu')
        self.group = KnowledgeGroup.objects.create(university=self.school, slug='campus_life', escalation_contact_email='test@example.test')
        self.ctx = {'canonical_student_id': str(self.student.uuid), 'student_profile': {}, 'turn_id': 'gate2',
                    'turn_intent': {'route': 'university', 'institutions': ['Franklin']}}

    def test_all_ten_explicit_citations_route_without_a_classifier(self):
        for question, *_ in QUESTIONS:
            text = question + ' Cite the official Franklin page you actually read to support this answer'
            with self.subTest(question=question), patch('pure_multi_agent.model_router.invoke') as model:
                intent = classify({'canonical_student_id': str(self.student.uuid), 'current_message': text}, [HumanMessage(content=text)])
                self.assertEqual(intent['route'], 'university')
                self.assertEqual(intent['institutions'], ['Franklin'])
                model.assert_not_called()

    def test_all_ten_consultations_accept_short_name_and_preserve_exact_citation(self):
        for question, topic, fact, path in QUESTIONS:
            url = 'https://www.franklin.edu/' + path
            answer = fact + ' [Franklin source](' + url + ')'
            def respond(messages, tools, **kwargs):
                reply = AIMessage(content=answer)
                kwargs['response_validator'](reply)
                return reply
            evidence = {'facts': [{'topic': topic, 'content': fact, 'source_url': url}]}
            with self.subTest(question=question), patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), patch('pure_multi_agent.registered_adviser.invoke', side_effect=respond), patch('agents.student_context.university_context', return_value={}):
                result = _consult(dict(self.ctx), self.school, question)
                self.assertEqual(result['answer'], answer)
                self.assertFalse(result['pending'])
                self.assertEqual(result['sources'][0]['source_url'], url)

    def test_lazy_search_finds_new_housing_record_after_more_than_128_old_matches(self):
        UniversityKnowledgeEntry.objects.bulk_create([
            UniversityKnowledgeEntry(university_id=str(self.school.uuid), topic='International student careers', content='International students can use career services.')
            for _ in range(150)
        ])
        target = UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid), topic='Newly admitted international students housing', content='OISP helps newly admitted international students find housing.', source_type='human_verified', source_url='https://www.franklin.edu/housing')
        rows = UniversityKnowledgeBase(str(self.school.uuid), lazy=True).search(QUESTIONS[7][0] + ' Cite the official Franklin page you actually read to support this answer')
        self.assertEqual(rows[0].db_id, target.pk)

    def test_ten_topics_remain_retrievable_when_loaded_together(self):
        records = []
        for question, topic, fact, path in QUESTIONS:
            records.append(UniversityKnowledgeEntry.objects.create(
                university_id=str(self.school.uuid), topic=topic, content=fact,
                source_type='human_verified', source_url='https://www.franklin.edu/' + path,
            ))
        for (question, *_), record in zip(QUESTIONS, records):
            with self.subTest(question=question):
                rows = UniversityKnowledgeBase(str(self.school.uuid), lazy=True).search(question, limit=5)
                self.assertIn(record.pk, [row.db_id for row in rows])

    def test_retrieval_failure_does_not_announce_a_nonexistent_escalation(self):
        from agent_queries.models import AgentQuery
        from pure_multi_agent.model_router import AIServiceUnavailable
        with patch('pure_multi_agent.tools.university_tools._university_evidence', side_effect=RuntimeError('offline')), patch('pure_multi_agent.registered_adviser.invoke', side_effect=AIServiceUnavailable('Unavailable')), patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, QUESTIONS[6][0])
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
        self.assertNotIn('pending a', result['answer'])

    def test_missing_housing_answer_creates_pending_campus_life_query(self):
        from agent_queries.models import AgentQuery
        question = 'Can Franklin guarantee accessible off-campus housing for my arrival next term?'
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value={'facts': []}), patch('pure_multi_agent.registered_adviser.invoke', return_value=AIMessage(content='I cannot confirm a housing guarantee from the available records.')), patch('agents.student_context.university_context', return_value={}), patch('agent_queries.services.notify_query') as notify:
            result = _consult(dict(self.ctx), self.school, question)
            result2 = _consult(dict(self.ctx), self.school, question)
        row = AgentQuery.objects.get()
        self.assertEqual(row.question, question)
        self.assertEqual(row.group_id, self.group.pk)
        self.assertEqual(row.status, 'unanswered')
        self.assertTrue(result['pending'])
        self.assertEqual(result['pending_query']['query_id'], result2['pending_query']['query_id'])
        notify.assert_called_once()

    def test_generation_failure_does_not_create_a_university_policy_question(self):
        from pure_multi_agent.model_router import AIServiceUnavailable
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value={'facts': [{'content': 'Career development information.', 'source_url': 'https://www.franklin.edu/careers'}]}), patch('pure_multi_agent.registered_adviser.invoke', side_effect=AIServiceUnavailable('Unavailable')), patch('agents.student_context.university_context', return_value={}), patch('agent_queries.services.notify_query'):
            result = _consult(dict(self.ctx), self.school, QUESTIONS[6][0])
        self.assertFalse(result['pending'])
        from agent_queries.models import AgentQuery
        self.assertFalse(AgentQuery.objects.exists())
        self.assertNotIn('Saved source excerpt', result['answer'])
        self.assertEqual(result['sources'], [])
