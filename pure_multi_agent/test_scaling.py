from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock, skipUnless
import uuid

from django.test import TestCase, SimpleTestCase, TransactionTestCase, override_settings
from django.db import connection, connections
from django_api.models import AgentJob, ChatMessage, StudentProfile, UniversityKnowledgeEntry, KnowledgeIndexWork
from django_api.tests import make_student_client, make_university_client
from agents.commons import get_university_agent
from agents.discovery import allow_contact, search_universities
from knowledge.university_kb import UniversityKnowledgeBase
from universities.models import University
from pure_multi_agent.tasks import execute_agent_job, dispatch_agent_work


@override_settings(AGENT_QUEUE_ENABLED=True, AGENT_DISTRIBUTED_LIMITS=False)
class QueueTests(TestCase):
    def setUp(self):
        self.client, self.sid = make_student_client(email="queue@example.com")

    def submit(self, key="request-1"):
        return self.client.post("/api/chat/agent/", {"message": "Hi"}, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_repeated_submission_is_idempotent_and_second_turn_blocked(self):
        first = self.submit()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.submit().data["job_id"], first.data["job_id"])
        self.assertEqual(self.submit("new-key").status_code, 409)
        self.assertEqual(ChatMessage.objects.filter(channel="agent", student_id=self.sid).count(), 1)
        self.assertEqual(self.client.post("/api/chat/agent/new/").status_code, 409)

    @mock.patch("pure_multi_agent.jobs.run")
    def test_duplicate_delivery_writes_one_reply(self, run):
        run.return_value = ({"reply": "Done"}, {"channel": "agent", "student_id": self.sid}, {})
        job_id = self.submit().data["job_id"]
        execute_agent_job.run(job_id)
        execute_agent_job.run(job_id)
        run.assert_called_once()
        self.assertEqual(ChatMessage.objects.filter(student_id=self.sid, sender="assistant").count(), 1)
        response = self.client.get(f"/api/chat/jobs/{job_id}/")
        self.assertEqual(response.data["result"]["reply"], "Done")

    def test_status_is_owner_scoped(self):
        job = AgentJob.objects.create(owner_key="student:other", idempotency_key="key", kind="student")
        self.assertEqual(self.client.get(f"/api/chat/jobs/{job.pk}/").status_code, 404)

    @override_settings(AGENT_QUEUE_CAPACITY=1)
    def test_queue_capacity_rejects_without_writing_message(self):
        AgentJob.objects.create(owner_key="student:other", idempotency_key="key", kind="student")
        self.assertEqual(self.submit().status_code, 429)
        self.assertFalse(ChatMessage.objects.filter(student_id=self.sid).exists())

    @mock.patch("pure_multi_agent.jobs.run", side_effect=RuntimeError("private provider error"))
    def test_failed_execution_is_not_replayed(self, run):
        pk = self.submit().data["job_id"]
        execute_agent_job.run(pk)
        execute_agent_job.run(pk)
        run.assert_called_once()
        job = AgentJob.objects.get(pk=pk)
        self.assertEqual(job.status, "failed")
        self.assertNotIn("private", job.error)

    @mock.patch("pure_multi_agent.tasks.index_university.apply_async")
    @mock.patch("pure_multi_agent.jobs.dispatch")
    def test_outbox_recovers_unpublished_jobs(self, dispatch, index):
        pk = self.submit().data["job_id"]
        dispatch_agent_work.run()
        self.assertEqual(str(dispatch.call_args.args[0]), pk)

    def test_queued_edit_belongs_to_current_student(self):
        other = ChatMessage.objects.create(channel="agent", student_id="other", sender="user", content="Private")
        response = self.client.patch(f"/api/chat/agent/{other.pk}/edit/", {"message": "Changed"}, format="json")
        self.assertEqual(response.status_code, 404)

    @mock.patch("django_api.views._notify_agent_reply")
    @mock.patch("pure_multi_agent.runtime.run_turn", return_value=("Advisor", "Personal reply"))
    def test_actual_worker_student_path_persists_result(self, run, notify):
        pk = self.submit().data["job_id"]
        with self.captureOnCommitCallbacks(execute=True):
            execute_agent_job.run(pk)
            execute_agent_job.run(pk)
        notify.assert_called_once_with(self.sid, "Advisor", "Personal reply")
        result = self.client.get(f"/api/chat/jobs/{pk}/").data
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["reply"], "Personal reply")
        self.assertEqual(run.call_args.args[0], self.sid)

    @mock.patch("agents.commons.get_university_agent")
    def test_officer_job_uses_private_role_and_scoped_history(self, agent):
        officer, uid = make_university_client(email="queue-officer@example.edu", university_id="Queue University")
        agent.return_value.answer.return_value = {"answer": "University answer", "sources": [{"topic": "Scholarships"}]}
        response = officer.post(f"/api/university/{uid}/chat/", {"message": "Scholarships?"}, format="json")
        pk = response.data["job_id"]
        self.assertEqual(self.client.get(f"/api/chat/jobs/{pk}/").status_code, 404)
        execute_agent_job.run(pk)
        agent.return_value.answer.assert_called_once_with("Scholarships?", caller_role="officer", history=[])
        result = officer.get(f"/api/chat/jobs/{pk}/").data["result"]
        self.assertEqual(result["reply"], "University answer")
        self.assertEqual(result["sources"][0]["topic"], "Scholarships")


