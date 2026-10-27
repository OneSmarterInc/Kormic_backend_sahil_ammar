from unittest import TestCase
from unittest.mock import patch

from langchain_core.messages import AIMessage

from pure_multi_agent.loaded_policy import (
    covered_policy, covered_tuition_answer, tuition_wording_problems,
)
from pure_multi_agent.response_contract import problems
from pure_multi_agent.test_student_gate2 import StudentGateTwoTests


URL = 'https://www.franklin.edu/tuition-fee-changes-fall-2026'
QUESTION = ('What is Franklin University’s standard master’s tuition per credit for 2026–2027, '
            'including changes effective Fall 2026? How long does the tuition guarantee last '
            'for new and currently enrolled master’s students? Cite the official Franklin pages '
            'you actually read. Distinguish tuition from additional fees.')
POLICY = ('Tuition for all new master’s students enrolling for fall 2026 will be $690 per credit hour. '
          'Currently enrolled master’s students maintain their present rate. '
          'Students lock in their first-term tuition rate on day one. '
          'For master’s students the lock lasts three years (9 terms). '
          'Current students retain their rate for up to three additional years beginning with the fall 2026 trimester. '
          'Beginning in fall 2026, a $100 resource fee is charged every trimester.')
EVIDENCE = {'facts': [{'content': POLICY, 'source_url': URL}]}


class LoadedPolicyTests(TestCase):
    def test_wrong_guarantee_and_semester_fee_are_rejected(self):
        wrong = ('The guarantee lasts three years from enrollment for everyone. '
                 'The resource fee is $100 per semester. Source: ' + URL)
        self.assertTrue(tuition_wording_problems(wrong, QUESTION, EVIDENCE))

    def test_complete_cited_answer_has_both_cohorts_and_correct_fee(self):
        answer = covered_tuition_answer(QUESTION, EVIDENCE)
        self.assertIn('three additional years beginning with the Fall 2026', answer)
        self.assertIn('$100 every trimester', answer)
        self.assertEqual(tuition_wording_problems(answer, QUESTION, EVIDENCE), [])

    def test_partial_policy_and_extra_request_are_not_claimed_complete(self):
        self.assertFalse(covered_tuition_answer(QUESTION, {'facts': [
            {'content': 'Fall 2026 tuition $690 per credit.', 'source_url': URL}]}))
        extra = QUESTION.replace('Cite the official', 'Am I eligible for a fee waiver? Cite the official')
        self.assertFalse(covered_policy(extra, EVIDENCE))

    def test_housing_absence_is_an_answer_but_accessibility_is_not(self):
        evidence = {'facts': [{'content': 'Franklin does not offer on-campus housing.', 'source_url': 'https://www.franklin.edu/housing'}]}
        self.assertTrue(covered_policy('Does Franklin offer on-campus housing?', evidence))
        self.assertFalse(covered_policy('Does Franklin offer on-campus housing? Can it guarantee accessible housing?', evidence))
        evidence['facts'] += EVIDENCE['facts']
        self.assertFalse(covered_tuition_answer('Does Franklin offer on-campus housing?', evidence))

    def test_internal_repair_messages_are_rejected(self):
        for text in ('Let me provide the answer without the tool call.',
                     'Thank you for the correction. Tuition is $690.',
                     'The tool call was rejected.'):
            self.assertTrue(problems(text))
        self.assertEqual(problems('The university has answered your question.'), [])

    def test_long_notice_keeps_guarantee_and_fee_clauses(self):
        from pure_multi_agent.answer_context import compact_evidence
        data = {'facts': [{'source_url': URL, 'content': POLICY + ' Navigation links. ' * 500}]}
        compact = compact_evidence(data, QUESTION)
        self.assertTrue(covered_tuition_answer(QUESTION, compact))


class ConsultationPolicyTests(StudentGateTwoTests):
    def test_fee_section_from_effective_policy_survives_marketing_results(self):
        from django_api.models import UniversityKnowledgeEntry
        from knowledge.university_kb import UniversityKnowledgeBase
        UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid),
            content=POLICY.split('Beginning in fall')[0], topic='Tuition guarantee changes', source_url=URL)
        fee = UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid),
            content='Beginning in fall 2026, a $100 resource fee is charged every trimester.',
            topic='Resource fee', source_url=URL)
        for index in range(10):
            UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid),
                content='Master tuition per credit for 2026–2027, Fall 2026. New and currently enrolled students tuition guarantee.',
                topic='Master tuition', source_url=f'https://www.franklin.edu/programs/{index}')
        rows = UniversityKnowledgeBase(str(self.school.uuid), lazy=True).search(QUESTION, limit=5)
        self.assertIn(fee.pk, [row.db_id for row in rows])

    def test_complete_notice_answers_without_model_or_escalation(self):
        from agent_queries.models import AgentQuery
        from pure_multi_agent.registered_adviser import _consult
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=EVIDENCE), \
             patch('pure_multi_agent.registered_adviser.invoke') as model, \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, QUESTION)
        model.assert_not_called()
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
        self.assertIn('three additional years', result['answer'])
        self.assertIn(URL, result['answer'])

    def test_loaded_housing_answer_disables_redundant_escalation(self):
        from agent_queries.models import AgentQuery
        from pure_multi_agent.registered_adviser import _consult
        url = 'https://www.franklin.edu/housing'
        evidence = {'facts': [{'content': 'Franklin does not offer on-campus housing.', 'source_url': url}]}
        def respond(messages, tools, **kwargs):
            self.assertEqual(tools, [])
            reply = AIMessage(content='Franklin does not offer on-campus housing. Source: ' + url)
            kwargs['response_validator'](reply)
            return reply
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=respond), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, 'Does Franklin offer on-campus housing?')
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
