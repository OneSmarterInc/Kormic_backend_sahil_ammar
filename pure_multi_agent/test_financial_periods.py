from datetime import date
from unittest import TestCase
from unittest.mock import patch

from django.test import TestCase as DatabaseTestCase, override_settings
from langchain_core.messages import AIMessage

from pure_multi_agent.financial_periods import financial_period_context
from pure_multi_agent.test_student_gate2 import StudentGateTwoTests


class FinancialPeriodTests(TestCase):
    def test_flat_fee_table_returns_only_deposit_row_with_conditions(self):
        from pure_multi_agent.financial_periods import financial_reference_answer
        table = ('Application fee $40 F-1 International Student Enrollment Term Confirmation Deposit '
                 '(non-refundable; applied directly to account for intended term of enrollment) $200 '
                 'F-1 International New Student Deposit (non-refundable; applied directly to first term tuition) $2,000 '
                 'Graduation application fee $65 Late payment fee $100 ' + 'Other fees and charges. ' * 20)
        assessment = self.assess(table)
        answer = financial_reference_answer(assessment)
        for term in ('$2,000', 'non-refundable', 'applied directly to first term tuition', 'year not specified'):
            self.assertIn(term, answer)
        for term in ('$40', '$200 ', '$65', 'Graduation', 'Late payment', '2025'):
            self.assertNotIn(term, answer)
        self.assertLess(len(answer), 700)

    def test_long_table_excerpt_keeps_actual_academic_heading(self):
        from pure_multi_agent.answer_context import compact_evidence
        row = 'F-1 International New Student Deposit (non-refundable; applied directly to first term tuition) $2,000'
        text = '2025–2026 Fees\n' + 'Other fee information. ' * 300 + row + ' Graduation fee $65 ' + 'Footer. ' * 400
        compact = compact_evidence({'facts': [{'content': text, 'source_url': 'https://example.edu/fees'}]},
                                   'What is the current F-1 new-student deposit?')
        content = compact['facts'][0]['content']
        self.assertIn('2025–2026', content)
        self.assertIn(row, content)
        self.assertNotIn('Graduation', content)
        self.assertEqual(self.assess(content)['rates'][0]['period'], '2025–2026')

    def test_mixed_academic_years_keep_current_deposit_evidence(self):
        from pure_multi_agent.answer_context import compact_evidence
        row = 'F-1 International New Student Deposit (non-refundable; applied directly to first term tuition) '
        text = '2025–2026 Fees\n' + row + '$2,000 ' + 'Other fees. ' * 700 + '\n2026–2027 Fees\n' + row + '$2,100'
        compact = compact_evidence({'facts': [{'content': text}]}, 'What is the F-1 new-student deposit for 2026–2027?')
        content = compact['facts'][0]['content']
        self.assertTrue(content.startswith('2026–2027'))
        self.assertIn('$2,100', content)
        self.assertFalse(self.assess(content, 'What is the F-1 new-student deposit for 2026–2027?')['requires_confirmation'])

    def test_reference_recovery_never_uses_uncited_or_truncated_text(self):
        from pure_multi_agent.financial_periods import financial_reference_answer
        self.assertEqual(financial_reference_answer({'requires_confirmation': True,
            'rates': [{'period': '2025–2026', 'quote': 'Deposit $2,000', 'source_url': ''}]}), '')

    def test_equivalent_year_labels_are_accepted(self):
        from pure_multi_agent.financial_periods import normalize_period
        self.assertEqual(normalize_period('2025 – 26'), normalize_period('2025-2026'))

    def assess(self, content, question='What is the F-1 new-student deposit?'):
        return financial_period_context(question, {'facts': [
            {'content': content, 'source_url': 'https://example.edu/fees'}]}, today=date(2026, 10, 27))

    def test_old_deposit_requires_confirmation_even_with_current_tuition(self):
        result = self.assess('2026–2027 Tuition $690 per credit.\n2025–2026 Fees\nF-1 new-student deposit $2,000, applied to first-term tuition.')
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual(result['rates'][0]['period'], '2025–2026')

    def test_current_deposit_does_not_escalate_just_because_old_rate_exists(self):
        result = self.assess('2025–2026 F-1 new-student deposit $2,000.\n2026–2027 F-1 new-student deposit $2,100.')
        self.assertFalse(result['requires_confirmation'])

    def test_current_semester_and_abbreviated_academic_year(self):
        for text in ('Fall 2026 F-1 new-student deposit $2,100.', '2026-27 F-1 new-student deposit $2,100.'):
            self.assertFalse(self.assess(text)['requires_confirmation'])

    def test_current_enrollment_deposit_does_not_validate_old_new_student_deposit(self):
        result = self.assess('2025–2026 F-1 new-student deposit $2,000.\n2026–2027 Enrollment confirmation deposit $200.')
        self.assertTrue(result['requires_confirmation'])

    def test_undated_rate_requires_confirmation(self):
        self.assertTrue(self.assess('F-1 new-student deposit $2,000.')['requires_confirmation'])

    def test_explicit_historical_question_is_not_a_current_rate_request(self):
        self.assertFalse(self.assess('2025–2026 F-1 new-student deposit $2,000.',
                                    'What was the F-1 new-student deposit in 2025–2026?')['requires_confirmation'])

    def test_year_in_tuition_sentence_is_recognized(self):
        self.assertFalse(self.assess('Standard master’s tuition for 2026–2027 is $690 per credit.',
                                    'What is master’s tuition for 2026–2027?')['requires_confirmation'])


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class FinancialEscalationTests(DatabaseTestCase):
    setUp = StudentGateTwoTests.setUp

    def test_unsupported_old_year_recovers_current_cited_rate_without_escalating(self):
        from pure_multi_agent.registered_adviser import _consult
        from pure_multi_agent.model_router import InvalidToolResponse
        from agent_queries.models import AgentQuery
        url = 'https://www.franklin.edu/tuition-financial-aid/tuition-fees'
        evidence = {'facts': [{'content': '2026–2027 F-1 International New Student Deposit (non-refundable; applied directly to first term tuition) $2,000', 'source_url': url}]}

        def reject(messages, tools, **kwargs):
            try:
                kwargs['response_validator'](AIMessage(content='The deposit was $2,000 in 2025. Source: ' + url))
            except ValueError as cause:
                self.assertIn('Unsupported numeric claims: 2025', str(cause))
                raise InvalidToolResponse('Invalid after correction') from cause
            self.fail('Invented historical year was accepted')

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=reject), \
             patch('agents.student_context.university_context', return_value={}), \
             patch('pure_multi_agent.financial_periods.date', wraps=date) as clock, \
             patch('agent_queries.services.notify_query') as notify:
            clock.today.return_value = date(2026, 10, 27)
            result = _consult(dict(self.ctx), self.school, 'What is the current F-1 new-student deposit, and is it applied toward tuition?')
        for term in ('$2,000', '2026–2027', 'non-refundable', 'applied directly to first term tuition', url):
            self.assertIn(term, result['answer'])
        for term in ('2025', 'Please retry', 'pending a university response'):
            self.assertNotIn(term, result['answer'])
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
        notify.assert_not_called()
        self.assertEqual(len(result['sources']), 1)

    def test_rejected_financial_prose_preserves_cited_rate_and_pending_query(self):
        from pure_multi_agent.registered_adviser import _consult
        from pure_multi_agent.model_router import InvalidToolResponse
        from agent_queries.models import AgentQuery, AgentConversationMessage
        from universities.models import KnowledgeGroup
        KnowledgeGroup.objects.create(university=self.school, slug='money')
        url = 'https://www.franklin.edu/fees'
        evidence = {'facts': [{'content': '2025–2026 F-1 new-student deposit $2,000, non-refundable, applied directly to first term tuition.', 'source_url': url}]}

        def reject(*args, **kwargs):
            try:
                raise ValueError('The query was routed to Money, not Admissions.')
            except ValueError as cause:
                raise InvalidToolResponse('Invalid after correction') from cause

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=reject), \
             patch('agents.student_context.university_context', return_value={}), \
             patch('agent_queries.services.notify_query') as notify:
            for _ in range(2):
                result = _consult(dict(self.ctx), self.school, 'What is the current F-1 new-student deposit?')
        for term in ('$2,000', '2025–2026', 'non-refundable', 'applied directly to first term tuition', url,
                     'Money department', 'pending a university response'):
            self.assertIn(term, result['answer'])
        self.assertNotIn('Please retry', result['answer'])
        self.assertTrue(result['pending'])
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(AgentQuery.objects.count(), 1)
        notify.assert_called_once()
        error = AgentConversationMessage.objects.filter(kind='error').first()
        self.assertEqual(error.metadata['validation_reason'], 'The query was routed to Money, not Admissions.')

    def test_current_rate_answer_does_not_create_an_escalation(self):
        from agent_queries.models import AgentQuery
        from pure_multi_agent.registered_adviser import _consult
        url = 'https://www.franklin.edu/fees'
        evidence = {'facts': [{'content': 'Fall 2026 F-1 new-student deposit $2,100 applied to first term tuition.', 'source_url': url}]}
        answer = 'For Fall 2026, the F-1 new-student deposit is $2,100, applied to first term tuition. Source: ' + url
        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', return_value=AIMessage(content=answer)), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, 'What is the F-1 new-student deposit for Fall 2026?')
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())

    def test_general_gpa_question_uses_loaded_policy_without_escalation_tool(self):
        from agent_queries.models import AgentQuery
        from pure_multi_agent.registered_adviser import _consult
        url = 'https://www.franklin.edu/admissions/admission-requirements/master-degrees'
        evidence = {'facts': [{'content': 'Domestic applicants with GPA between 2.250 and 2.499 receive conditional admission if they meet other admission requirements.', 'source_url': url}]}
        question = 'Can a domestic master’s applicant with a 2.30 GPA receive conditional admission at Franklin University?'

        def generate(messages, tools, **kwargs):
            self.assertEqual(tools, [])
            with self.assertRaisesRegex(ValueError, 'loaded general admissions policy'):
                kwargs['response_validator'](AIMessage(content='I cannot confirm this policy. I have escalated your question.'))
            reply = AIMessage(content='Yes, a domestic applicant with a 2.30 GPA can receive conditional admission if the other admission requirements are met. Source: ' + url)
            kwargs['response_validator'](reply)
            return reply

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=generate), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, question)
        self.assertFalse(result['pending'])
        self.assertFalse(AgentQuery.objects.exists())
        self.assertIn(url, result['answer'])

    def test_gpa_answer_with_timestamped_officer_history_does_not_crash(self):
        from django.utils import timezone
        from agent_queries.models import AgentQuery
        from agent_queries.services import conversation_for
        conversation = conversation_for(str(self.student.uuid), str(self.school.uuid))
        previous = AgentQuery.objects.create(conversation=conversation, direction=AgentQuery.Direction.STUDENT,
            question='Does Franklin require GRE?', question_hash='previous-gre',
            status='answered', answer='GRE is not required.', answered_at=timezone.now())
        # Exercise the consultation with a real timestamped officer answer.
        from pure_multi_agent.registered_adviser import _consult
        url = 'https://www.franklin.edu/admissions/admission-requirements/master-degrees'
        evidence = {'facts': [{'content': 'Domestic applicants with GPA between 2.250 and 2.499 receive conditional admission if they meet other admission requirements.', 'source_url': url}]}
        answer = 'A domestic applicant with a 2.30 GPA can receive conditional admission if the other admission requirements are met. Source: ' + url

        def generate(messages, tools, **kwargs):
            reply = AIMessage(content=answer)
            kwargs['response_validator'](reply)
            return reply

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=generate), \
             patch('agents.student_context.university_context', return_value={}):
            result = _consult(dict(self.ctx), self.school, 'Can a domestic master’s applicant with a 2.30 GPA receive conditional admission at Franklin University?')
        self.assertEqual(result['answer'], answer)
        self.assertFalse(result['pending'])
        self.assertEqual(list(AgentQuery.objects.values_list('pk', flat=True)), [previous.pk])

    def test_specific_gpa_exception_does_not_disable_escalation(self):
        from pure_multi_agent.answer_checks import general_conditional_policy
        evidence = {'facts': [{'content': 'Domestic master applicants with GPA between 2.250 and 2.499 receive conditional admission.',
                              'source_url': 'https://example.edu/admissions'}]}
        self.assertIsNone(general_conditional_policy(
            'Can a domestic master’s applicant with a 2.30 GPA receive conditional admission despite a prior dismissal?', evidence))

    def test_old_deposit_is_labelled_and_real_money_query_is_created_once(self):
        from agent_queries.models import AgentQuery
        from universities.models import KnowledgeGroup
        from pure_multi_agent.registered_adviser import _consult
        money = KnowledgeGroup.objects.create(university=self.school, slug='money')
        KnowledgeGroup.objects.create(university=self.school, slug='international')
        url = 'https://www.franklin.edu/fees'
        evidence = {'facts': [{'content': '2025–2026 F-1 new-student deposit $2,000 applied to first term tuition.', 'source_url': url}]}
        answer = 'Historical rate for 2025–2026: the F-1 new-student deposit was $2,000, applied to first term tuition. The current rate is not confirmed. Source: ' + url

        def generate(messages, tools, **kwargs):
            with self.assertRaisesRegex(ValueError, 'historical|unverified'):
                kwargs['response_validator'](AIMessage(content='The F-1 new-student deposit is $2,000. Source: ' + url))
            with self.assertRaisesRegex(ValueError, 'Money, not Admissions'):
                kwargs['response_validator'](AIMessage(content=answer + ' Please contact Franklin University Admissions Office at admissions@franklin.edu.'))
            reply = AIMessage(content=answer)
            kwargs['response_validator'](reply)
            return reply

        with patch('pure_multi_agent.tools.university_tools._university_evidence', return_value=evidence), \
             patch('pure_multi_agent.registered_adviser.invoke', side_effect=generate), \
             patch('agents.student_context.university_context', return_value={}), \
             patch('agent_queries.services.notify_query') as notify:
            for _ in range(2):
                result = _consult(dict(self.ctx), self.school, 'What is the F-1 new-student deposit?')
        self.assertTrue(result['pending'])
        self.assertIn('Money department', result['answer'])
        self.assertIn('pending a university response', result['answer'])
        self.assertEqual(AgentQuery.objects.get().group, money)
        notify.assert_called_once()

    def test_routing_retains_original_tuition_topic_after_model_rephrasing(self):
        from agent_queries import services
        from universities.models import KnowledgeGroup
        from agent_queries.models import AgentQuery
        money = KnowledgeGroup.objects.create(university=self.school, slug='money')
        KnowledgeGroup.objects.create(university=self.school, slug='admissions')
        conversation = services.conversation_for(str(self.student.uuid), str(self.school.uuid))
        with patch('agent_queries.services.notify_query'):
            result = services.raise_query(conversation, AgentQuery.Direction.STUDENT,
                'Please confirm the standard amount for this intake.',
                routing_question='What is standard master’s tuition for 2026–2027?')
        self.assertEqual(AgentQuery.objects.get().group, money)
        self.assertEqual(result['department'], 'money')

    def test_loaded_general_gpa_policy_precedes_programme_pages(self):
        from django_api.models import UniversityKnowledgeEntry
        from knowledge.university_kb import UniversityKnowledgeBase
        for i in range(12):
            UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid),
                topic='Domestic conditional admission GPA master programme',
                content='Domestic master applicants can ask about GPA and conditional admission.',
                source_url=f'https://www.franklin.edu/degrees/{i}', source_type='manual')
        policy = UniversityKnowledgeEntry.objects.create(university_id=str(self.school.uuid),
            topic='Master admission requirements', source_type='manual',
            content='Domestic applicants with GPA between 2.250 and 2.499 receive conditional admission if they meet other admission requirements.',
            source_url='https://www.franklin.edu/admissions/admission-requirements/master-degrees')
        rows = UniversityKnowledgeBase(str(self.school.uuid), lazy=True).search(
            'Can a domestic master’s applicant with a 2.30 GPA receive conditional admission?')
        self.assertEqual(rows[0].db_id, policy.pk)
