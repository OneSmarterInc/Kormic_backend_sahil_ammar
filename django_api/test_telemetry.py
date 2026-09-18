import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django_api.test_chat_jobs import fixture
from django_api.models import ChatModelCall
from django_api.telemetry import current_request, standalone_call

class TelemetryTests(TestCase):
    def test_usage_cost_request_id_and_privacy_safe_account_id(self):
        user, _, _=fixture()
        token=current_request.set(SimpleNamespace(user=user,request_id='test-request'))
        self.addCleanup(current_request.reset,token)
        with patch.dict(os.environ, {'MODEL_COST_RATES_JSON':'{"test":{"input":1,"output":2}}'}):
            response=standalone_call('test',lambda:SimpleNamespace(usage={'input_tokens':100,'output_tokens':10}))
        row=ChatModelCall.objects.get(); self.assertEqual(row.request_id,'test-request'); self.assertEqual(row.account_id,user.account.pk)
        self.assertEqual(row.actual_cost_usd,Decimal('.000120')); self.assertEqual(row.input_tokens,100); self.assertIsNotNone(row.latency_ms)
        self.assertFalse(hasattr(row,'prompt')); self.assertFalse(hasattr(row,'email'))
    def test_failure_and_unknown_usage_are_not_zero_cost(self):
        def fail(): raise TimeoutError('private provider message')
        with self.assertRaises(TimeoutError): standalone_call('test',fail)
        row=ChatModelCall.objects.get(); self.assertEqual(row.error_category,'timeout'); self.assertIsNone(row.actual_cost_usd); self.assertTrue(row.request_id)
        standalone_call('test',lambda:SimpleNamespace(usage=None))
        self.assertEqual(ChatModelCall.objects.latest('pk').status,'usage_unknown')
    def test_superuser_aggregate_excludes_account_identifiers(self):
        user,_,client=fixture()
        self.assertEqual(client.get('/api/superuser/metrics/models/').status_code,403)
        account=user.account; account.role='superuser'; account.save()
        standalone_call('test',lambda:SimpleNamespace(usage={'input_tokens':1,'output_tokens':2}))
        response=client.get('/api/superuser/metrics/models/')
        self.assertEqual(response.status_code,200); self.assertEqual(response.data['totals']['model_calls'],1)
        self.assertNotIn('account_id',str(response.data)); self.assertNotIn(user.email,str(response.data))
