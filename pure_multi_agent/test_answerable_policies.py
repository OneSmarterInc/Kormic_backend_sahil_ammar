"""Applicant inputs and long tuition tables must not cause false escalation."""
from unittest import TestCase
from unittest.mock import patch
from django.test import TestCase as DatabaseTestCase

from pure_multi_agent.answer_checks import applicant_gpa_comparison, violations
from pure_multi_agent.answer_context import compact_evidence
from pure_multi_agent import test_student_gate2 as gate2


class ApplicantScoreTests(TestCase):
    question = "Can a domestic master's applicant with a 2.30 GPA receive conditional admission?"
    evidence = {'content': 'Domestic applicants with GPA 2.250–2.499 on a 4.0 scale may receive conditional enrollment if they meet other admission requirements.'}

    def test_range_membership_uses_decimal_arithmetic_and_boundaries(self):
        for score, relation in (('2.30', 'within'), ('2.250', 'within'), ('2.499', 'within'), ('2.10', 'outside'), ('2.50', 'outside')):
            with self.subTest(score=score):
                result = applicant_gpa_comparison(self.question.replace('2.30', score), {'facts': [self.evidence]})
                self.assertEqual(result['relation'], relation)

    def test_ambiguous_programme_ranges_do_not_infer_eligibility(self):
        self.assertIsNone(applicant_gpa_comparison(self.question, {'facts': [self.evidence,
            {'content': 'Another programme grants conditional admission with GPA between 2.0 and 2.2.'}]}))

    def test_housing_answer_cannot_invent_an_office_referral(self):
        evidence = {'content': 'No on-campus housing. The Office of International Students and Programs helps students find housing.'}
        self.assertTrue(violations('The Office of Student Life helps find housing.', evidence))
        self.assertEqual(violations('The Office of International Students and Programs helps find housing.', evidence), [])

    def test_applicant_gpa_can_be_applied_to_a_documented_range(self):
        answer = 'Yes, a domestic applicant with a 2.30 GPA can receive conditional admission, subject to the other admission requirements.'
        self.assertEqual(violations(answer, self.evidence, question=self.question), [])

    def test_question_cannot_supply_a_university_threshold_or_price(self):
        for answer in ('The minimum GPA is 2.30.', 'Your required GPA is 2.30.',
                       'Tuition is $690.', 'Your GPA must be at least 2.30.'):
            with self.subTest(answer=answer):
                self.assertTrue(violations(answer, self.evidence, question=self.question + ' Is tuition $690?'))

    def test_a_different_applicant_score_is_not_supported(self):
        self.assertTrue(violations('An applicant with a 2.10 GPA qualifies.', self.evidence, question=self.question))

    def test_applicant_score_and_threshold_in_one_sentence_remain_distinct(self):
        evidence = {'content': self.evidence['content'] + ' Standard admission requires 2.50.'}
        self.assertEqual(violations('With a 2.30 GPA, you are below the standard minimum of 2.50.', evidence, question=self.question), [])
        self.assertTrue(violations('With a 2.30 GPA, you meet the minimum GPA of 2.30.', evidence, question=self.question))


