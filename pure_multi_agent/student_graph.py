# pure_multi_agent/student_graph.py
# The student's personal agent, built with langgraph.prebuilt.create_react_agent
# -- the standard LangGraph pattern for "the model decides which tool(s) to
# call, in a loop, until it's ready to answer". This is what replaces the old
# fixed classify-then-branch dispatch in agents.student_agent.StudentAgent.chat().

from __future__ import annotations

from typing import Any, Dict

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent

from pure_multi_agent.tools import build_all_tools

MODEL_NAME = "claude-haiku-4-5-20251001"

_model = None


# Every turn can chain up to `recursion_limit` (see runtime.run_turn) model
# calls in a tool loop. With no timeout, a single hung upstream call ties up
# a Django worker indefinitely -- on the highest-traffic endpoint in the
# system, that's the fastest path to exhausting the whole worker pool.
# timeout bounds each individual call; max_retries=1 keeps the worst case
# for one call predictable (timeout + one retry) instead of the SDK's
# default 2 retries compounding it further.
CHAT_MODEL_TIMEOUT_SECONDS = 120.0


def _get_model() -> ChatAnthropic:
    global _model
    if _model is None:
        _model = ChatAnthropic(
            model=MODEL_NAME,
            max_tokens=1200,
            timeout=CHAT_MODEL_TIMEOUT_SECONDS,
            max_retries=1,
        )
    return _model


def build_student_agent(ctx: Dict[str, Any], system_prompt: str, checkpointer):
    """Build a fresh react-agent graph for this turn. Tools are closures over
    this turn's mutable context dict (see pure_multi_agent.tools), so the
    agent is rebuilt per turn -- cheap, since compilation itself does no I/O.
    The checkpointer is a shared, process-level instance passed in by
    pure_multi_agent.runtime, so conversation history for a given
    thread_id (student id) is retained across these per-turn rebuilds."""
    tools = build_all_tools(ctx)

    return create_react_agent(
        model=_get_model(),
        tools=tools,
        prompt=system_prompt,
        checkpointer=checkpointer,
    )
