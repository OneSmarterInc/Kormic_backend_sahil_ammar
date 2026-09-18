from datetime import timedelta
from decimal import Decimal
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.db import connection, close_old_connections
from django.utils import timezone
from rest_framework.test import APIClient
from accounts.models import Account, TOTPDevice
from django_api.models import StudentProfile, ChatGeneration, ChatModelCall, ChatMessage, ChatLease
from django_api.chat_policy import TurnBudget, TurnStopped, current_budget, metered_call, university_slot
from django_api.chat_tasks import generate_chat


def fixture(name="one"):
    user = User.objects.create_user(username=name, email=name + "@example.com")
    profile = StudentProfile.objects.create(name=name)
    Account.objects.create(user=user, role="student", student_profile=profile)
    TOTPDevice.objects.create(user=user, secret="JBSWY3DPEHPK3PXP", confirmed_at=timezone.now())
    client = APIClient()
    client.force_authenticate(user)
    return user, profile, client


class ChatJobTests(TestCase):
    def setUp(self):
        self.user, self.profile, self.client = fixture()
        self.publisher = patch("django_api.chat_tasks.generate_chat.apply_async").start()
        self.addCleanup(patch.stopall)

    def submit(self, message="Hello"):
        response = self.client.post("/api/chat/agent/", {"message": message}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        return ChatGeneration.objects.get(pk=response.data["job_id"])

    def run_job(self, job, error=None):
        module = ModuleType("pure_multi_agent.runtime")
        module.run_turn = Mock(return_value=("Aria", "Hello back"), side_effect=error)
        module.seed_conversation = Mock()
        with patch.dict(sys.modules, {"pure_multi_agent.runtime": module}), patch("django_api.views._notify_agent_reply"):
            generate_chat(str(job.pk))
        job.refresh_from_db()
        return module

    def test_submit_is_async_and_result_is_owner_scoped(self):
        job = self.submit()
        self.assertEqual(job.status, "queued")
        self.publisher.assert_called_once()
        other = fixture("two")[2]
        url = f"/api/chat/agent/jobs/{job.pk}/"
        self.assertEqual(other.get(url).status_code, 404)
        self.run_job(job)
        result = self.client.get(url).data
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reply"], "Hello back")
        self.assertIsNotNone(job.latency_ms)

    def test_concurrent_submit_edit_and_clear_are_rejected(self):
        job = self.submit()
        for response in [self.client.post("/api/chat/agent/", {"message": "Again"}),
            self.client.patch(f"/api/chat/agent/{job.message_id}/edit/", {"message": "Edit"}),
            self.client.post("/api/chat/agent/new/")]:
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.data["code"], "CHAT_IN_PROGRESS")
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_timeout_is_a_code_and_is_not_saved_as_success(self):
        job = self.submit()
        self.run_job(job, TurnStopped())
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "CHAT_TIMEOUT")
        self.assertEqual(ChatMessage.objects.filter(sender="assistant").count(), 0)

    def test_duplicate_delivery_does_not_repeat_model_call(self):
        job = self.submit()
        module = self.run_job(job)
        with patch.dict(sys.modules, {"pure_multi_agent.runtime": module}):
            generate_chat(str(job.pk))
        self.assertEqual(module.run_turn.call_count, 1)

    def test_stale_queued_job_cannot_run(self):
        job = self.submit()
        ChatGeneration.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(seconds=65))
        module = self.run_job(job)
        module.run_turn.assert_not_called()

    def test_expired_worker_lease_returns_timeout(self):
        job = self.submit()
        ChatGeneration.objects.filter(pk=job.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(f"/api/chat/agent/jobs/{job.pk}/")
        self.assertEqual(response.data["code"], "CHAT_TIMEOUT")

    def test_request_rate_budget_applies_to_edits(self):
        job = self.submit()
        self.run_job(job)
        with patch.dict(os.environ, {"CHAT_REQUESTS_PER_MINUTE": "1"}):
            response = self.client.patch(f"/api/chat/agent/{job.message_id}/edit/", {"message": "Edit"})
        self.assertEqual(response.data["code"], "CHAT_RATE_LIMIT")

    def test_oversized_input_is_rejected_before_persistence(self):
        response = self.client.post("/api/chat/agent/", {"message": "x" * 8001})
        self.assertEqual(response.status_code, 413)
        self.assertFalse(ChatGeneration.objects.exists())
        self.assertFalse(ChatMessage.objects.exists())

    def test_attachment_count_checked_before_persistence(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        files = [SimpleUploadedFile(f"{i}.png", b"png", content_type="image/png") for i in range(4)]
        response = self.client.post("/api/chat/agent/", {"message": "Hi", "attachments": files}, format="multipart")
        self.assertEqual(response.data["code"], "CHAT_ATTACHMENT_LIMIT")
        self.assertFalse(ChatMessage.objects.exists())

    def test_daily_budget_blocks_before_network_and_keeps_unknown_cost(self):
        job = self.submit()
        token = current_budget.set(TurnBudget(job))
        try:
            provider = Mock(side_effect=TimeoutError())
            with self.assertRaises(TimeoutError):
                metered_call("test-model", {"text": "Hi"}, 100, provider)
            call = ChatModelCall.objects.get()
            self.assertEqual(call.status, "failed_or_unknown")
            self.assertGreater(call.charged_tokens, 0)
            with patch.dict(os.environ, {"CHAT_DAILY_TOKENS": "1"}):
                with self.assertRaises(TurnStopped) as stopped:
                    metered_call("test-model", {}, 100, provider)
            self.assertEqual(stopped.exception.code, "CHAT_DAILY_BUDGET")
            self.assertEqual(provider.call_count, 1)
        finally:
            current_budget.reset(token)

    def test_usage_and_latency_recorded(self):
        job = self.submit()
        token = current_budget.set(TurnBudget(job))
        try:
            usage = SimpleNamespace(model_dump=lambda: {"input_tokens": 40, "output_tokens": 12})
            metered_call("test-model", {}, 100, lambda: SimpleNamespace(usage=usage))
        finally:
            current_budget.reset(token)
        call = ChatModelCall.objects.get()
        self.assertEqual((call.input_tokens, call.output_tokens), (40, 12))
        self.assertEqual(call.status, "completed")
        self.assertGreater(call.estimated_cost_usd, Decimal(0))

    def test_deadline_tool_loop_and_fanout_bounded(self):
        budget = TurnBudget(self.submit())
        budget.deadline = 0
        with self.assertRaises(TurnStopped): budget.check()
        budget = TurnBudget(ChatGeneration.objects.first())
        with patch.dict(os.environ, {"CHAT_MAX_TOOL_CALLS": "1"}):
            budget.tool()
            with self.assertRaises(TurnStopped): budget.tool()
        with self.assertRaises(TurnStopped): budget.universities(["1", "2", "3", "4", "5"])

    def test_university_leases_shared_and_released(self):
        token = current_budget.set(TurnBudget(self.submit()))
        try:
            with patch.dict(os.environ, {"CHAT_UNIVERSITY_CONCURRENCY": "1"}):
                with university_slot("uni"):
                    with self.assertRaises(TurnStopped):
                        with university_slot("uni"): pass
            self.assertEqual(ChatLease.objects.count(), 0)
        finally:
            current_budget.reset(token)

    def test_real_graph_propagates_budget_into_model_and_tool_threads(self):
        from langchain_core.messages import HumanMessage, AIMessage
        from langchain_core.outputs import ChatResult, ChatGeneration as LCGeneration
        from langchain_core.tools import tool
        from langgraph.prebuilt import create_react_agent
        from pure_multi_agent.budget_model import BudgetedChatAnthropic, BudgetCallbacks
        budget = Mock()
        budget.reserve.return_value = (Mock(), Decimal(1), Decimal(1))
        @tool
        def nested_provider() -> str:
            """Call a nested provider."""
            self.assertIs(current_budget.get(), budget)
            metered_call("nested", {}, 10, lambda: SimpleNamespace(usage={"input_tokens": 2, "output_tokens": 1}))
            return "Done"
        model = BudgetedChatAnthropic(model="claude-haiku-4-5-20251001", api_key="test", max_retries=0)
        replies = [ChatResult(generations=[LCGeneration(message=AIMessage(content="",
            tool_calls=[{"name": "nested_provider", "args": {}, "id": "call-one", "type": "tool_call"}]))]),
            ChatResult(generations=[LCGeneration(message=AIMessage(content="Final"))])]
        token = current_budget.set(budget)
        try:
            with patch("langchain_anthropic.ChatAnthropic._generate", side_effect=replies):
                graph = create_react_agent(model, [nested_provider])
                result = graph.invoke({"messages": [HumanMessage(content="Hi")]},
                    config={"callbacks": [BudgetCallbacks()], "max_concurrency": 1})
            self.assertEqual(result["messages"][-1].content, "Final")
            self.assertEqual(budget.reserve.call_count, 3)
            budget.tool.assert_called_once()
        finally:
            current_budget.reset(token)

    def test_multimodal_inputs_use_provider_token_count_before_reservation(self):
        from django_api.chat_policy import count_multimodal_input
        token = current_budget.set(TurnBudget(self.submit()))
        api = Mock(); api.count_tokens.return_value.input_tokens = 1600
        payload = {"model": "test", "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "image", "source": {"data": "long-base64"}}]}]}
        try:
            self.assertEqual(count_multimodal_input(api, payload), 1600)
            self.assertNotIn("max_tokens", api.count_tokens.call_args.kwargs)
        finally:
            current_budget.reset(token)


class ChatAdmissionConcurrencyTests(TransactionTestCase):
    def test_two_workers_admit_only_one_generation(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock test runs in CI")
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from django_api.models import ChatGate
        user, _, _ = fixture()
        ChatGate.objects.create(key="chat-admission")
        barrier = threading.Barrier(2)
        def post():
            close_old_connections()
            client = APIClient(); client.force_authenticate(User.objects.get(pk=user.pk))
            barrier.wait()
            try:
                return client.post("/api/chat/agent/", {"message": "Hi"}).status_code
            finally:
                close_old_connections()
        with patch("django_api.chat_tasks.generate_chat.apply_async"), ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: post(), range(2)))
        self.assertEqual(sorted(statuses), [202, 429])
