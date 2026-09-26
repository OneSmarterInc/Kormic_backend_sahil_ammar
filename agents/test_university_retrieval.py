from unittest import mock

from django.test import TestCase, override_settings

from agents.tests import _fake_response
from agents.university_agent import UniversityAgent
from agents import identity_registry
from django_api.models import PendingQuery, UniversityKnowledgeEntry
from pure_multi_agent.university_graph import _answer_node
from universities.models import University


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class UniversityContextTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(name="Own University", contact_email="old@example.edu")
        self.other = University.objects.create(name="Other University")
        self.agent = UniversityAgent(str(self.university.uuid), auto_scrape=False)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_answer_uses_fresh_profile_scoped_knowledge_and_history(self, client):
        self.university.contact_email = "current@example.edu"
        self.university.save()
        own = UniversityKnowledgeEntry.objects.create(
            university_id=str(self.university.uuid), topic="Tuition", content="Tuition costs $12000.",
        )
        UniversityKnowledgeEntry.objects.create(
            university_id=str(self.other.uuid), topic="Tuition", content="OTHER_PRIVATE_FACT",
        )
        client.return_value.messages.create.return_value = _fake_response({"answer": "$12000", "confidence": 0.9})
        history = [{"role": "user", "content": "What is the tuition?"}, {"role": "assistant", "content": "$12000"}]
        result = self.agent.answer("Who can I contact about it?", caller_role="officer", history=history)
        request = client.return_value.messages.create.call_args.kwargs
        self.assertEqual(request["messages"][:2], history)
        prompt = request["messages"][-1]["content"]
        self.assertIn("current@example.edu", prompt)
        self.assertIn("Tuition costs $12000", prompt)
        self.assertNotIn("old@example.edu", prompt)
        self.assertNotIn("OTHER_PRIVATE_FACT", prompt + request["system"])
        self.assertEqual(result["sources"][0]["id"], own.pk)

    def test_resolving_another_university_query_is_refused(self):
        query = PendingQuery.objects.create(university_id=str(self.other.uuid), question="Funding?")
        self.assertFalse(self.agent.resolve_pending_query(query.pk, "Wrong answer"))
        query.refresh_from_db()
        self.assertEqual(query.status, "pending")
        self.assertEqual(UniversityKnowledgeEntry.objects.count(), 0)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_new_topic_is_not_drowned_out_by_previous_question(self, client):
        client.return_value.messages.create.return_value = _fake_response({"answer": "Apply online.", "confidence": 0.9})
        question = "what are our policies related to scholarsif"
        with mock.patch.object(self.agent, "_build_relevant_kb_context", return_value=("Scholarships: apply online", [])) as retrieval:
            self.agent.answer(question, caller_role="officer", history=[
                {"role": "user", "content": "Which students are interested and qualified for admission?"},
                {"role": "assistant", "content": "Earlier reply"},
            ])
        retrieval.assert_called_once_with(question)
        current_message = client.return_value.messages.create.call_args.kwargs["messages"][-1]["content"]
        self.assertTrue(current_message.endswith("CURRENT REQUEST TO ANSWER NOW: " + question))

    @mock.patch("agents.university_agent.UniversityAgent._classify_query_urgency", return_value={"priority": "normal", "urgency_reason": "test"})
    def test_same_question_from_different_students_has_separate_followups(self, _urgency):
        first = self.agent.create_pending_query("Funding?", {"student_id": "first", "name": "First"})
        second = self.agent.create_pending_query("Funding?", {"student_id": "second", "name": "Second"})
        self.assertNotEqual(first["query_id"], second["query_id"])
        self.assertEqual(self.agent.create_pending_query("Funding?", {"student_id": "first"})["query_id"], first["query_id"])

    @mock.patch("agents.commons.get_university_agent")
    def test_bot_exchange_history_is_scoped_to_both_participants(self, get_agent):
        uid = str(self.university.uuid)
        for student_id, university_id, question in [
            ("first", uid, "Own earlier question"),
            ("second", uid, "OTHER_STUDENT_PRIVATE"),
            ("first", str(self.other.uuid), "OTHER_UNIVERSITY_EXCHANGE"),
        ]:
            identity_registry.log_conversation(student_id=student_id, university_id=university_id,
                                               question=question, answer="Reply")
        get_agent.return_value.answer.return_value = {"answer": "Current reply"}
        result = _answer_node({"university_id": uid, "question": "And next?", "student_context": {"student_id": "first"}})
        history = get_agent.return_value.answer.call_args.kwargs["history"]
        self.assertEqual(history, [{"role": "user", "content": "Own earlier question"}, {"role": "assistant", "content": "Reply"}])
        self.assertEqual(result["result"]["answer"], "Current reply")
