from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from .pipeline import RoutedModel


class LinkedInRoutingTests(SimpleTestCase):
    def test_text_pipeline_uses_extraction_only_and_preserves_validated_sections(self):
        import json
        import tempfile
        from pathlib import Path
        from .pipeline import extract
        from .schemas import ImageObservation
        source = 'Education\nCity College\nDiploma'
        payload = {'education': [{'institution': 'City College', 'degree': 'Diploma', 'evidence': source}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'linkedin.txt'
            path.write_text(source, encoding='utf-8')
            with patch('pure_multi_agent.model_router.invoke', return_value=SimpleNamespace(content=json.dumps(payload))) as paid:
                result = extract([str(path)])
        self.assertTrue(result['education'])
        self.assertTrue(paid.call_count)
        for call in paid.call_args_list:
            self.assertFalse(call.args[0][0].content.startswith('You control'))
        self.assertEqual(result['education'][0]['institution'], 'City College')
        self.assertEqual([step['action'] for step in result['agent_trace'][0]['actions']], ['extract', 'finish'])

    @patch('pure_multi_agent.model_router.invoke')
    def test_json_contract_reaches_primary_router_without_extra_call(self, invoke):
        invoke.return_value = SimpleNamespace(content='{"action":"finish"}')
        RoutedModel().invoke([('human', 'Extract the supplied screenshot')], format='json')
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.kwargs['json_schema'], {'type': 'object'})

    @patch('pure_multi_agent.model_router.invoke')
    def test_invalid_result_falls_back_with_json_contract(self, invoke):
        invoke.side_effect = [SimpleNamespace(content='invalid'), SimpleNamespace(content='{}')]
        RoutedModel().invoke([('human', 'Extract')], format='json')
        self.assertEqual(invoke.call_count, 2)
        self.assertTrue(invoke.call_args.kwargs['force_claude'])
        self.assertEqual(invoke.call_args.kwargs['json_schema'], {'type': 'object'})
