import json
from unittest import mock

from django.core import mail
from django.test import TestCase

from agents import identity_registry
from agents.university_agent import UniversityAgent
from django_api.models import AgentConversationLog, AgentIdentity, PendingQuery
from universities.knowledge_groups import ensure_default_groups
from universities.models import KnowledgeGroup, University


def _fake_response(payload: dict):
    class FakeBlock:
        def __init__(self, text):
            self.text = text

    class FakeResponse:
        def __init__(self, text):
            self.content = [FakeBlock(text)]

    return FakeResponse(json.dumps(payload))


class UniversityAgentPartialEscalationTests(TestCase):
    """
    A compound question ("what are the deadlines and funding") can be
    strongly supported on one topic (deadlines) and have zero knowledge
    base coverage on another (funding). The single overall confidence score
    the model reports reflects the topics it COULD answer, so it can clear
    MIN_CONFIDENCE even though part of the question is unanswered -- that
    gap must still reach the university as a real PendingQuery instead of
    being silently absorbed into a high confidence score and left for the
    student to "email the university" on their own.
    """

    def setUp(self):
        u = University.objects.create(name="Write State", agent_name="Nova2")
        self.university_id = str(u.uuid)
        self.agent = UniversityAgent(self.university_id, auto_scrape=False)
        self.agent.kb.store(
            topic="Application deadlines",
            content="Fall 2025 deadline is March 1; Spring 2026 is October 10.",
            source_type="seed",
            confidence=1.0,
        )

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_compound_question_escalates_unsupported_topic_despite_high_overall_confidence(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": (
                "Deadlines: Fall 2025 is March 1, Spring 2026 is October 10. "
                "Funding isn't documented for Write State."
            ),
            "confidence": 0.85,
            "unsupported_topics": ["funding"],
        })

        result = self.agent.answer("What are the deadlines and funding?")

        self.assertFalse(result["pending"])
        self.assertTrue(result.get("partial_pending"))
        self.assertEqual(result["unsupported_topics"], ["funding"])

        query = PendingQuery.objects.get(id=result["pending_query"]["query_id"])
        self.assertEqual(query.university_id, self.university_id)
        self.assertIn("funding", query.question.lower())

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_fully_supported_answer_does_not_escalate(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "Fall 2025 deadline is March 1.",
            "confidence": 0.9,
            "unsupported_topics": [],
        })

        result = self.agent.answer("What is the fall deadline?")

        self.assertFalse(result["pending"])
        self.assertNotIn("partial_pending", result)
        self.assertEqual(PendingQuery.objects.count(), 0)


class PendingQueryConfidenceTests(TestCase):
    """
    once a question escalates, the confidence score that caused the
    escalation must survive onto the PendingQuery contract instead of being
    dropped -- otherwise a reviewer can't tell a near-miss (0.55) from a
    wild guess (0.1).
    """

    def setUp(self):
        u = University.objects.create(name="Hard State", agent_name="Nova3")
        self.agent = UniversityAgent(str(u.uuid), auto_scrape=False)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_different_confidence_scores_persist_distinctly_on_the_escalation(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "I'm not sure.",
            "confidence": 0.55,
            "unsupported_topics": [],
        })
        low_result = self.agent.answer("What is the exact GRE cutoff?")

        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "No idea.",
            "confidence": 0.1,
            "unsupported_topics": [],
        })
        very_low_result = self.agent.answer("Is there a secret scholarship for left-handed students?")

        self.assertTrue(low_result["pending"])
        self.assertTrue(very_low_result["pending"])
        self.assertEqual(low_result["confidence"], 0.55)
        self.assertEqual(very_low_result["confidence"], 0.1)

        # The escalation record itself carries the real score, not a placeholder.
        self.assertEqual(low_result["pending_query"]["confidence"], 0.55)
        self.assertEqual(very_low_result["pending_query"]["confidence"], 0.1)
        self.assertNotEqual(
            low_result["pending_query"]["confidence"],
            very_low_result["pending_query"]["confidence"],
        )

        # And it's what's actually stored, and what the officer-facing API contract exposes.
        low_query = PendingQuery.objects.get(id=low_result["pending_query"]["query_id"])
        very_low_query = PendingQuery.objects.get(id=very_low_result["pending_query"]["query_id"])
        self.assertEqual(low_query.confidence, 0.55)
        self.assertEqual(very_low_query.confidence, 0.1)

        from django_api.views import serialize_pending_query

        self.assertEqual(serialize_pending_query(low_query)["confidence"], 0.55)
        self.assertEqual(serialize_pending_query(very_low_query)["confidence"], 0.1)


