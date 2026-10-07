from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from django_api.models import ChatMessage, PendingQuery, StudentProfile


class AgentChatUpdatesTests(TestCase):
    def make_client(self, name):
        student = StudentProfile.objects.create(name=name)
        user = User.objects.create_user(username=name, email=f"{name}@example.edu")
        Account.objects.create(user=user, role=Account.Role.STUDENT, student_profile=student)
        TOTPDevice.objects.create(user=user, secret_encrypted="unused", confirmed_at=timezone.now())
        client = APIClient()
        client.force_authenticate(user=user)
        return client, str(student.uuid)

    def setUp(self):
        self.client, self.student_id = self.make_client("first")
        self.other_client, self.other_id = self.make_client("second")
        self.query = PendingQuery.objects.create(
            student_id=self.student_id, university_id="university-one", question="Is funding available?",
        )
        self.other_query = PendingQuery.objects.create(
            student_id=self.other_id, university_id="university-two", question="Private question",
        )
        self.old = ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=self.student_id,
            sender=ChatMessage.Sender.ASSISTANT, content="I will check with the university.",
            meta={"type": "escalation_pending", "query_id": self.query.pk},
        )

    def test_poll_returns_new_messages_and_status_for_old_bubble(self):
        self.query.status = PendingQuery.Status.RESOLVED
        self.query.save(update_fields=["status"])
        answer = ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=self.student_id,
            sender=ChatMessage.Sender.ASSISTANT, content="The university answered your question.",
            meta={"type": "pending_query_resolved", "query_id": self.query.pk},
        )
        ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=self.other_id,
            sender=ChatMessage.Sender.ASSISTANT, content="Another student's answer",
        )
        response = self.client.get("/api/chat/agent/updates/", {
            "after_id": self.old.pk,
            "query_ids": f"{self.query.pk},{self.other_query.pk}",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["messages"]], [answer.pk])
        self.assertEqual(response.data["messages"][0]["escalation"], {
            "query_id": self.query.pk, "status": "resolved",
        })
        self.assertEqual(response.data["escalations"], {
            str(self.query.pk): "resolved", str(self.other_query.pk): "unknown",
        })
        self.assertEqual(response.data["last_id"], answer.pk)
        self.assertFalse(response.data["has_more"])

        with CaptureQueriesContext(connection) as queries:
            unchanged = self.client.get("/api/chat/agent/updates/", {
                "after_id": answer.pk, "query_ids": str(self.query.pk),
            })
        self.assertEqual(unchanged.data["messages"], [])
        self.assertEqual(unchanged.data["escalations"], {str(self.query.pk): "resolved"})
        self.assertFalse(any("COUNT(" in query["sql"].upper() for query in queries))
        self.assertFalse(any("chatattachment" in query["sql"].lower() for query in queries))

        full = self.client.get("/api/chat/agent/history/")
        self.assertEqual(full.data["messages"][0]["escalation"]["status"], "resolved")

    def test_poll_is_bounded_and_can_continue_from_last_id(self):
        for index in range(55):
            ChatMessage.objects.create(
                channel=ChatMessage.Channel.AGENT, student_id=self.student_id,
                sender=ChatMessage.Sender.ASSISTANT, content=f"Update {index}",
            )
        first = self.client.get("/api/chat/agent/updates/", {"after_id": self.old.pk})
        self.assertEqual(len(first.data["messages"]), 50)
        self.assertTrue(first.data["has_more"])
        second = self.client.get("/api/chat/agent/updates/", {
            "after_id": first.data["last_id"],
        })
        self.assertEqual(len(second.data["messages"]), 5)
        self.assertFalse(second.data["has_more"])
        self.assertFalse({row["id"] for row in first.data["messages"]} &
                         {row["id"] for row in second.data["messages"]})

    def test_invalid_poll_parameters_are_rejected(self):
        for params in ({"after_id": -1}, {"after_id": "not-an-id"},
                       {"query_ids": "1,nope"}, {"query_ids": "0"},
                       {"query_ids": ",".join(str(i) for i in range(201))}):
            self.assertEqual(self.client.get("/api/chat/agent/updates/", params).status_code, 400)