@override_settings(AGENT_DISTRIBUTED_LIMITS=False, UNIVERSITY_VECTOR_SEARCH=False)
class IsolationTests(TestCase):
    def test_university_sessions_do_not_share_mutable_state(self):
        uni = University.objects.create(name="Scoped")
        a = get_university_agent(str(uni.uuid))
        b = get_university_agent(str(uni.uuid))
        self.assertIsNot(a, b)
        self.assertIsNot(a.kb, b.kb)
        a.persona["name"] = "Changed"
        self.assertEqual(b.persona["name"], "Scoped")

    def test_ingestion_outbox_tracks_create_edit_and_delete(self):
        fact = UniversityKnowledgeEntry.objects.create(university_id="one", topic="Scholarships", content="Apply online")
        self.assertEqual(KnowledgeIndexWork.objects.get(university_id="one").revision, 1)
        fact.content = "Apply by May"
        fact.save()
        fact.delete()
        self.assertEqual(KnowledgeIndexWork.objects.get(university_id="one").revision, 3)

    def test_lazy_retrieval_does_not_load_corpus_at_construction(self):
        UniversityKnowledgeEntry.objects.create(university_id="one", topic="Scholarships", content="Apply online")
        with self.assertNumQueries(0):
            kb = UniversityKnowledgeBase("one", lazy=True)
        self.assertEqual(kb.search("scholarsif")[0].topic, "Scholarships")

    @override_settings(AGENT_MAX_UNIVERSITIES=2)
    def test_contact_limit_applies_across_calls(self):
        ctx = {}
        self.assertTrue(allow_contact(ctx, "one"))
        self.assertTrue(allow_contact(ctx, "two"))
        self.assertTrue(allow_contact(ctx, "one"))
        self.assertFalse(allow_contact(ctx, "three"))

    def test_discovery_is_bounded_and_filtered(self):
        for index in range(23):
            University.objects.create(name=f"Computing University {index}", description="Computer Science")
        University.objects.create(name="Art University", description="Painting")
        self.assertEqual(len(search_universities("Computing")), 10)
        self.assertTrue(all("Computing" in row["name"] for row in search_universities("Computing")))