class TuitionExcerptTests(TestCase):
    def test_abbreviated_academic_year_supports_full_year_only(self):
        evidence = {'content': 'Tuition Rates for 2026-27: master’s tuition $690 per credit.'}
        self.assertEqual(violations('For 2026–2027, tuition is $690 per credit.', evidence), [])
        self.assertTrue(violations('For 2027–2028, tuition is $690 per credit.', evidence))

    def test_policy_details_beat_introductory_topic_mentions(self):
        from pure_multi_agent.answer_context import evidence_excerpt
        text = ('International Student Orientation includes Immigration Check-In. ' + 'Contact office for appointments. ' * 100
                + 'Immigration Check-In is mandatory. Immigration Check-In occurs on campus. '
                + 'Bring to Immigration Check-In electronic copies of passport, visa, I-20 and I-94, local address, US phone and myFranklin login. '
                + 'Register for Immigration Check-In. ' + 'Footer navigation. ' * 100)
        excerpt = evidence_excerpt(text, 'What must students bring to Immigration Check-In?', 1200)
        for term in ('passport', 'visa', 'I-20', 'I-94', 'local address', 'myFranklin'):
            self.assertIn(term, excerpt)

    def test_oversized_masters_table_is_retained_with_year_and_rate(self):
        text = ('Undergraduate Costs 2026-2027 Standard tuition $398. ' + 'General campus costs. ' * 150
                + "Master's Costs 2025-2026 Standard tuition per credit $670 " + 'Other fees. ' * 200
                + "Master's Costs 2026-2027 Standard tuition per credit hour $690. " + 'Other fees. ' * 220
                + 'Doctorate Costs 2026-2027 Standard tuition $828. ' + 'Other fees. ' * 200)
        data = compact_evidence({'facts': [{'content': text, 'source_url': 'https://example.edu/fees'}]},
                                'What is standard master’s tuition per credit for 2026–2027?')
        result = data['facts'][0]
        self.assertIn("Master's Costs 2026-2027", result['content'])
        self.assertIn('$690', result['content'])
        self.assertNotIn('$828', result['content'])
        self.assertNotIn('$670', result['content'])
        self.assertEqual(result['source_url'], 'https://example.edu/fees')
        self.assertTrue(result['evidence_partial'])

    def test_unqualified_question_does_not_invent_degree_scope(self):
        data = compact_evidence({'facts': [{'content': 'Tuition is unavailable.'}]}, 'What is tuition?')
        self.assertEqual(data['facts'][0]['content'], 'Tuition is unavailable.')

    def test_compaction_preserves_retrieval_order_instead_of_reranking_boilerplate(self):
        data = compact_evidence({'facts': [
            {'content': 'OISP helps you find housing.', 'retrieval_rank': 0},
            {'content': 'Who helps newly admitted international students? Navigation menu housing.', 'retrieval_rank': 1},
        ]}, 'Who helps newly admitted international students find housing?')
        self.assertEqual(data['facts'][0]['content'], 'OISP helps you find housing.')


class AdviserPolicyTests(DatabaseTestCase):
    setUp = gate2.StudentGateTwoTests.setUp

    def test_context_limit_does_not_raise_an_officer_query(self):
        from pure_multi_agent.context_budget import ContextBudgetExceeded
        from pure_multi_agent.registered_adviser import _consult
        from agent_queries.models import AgentQuery
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value={'facts': [ApplicantScoreTests.evidence]}), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=ContextBudgetExceeded('too large')), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, gate2.QUESTIONS[1][0])
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
        self.assertIn('Please retry', result['answer'])

    def test_realistic_gpa_range_answer_passes_validator_without_escalation(self):
        from langchain_core.messages import AIMessage
        from pure_multi_agent.registered_adviser import _consult
        from agent_queries.models import AgentQuery
        question = gate2.QUESTIONS[1][0]
        url = 'https://example.edu/admissions'
        evidence = {'facts': [{'content': ApplicantScoreTests.evidence['content'], 'source_url': url}]}

        def generate(messages, tools, **kwargs):
            contradictory = AIMessage(content='Your 2.30 GPA is below the automatic conditional-admission threshold. Other admission requirements apply. Source: ' + url)
            with self.assertRaisesRegex(ValueError, 'contradictory range comparison'):
                kwargs['response_validator'](contradictory)
            reply = AIMessage(content='Yes, a domestic applicant with a 2.30 GPA can receive conditional admission if the other admission requirements are met. Source: ' + url)
            kwargs['response_validator'](reply)
            return reply

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=generate), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, question)
        self.assertFalse(result['pending'])
        self.assertIn('2.30', result['answer'])
        self.assertFalse(AgentQuery.objects.exists())
