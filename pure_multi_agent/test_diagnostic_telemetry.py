from django.test import TestCase
from django_api.models import AgentAuditLog
from pure_multi_agent.telemetry import diagnostic_scope, operation


class DiagnosticTelemetryTests(TestCase):
    def test_failed_diagnostic_is_not_student_activity_and_context_resets(self):
        with self.assertRaises(ValueError):
            with diagnostic_scope(), operation('University Research Agent', 'manual_check'):
                raise ValueError('Diagnostic connection failure')
        self.assertFalse(AgentAuditLog.objects.exists())
        with operation('Common University Agent', 'student_request', student_id='student'):
            pass
        self.assertEqual(AgentAuditLog.objects.count(), 2)

    def test_real_errors_are_still_recorded(self):
        with self.assertRaises(ValueError):
            with operation('University Research Agent', 'research', student_id='student'):
                raise ValueError('Actual connection failure')
        self.assertEqual(AgentAuditLog.objects.filter(action_type='AGENT_COMMUNICATION_ERROR').count(), 1)
