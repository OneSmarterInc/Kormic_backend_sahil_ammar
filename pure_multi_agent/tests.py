from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase

from django_api.models import AgentConversationLog
from django_api.tests import _reset_inprocess_agent_caches
from pure_multi_agent import prompts
from pure_multi_agent.time_context import current_time_payload
from pure_multi_agent.tools.time_tools import build_tools as build_time_tools
from pure_multi_agent.tools.university_tools import build_tools
from universities.models import University


def _fake_response(payload: dict):
    class FakeBlock:
        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text):
            self.content = [FakeBlock(text)]

    return FakeResponse(json.dumps(payload))


class CurrentDateContextTests(TestCase):
    fixed_utc = datetime(2026, 9, 4, 7, 0, 0, tzinfo=timezone.utc)

    def test_runtime_prompt_contains_authoritative_current_date(self):
        prompt = prompts.build_runtime_system_prompt(
            agent_name="TestAgent",
            student_profile={"name": "Tester"},
            memory={},
            response_mode="short",
            now_utc=self.fixed_utc,
        )
        normalized_prompt = " ".join(prompt.split())

        self.assertIn("CURRENT DATE/TIME — AUTHORITATIVE RUNTIME CONTEXT", prompt)
        self.assertIn("Timezone: Asia/Kolkata", prompt)
        self.assertIn("Current date: Friday, September 4, 2026", prompt)
        self.assertIn(
            "Never infer the current date from model training knowledge",
            normalized_prompt,
        )

    def test_saved_student_timezone_overrides_default(self):
        payload = current_time_payload(
            {"timezone": "America/New_York"},
            now_utc=self.fixed_utc,
        )

        self.assertEqual(payload["timezone"], "America/New_York")
        self.assertEqual(payload["date"], "Friday, September 4, 2026")
        self.assertEqual(payload["time"], "03:00:00")

    def test_invalid_timezone_falls_back_to_utc(self):
        payload = current_time_payload(
            {"timezone": "Definitely/Not-A-Timezone"},
            now_utc=self.fixed_utc,
        )

        self.assertEqual(payload["timezone"], "UTC")
        self.assertEqual(payload["time"], "07:00:00")

    @mock.patch("pure_multi_agent.tools.time_tools.current_time_payload")
    def test_current_datetime_tool_uses_authoritative_clock_helper(self, mock_payload):
        mock_payload.return_value = {
            "timezone": "Asia/Kolkata",
            "date": "Friday, September 4, 2026",
            "time": "12:30:00",
            "iso": "2026-09-04T12:30:00+05:30",
            "utc_iso": "2026-09-04T07:00:00+00:00",
        }
        tool = build_time_tools({"student_profile": {"name": "Tester"}})[0]

        result = json.loads(tool.invoke({"timezone_name": "Asia/Kolkata"}))

        self.assertEqual(result["date"], "Friday, September 4, 2026")
        mock_payload.assert_called_once_with(
            {"name": "Tester"},
            timezone_name="Asia/Kolkata",
        )


class AskUniversityToolConversationLoggingTests(TestCase):
    """
    A3: ask_university is the actual student-agent -> university-agent
    boundary the student's personal agent calls through -- the
    AgentConversationLog row must get written here, not just be reachable
    from the lower-level UniversityAgent.answer() it wraps.
    """

    def setUp(self):
        _reset_inprocess_agent_caches()
        u = University.objects.create(name="Tool Log University", agent_name="Nova5")
        self.university_id = str(u.uuid)
        self.ctx = {
            "canonical_student_id": "student_tool_log",
            "student_profile": {"student_id": "student_tool_log", "name": "Tester"},
        }
        self.tools = {t.name: t for t in build_tools(self.ctx)}

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_ask_university_logs_conversation(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "The deadline is March 1.",
            "confidence": 0.9,
            "unsupported_topics": [],
        })

        result = self.tools["ask_university"].invoke({
            "university_id": self.university_id,
            "question": "What is the deadline?",
        })

        self.assertIn("March 1", result)
        log = AgentConversationLog.objects.get()
        self.assertEqual(log.asker.owner_id, "student_tool_log")
        self.assertEqual(log.responder.owner_id, self.university_id)
        self.assertEqual(log.question, "What is the deadline?")
        self.assertIn("March 1", log.answer)
        self.assertEqual(log.confidence, 0.9)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_unknown_university_id_does_not_log(self, mock_client):
        result = self.tools["ask_university"].invoke({
            "university_id": "no_such_university",
            "question": "What is the deadline?",
        })

        self.assertIn("Unknown university_id", result)
        self.assertEqual(AgentConversationLog.objects.count(), 0)