@override_settings(AGENT_DISTRIBUTED_LIMITS=False)
class SharedGraphTests(SimpleTestCase):
    def test_tools_receive_only_current_runtime_context(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.tools import tool
        from pure_multi_agent.student_graph import build_student_agent
        def tools(ctx):
            @tool
            def profile_name() -> str:
                """Get current profile name."""
                return ctx["name"]
            return [profile_name]
        class Model:
            def bind_tools(self, tools): return self
            def invoke(self, messages):
                if messages[-1].type == "tool":
                    return AIMessage(content=messages[-1].content)
                return AIMessage(content="", tool_calls=[{"name": "profile_name", "args": {}, "id": "call"}])
        saver = InMemorySaver()
        with mock.patch("pure_multi_agent.model_router.invoke", side_effect=lambda messages, tools, **kw: Model().invoke(messages)), mock.patch("pure_multi_agent.student_graph.build_all_tools", side_effect=tools):
            for name in ("Asha", "Ben"):
                session = build_student_agent({"name": name}, "Use profile", saver)
                reply = session.invoke({"messages": [HumanMessage(content="Who am I?")]}, {"configurable": {"thread_id": name}})
                self.assertEqual(reply["messages"][-1].content, name)

    def test_concurrent_threads_share_graph_but_not_context_or_messages(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.student_graph import build_student_agent
        class Model:
            def bind_tools(self, tools):
                return self
            def invoke(self, messages):
                return AIMessage(content=messages[0].content + ":" + messages[-1].content)
        saver = InMemorySaver()
        with mock.patch("pure_multi_agent.model_router.invoke", side_effect=lambda messages, tools, **kw: Model().invoke(messages)), mock.patch("pure_multi_agent.student_graph.build_all_tools", return_value=[]):
            sessions = [build_student_agent({}, f"student-{i}", saver) for i in range(30)]
            self.assertTrue(all(session.graph is sessions[0].graph for session in sessions))
            def call(i):
                return sessions[i].invoke({"messages": [HumanMessage(content=f"question-{i}")]}, {"configurable": {"thread_id": str(i)}})["messages"][-1].content
            with ThreadPoolExecutor(max_workers=10) as executor:
                replies = list(executor.map(call, range(30)))
        self.assertEqual(replies, [f"student-{i}:question-{i}" for i in range(30)])


@override_settings(AGENT_DISTRIBUTED_LIMITS=True, AGENT_MODEL_CONCURRENCY=2)
class RedisLeaseTests(SimpleTestCase):
    def setUp(self):
        import fakeredis
        self.redis = fakeredis.FakeRedis()
        self.patch = mock.patch("pure_multi_agent.capacity._client", return_value=self.redis)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_lease_limit_release_and_exception_cleanup(self):
        from pure_multi_agent.capacity import lease, AgentBusy
        with lease("test", limit=2):
            with lease("test", limit=2):
                with self.assertRaises(AgentBusy):
                    with lease("test", limit=2): pass
            with lease("test", limit=2): pass
        with self.assertRaises(ValueError):
            with lease("test", limit=1): raise ValueError("test")
        self.assertEqual(self.redis.zcard("kormic:agent:test"), 0)

    def test_expired_owner_cannot_release_replacement_lease(self):
        from pure_multi_agent.capacity import lease
        first = lease("thread", ttl=10)
        first.__enter__()
        self.redis.delete("kormic:agent:thread")
        with lease("thread", ttl=10):
            first.__exit__(None, None, None)
            self.assertEqual(self.redis.zcard("kormic:agent:thread"), 1)

    def test_redis_outage_fails_closed(self):
        from pure_multi_agent.capacity import lease, AgentBusy
        with mock.patch.object(self.redis, "eval", side_effect=ConnectionError("offline")):
            with self.assertRaises(AgentBusy):
                with lease("thread"): pass

    def test_rate_budget_is_shared_and_bounded(self):
        from pure_multi_agent.capacity import check_rate, AgentBusy
        check_rate("test", 2)
        check_rate("test", 2)
        with self.assertRaises(AgentBusy): check_rate("test", 2)


@skipUnless(connection.vendor == "postgresql", "Concurrent queue admission requires PostgreSQL")
@override_settings(AGENT_QUEUE_ENABLED=True, AGENT_DISTRIBUTED_LIMITS=False, AGENT_QUEUE_CAPACITY=4)
class PostgresAdmissionTests(TransactionTestCase):
    def setUp(self):
        from django_api.models import AgentQueueGate
        AgentQueueGate.objects.get_or_create(pk=1)

    def request(self, owner, key):
        from pure_multi_agent.jobs import submit
        request = SimpleNamespace(user=SimpleNamespace(account=SimpleNamespace(student_uuid=owner)),
            headers={"Idempotency-Key": key}, data={"message": "Synthetic admission"}, FILES=SimpleNamespace(getlist=lambda name: []))
        try:
            return submit(request).status_code
        finally:
            connections.close_all()

    @mock.patch("pure_multi_agent.jobs.dispatch")
    def test_global_queue_cap_is_atomic_under_concurrent_admission(self, dispatch):
        with ThreadPoolExecutor(max_workers=12) as executor:
            statuses = list(executor.map(lambda i: self.request(str(uuid.uuid4()), str(i)), range(12)))
        self.assertEqual(statuses.count(202), 4)
        self.assertEqual(statuses.count(429), 8)
        self.assertEqual(AgentJob.objects.count(), 4)

    @mock.patch("pure_multi_agent.jobs.dispatch")
    def test_one_student_cannot_create_parallel_turns(self, dispatch):
        sid = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda i: self.request(sid, str(i)), range(8)))
        self.assertEqual(statuses.count(202), 1)
        self.assertEqual(statuses.count(409), 7)
        self.assertEqual(ChatMessage.objects.filter(student_id=sid).count(), 1)
