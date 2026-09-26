"""No-network synthetic runtime/isolation benchmark, not a production SLA test."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from django.core.management.base import BaseCommand
from django.test import override_settings


class Command(BaseCommand):
    help = "Exercise shared LangGraph with synthetic model latency; no real students or Claude calls."

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=500)
        parser.add_argument("--concurrency", type=int, default=50)
        parser.add_argument("--model-delay-ms", type=int, default=20)

    def handle(self, **options):
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.student_graph import build_student_agent
        count = max(1, options["students"])
        delay = max(0, options["model_delay_ms"]) / 1000
        class Model:
            def bind_tools(self, tools): return self
            def invoke(self, messages):
                time.sleep(delay)
                return AIMessage(content=messages[0].content + ":" + messages[-1].content)
        saver = InMemorySaver()
        durations = []
        graphs = set()
        def call(i):
            session = build_student_agent({}, f"student-{i}", saver)
            begin = time.perf_counter()
            answer = session.invoke({"messages": [HumanMessage(content=f"question-{i}")]}, {"configurable": {"thread_id": str(i)}})
            assert answer["messages"][-1].content == f"student-{i}:question-{i}", "Cross-student context leak"
            return time.perf_counter() - begin, id(session.graph)
        begin = time.perf_counter()
        with override_settings(AGENT_DISTRIBUTED_LIMITS=False, TESTING=True), patch("pure_multi_agent.model_router.invoke", side_effect=lambda messages, tools, **kw: Model().invoke(messages)), patch("pure_multi_agent.student_graph.build_all_tools", return_value=[]):
            with ThreadPoolExecutor(max_workers=max(1, options["concurrency"])) as executor:
                for duration, graph in executor.map(call, range(count)):
                    durations.append(duration)
                    graphs.add(graph)
        elapsed = time.perf_counter() - begin
        durations.sort()
        self.stdout.write(json.dumps({"mode": "synthetic-runtime-only", "students": count, "concurrency": options["concurrency"],
            "graph_instances": len(graphs), "isolated_replies": len(durations), "elapsed_seconds": round(elapsed, 3),
            "p95_seconds": round(durations[min(count - 1, int(count * .95))], 3),
            "excludes": ["HTTP", "Redis", "PostgreSQL", "Claude rate limits", "university tools"]}))
