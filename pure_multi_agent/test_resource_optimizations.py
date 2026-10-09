from django.test import SimpleTestCase
from langchain_core.messages import HumanMessage

from pure_multi_agent.conversation_context import needs_history_lookup, relevant_history
from pure_multi_agent.inference_admission import admission_poll_delay
from pure_multi_agent.reservations import reservation_estimate, claude_output_allowance


class ResourceOptimizationTests(SimpleTestCase):
    def test_adaptive_wait_has_bounded_fallback_checks(self):
        self.assertEqual([admission_poll_delay(value) for value in (0, 1, 5, 15)],
            [0.15, 0.35, 0.65, 1.0])

    def test_reservation_follows_actual_task_output_allowance(self):
        prompt = [HumanMessage(content='Classify this message')]
        self.assertLess(reservation_estimate(prompt, profile='routing'),
            reservation_estimate(prompt, profile='general'))
        self.assertEqual(claude_output_allowance('routing'), 512)
        self.assertGreater(reservation_estimate([HumanMessage(content=[
            {'type': 'image_url', 'image_url': 'data:image/png;base64,placeholder'}])], profile='routing'),
            reservation_estimate(prompt, profile='routing'))

    def test_relevant_history_keeps_old_clarification_without_full_transcript(self):
        rows = [{'speaker': 'Student Agent', 'content': 'I mean a taught master in Computer Science.'}]
        rows += [{'speaker': 'Student Agent', 'content': f'Unrelated campus topic {index}'} for index in range(10)]
        selected = relevant_history(rows, 'Are taught master requirements different?',
            {'target_degree': 'MS', 'target_subject': 'Computer Science'})
        self.assertLessEqual(len(selected), 4)
        self.assertIn('taught master', ' '.join(item['content'] for item in selected))
        self.assertTrue(needs_history_lookup('As discussed earlier, I meant a taught master.'))
        self.assertFalse(needs_history_lookup('What is the IELTS requirement?'))
