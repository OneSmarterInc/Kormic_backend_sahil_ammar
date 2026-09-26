from unittest import mock

from django.test import TestCase

from django_api.models import ChatMessage
from django_api.tests import make_university_client


class UniversityChatTests(TestCase):
    def setUp(self):
        self.client, self.uid = make_university_client(email="retrieval@example.edu", university_id="Retrieval University")
        self.url = f"/api/university/{self.uid}/chat/"

    @mock.patch("agents.commons.get_university_agent")
    def test_officer_history_and_sources_are_scoped_and_returned(self, get_agent):
        for university_id, content in [(self.uid, "Earlier own question"), ("other", "OTHER_HISTORY")]:
            ChatMessage.objects.create(channel="university", university_id=university_id,
                                       student_id="", sender="user", content=content)
        get_agent.return_value.answer.return_value = {
            "answer": "Here are the fees.", "sources": [{"id": 123, "topic": "Tuition"}],
        }
        response = self.client.post(self.url, {"message": "And the cost?"}, format="json")
        self.assertEqual(response.status_code, 200)
        get_agent.return_value.answer.assert_called_once_with(
            "And the cost?", caller_role="officer",
            history=[{"role": "user", "content": "Earlier own question"}],
        )
        self.assertEqual(response.data["sources"], [{"id": 123, "topic": "Tuition"}])

    @mock.patch("agents.commons.get_university_agent")
    def test_officer_cannot_chat_as_another_university(self, get_agent):
        from universities.models import University
        other = University.objects.create(name="Other University")
        response = self.client.post(f"/api/university/{other.uuid}/chat/", {"message": "Hi"}, format="json")
        self.assertEqual(response.status_code, 403)
        get_agent.assert_not_called()

    def test_invalid_message_is_rejected(self):
        for message in ["   ", ["tuition"], "x" * 12001]:
            response = self.client.post(self.url, {"message": message}, format="json")
            self.assertEqual(response.status_code, 400)
