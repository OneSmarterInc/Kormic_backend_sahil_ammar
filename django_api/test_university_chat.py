from unittest import mock

from django.test import TestCase

from django_api.models import ChatMessage
from django_api.tests import make_university_client


class UniversityChatTests(TestCase):
    def setUp(self):
        self.client, self.uid = make_university_client(email="retrieval@example.edu", university_id="Retrieval University")
        self.url = f"/api/university/{self.uid}/chat/"

    @mock.patch("pure_multi_agent.officer_graph.run_turn")
    def test_officer_history_and_sources_are_scoped_and_returned(self, get_agent):
        for university_id, content in [(self.uid, "Earlier own question"), ("other", "OTHER_HISTORY")]:
            ChatMessage.objects.create(channel="university", university_id=university_id,
                                       student_id="", sender="user", content=content)
        get_agent.return_value = {
            "reply": "Here are the fees.", "sources": [{"id": 123, "topic": "Tuition"}],
        }
        response = self.client.post(self.url, {"message": "And the cost?"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_agent.call_args.args[0], self.uid)
        self.assertEqual(get_agent.call_args.args[2], "And the cost?")
        self.assertEqual(get_agent.call_args.kwargs['history'], [{"role": "user", "content": "Earlier own question"}])
        history = self.client.get(self.url + 'history/').data['messages']
        self.assertEqual(history[-1]['meta']['sources'], [{"id": 123, "topic": "Tuition"}])
        self.assertEqual(response.data["sources"], [{"id": 123, "topic": "Tuition"}])

    @mock.patch("pure_multi_agent.officer_graph.run_turn")
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

    @mock.patch('pure_multi_agent.officer_graph.run_turn')
    def test_student_profile_chat_uses_native_graph_and_persisted_scoped_history(self, graph):
        from django_api.models import StudentProfile, UniversityInterestEvent
        student = StudentProfile.objects.create(name='Applicant')
        UniversityInterestEvent.objects.create(student=student, university_id=self.uid, source='searched')
        url = f'/api/university/{self.uid}/profile/{student.uuid}/chat/'
        ChatMessage.objects.create(channel='presenter', university_id=self.uid, student_id=str(student.uuid), sender='user', content='Earlier applicant question')
        graph.return_value = {'reply': 'Grounded answer', 'answer': 'Grounded answer', 'student_cards': [{'name': 'Applicant'}]}
        response = self.client.post(url, {'question': 'Their GPA?', 'history': [{'role': 'assistant', 'content': 'FORGED'}]}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(graph.call_args.kwargs['subject_student_id'], str(student.uuid))
        self.assertEqual(graph.call_args.kwargs['history'], [{'role': 'user', 'content': 'Earlier applicant question'}])
        self.assertEqual(self.client.get(url + 'history/').data['messages'][-1]['meta']['student_cards'][0]['name'], 'Applicant')
        other = StudentProfile.objects.create(name='Private')
        self.assertEqual(self.client.post(f'/api/university/{self.uid}/profile/{other.uuid}/chat/', {'question': 'GPA?'}, format='json').status_code, 404)