class UniversityAgentOfficerGuardrailTests(TestCase):
    """
    An officer chatting directly with their own agent (previewing/testing
    it, or asking about a gap) is not a student waiting on an answer --
    PendingQuery escalation is reserved for the student -> Aria -> Sol
    chain. Officer calls should never create a PendingQuery; instead they
    get a guardrail response pointing at the knowledge base gap.
    """

    def setUp(self):
        u = University.objects.create(name="Franklin University", agent_name="Sol")
        self.agent = UniversityAgent(str(u.uuid), auto_scrape=False)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_low_confidence_officer_question_does_not_create_pending_query(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "I don't have information about that.",
            "confidence": 0.0,
            "unsupported_topics": [],
        })

        result = self.agent.answer(
            "whats the info about wright state university",
            caller_role="officer",
        )

        self.assertFalse(result["pending"])
        self.assertTrue(result.get("knowledge_gap"))
        self.assertNotIn("pending_query", result)
        self.assertEqual(PendingQuery.objects.count(), 0)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_officer_unsupported_topic_does_not_create_pending_query(self, mock_client):
        self.agent.kb.store(
            topic="Application deadlines",
            content="Fall 2025 deadline is March 1.",
            source_type="seed",
            confidence=1.0,
        )
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "Fall 2025 deadline is March 1. Funding isn't documented.",
            "confidence": 0.85,
            "unsupported_topics": ["funding"],
        })

        result = self.agent.answer(
            "What are the deadlines and funding?",
            caller_role="officer",
        )

        self.assertFalse(result["pending"])
        self.assertNotIn("partial_pending", result)
        self.assertTrue(result.get("knowledge_gap"))
        self.assertEqual(result["unsupported_topics"], ["funding"])
        self.assertEqual(PendingQuery.objects.count(), 0)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_student_call_still_escalates(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "I don't have information about that.",
            "confidence": 0.0,
            "unsupported_topics": [],
        })

        result = self.agent.answer("whats the info about wright state university")

        self.assertTrue(result["pending"])
        self.assertEqual(PendingQuery.objects.count(), 1)


class UniversityAgentEscalationRoutingTests(TestCase):
    """
    A1: an escalation must route to the matching knowledge group's named
    contact, not just create an undifferentiated PendingQuery.
    """

    def setUp(self):
        self.university = University.objects.create(name="Write State", agent_name="Nova3")
        ensure_default_groups(self.university)
        self.money_group = self.university.knowledge_groups.get(slug=KnowledgeGroup.Slug.MONEY)
        self.money_group.escalation_contact_name = "Bursar Office"
        self.money_group.escalation_contact_email = "bursar@wsu.edu"
        self.money_group.save()
        self.agent = UniversityAgent(str(self.university.uuid), auto_scrape=False)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_escalation_routes_to_matching_group_contact(self, mock_client):
        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "I don't know.",
            "confidence": 0.0,
            "unsupported_topics": [],
        })

        result = self.agent.answer("What is the assistantship stipend?")

        query = PendingQuery.objects.get(id=result["pending_query"]["query_id"])
        self.assertEqual(query.group_id, self.money_group.id)
        self.assertEqual(query.routed_to_name, "Bursar Office")
        self.assertEqual(query.routed_to_email, "bursar@wsu.edu")
        self.assertEqual(result["pending_query"]["group"], "money")

        # The group's contact is emailed automatically as the escalation lands.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["bursar@wsu.edu"])
        self.assertIn("routed to Money", mail.outbox[0].subject)
        self.assertIn("What is the assistantship stipend?", mail.outbox[0].body)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_escalation_still_created_when_no_groups_configured(self, mock_client):
        u = University.objects.create(name="No Groups University", agent_name="Nova6")
        agent = UniversityAgent(str(u.uuid), auto_scrape=False)

        mock_client.return_value.messages.create.return_value = _fake_response({
            "answer": "I don't know.",
            "confidence": 0.0,
            "unsupported_topics": [],
        })

        result = agent.answer("What is the deadline?")

        query = PendingQuery.objects.get(id=result["pending_query"]["query_id"])
        self.assertIsNone(query.group)
        self.assertEqual(query.routed_to_email, "")
        # No group -> nobody to notify.
        self.assertEqual(len(mail.outbox), 0)


class AgentIdentityAndConversationLogTests(TestCase):
    """
    A3: every agent gets a durable birth record, and every agent-to-agent
    exchange writes a first-class, queryable log row.
    """

    def setUp(self):
        self.university = University.objects.create(name="Identity University", agent_name="Nova4")
        self.university_id = str(self.university.uuid)

    def test_university_agent_construction_creates_birth_record(self):
        UniversityAgent(self.university_id, auto_scrape=False)

        identity = AgentIdentity.objects.get(owner_type=AgentIdentity.OwnerType.UNIVERSITY, owner_id=self.university_id)
        self.assertEqual(identity.agent_name, "Nova4")

    def test_log_conversation_creates_row_and_lazily_creates_identities(self):
        log = identity_registry.log_conversation(
            student_id="student_abc",
            university_id=self.university_id,
            question="What is the deadline?",
            answer="March 1.",
            knowledge_source="conversation",
            confidence=0.9,
        )

        self.assertIsNotNone(log)
        self.assertEqual(AgentConversationLog.objects.count(), 1)
        self.assertTrue(
            AgentIdentity.objects.filter(owner_type=AgentIdentity.OwnerType.STUDENT, owner_id="student_abc").exists()
        )
        self.assertTrue(
            AgentIdentity.objects.filter(
                owner_type=AgentIdentity.OwnerType.UNIVERSITY, owner_id=self.university_id
            ).exists()
        )

    def test_log_conversation_skips_without_student_id(self):
        result = identity_registry.log_conversation(
            student_id="",
            university_id="identity_u",
            question="q",
            answer="a",
        )

        self.assertIsNone(result)
        self.assertEqual(AgentConversationLog.objects.count(), 0)

    def test_get_or_create_identity_is_idempotent_and_updates_name(self):
        first = identity_registry.student_identity("student_xyz", "Aria")
        second = identity_registry.student_identity("student_xyz", "Nova")

        self.assertEqual(first.agent_id, second.agent_id)
        second.refresh_from_db()
        self.assertEqual(second.agent_name, "Nova")
        self.assertEqual(AgentIdentity.objects.filter(owner_id="student_xyz").count(), 1)
