from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from pure_multi_agent.qwen_context import ContextBudgetExceeded
from university_research.models import PublicUniversity, ResearchRun
from university_research.worker import work_once


class ResearchContextBudgetTests(TestCase):
    @patch('university_research.worker.finish_research')
    @patch('university_research.worker.graph_for')
    def test_oversized_page_uses_existing_fallback_instead_of_retrying_same_prompt(self, graph, finish):
        university = PublicUniversity.objects.create(identity_key='context-test', name='Example University', website='https://example.edu')
        run = ResearchRun.objects.create(university=university, available_at=timezone.now())
        graph.return_value.invoke.side_effect = ContextBudgetExceeded('Request exceeds local context')
        self.assertTrue(work_once())
        finish.assert_called_once()
        saved_run, state = finish.call_args.args
        self.assertEqual(saved_run.pk, run.pk)
        self.assertIsInstance(state['messages'][0], dict)
        self.assertEqual(state['pages'], {})
