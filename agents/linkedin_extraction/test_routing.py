from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from .pipeline import RoutedModel


class LinkedInRoutingTests(SimpleTestCase):
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
