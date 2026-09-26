# pure_multi_agent/student_graph.py
# A shared LangGraph ReAct loop. Mutable profile/tools live in Runtime context
# for each invocation; only messages are persisted in the checkpointer.

from __future__ import annotations

from typing import Any, Dict

from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.runtime import Runtime
from langchain_core.messages import SystemMessage, ToolMessage
from threading import Lock
from weakref import WeakKeyDictionary

from pure_multi_agent.tools import build_all_tools

_graphs = WeakKeyDictionary()
_graph_lock = Lock()


def _reason(state: MessagesState, runtime: Runtime[dict]):
    import json
    from pure_multi_agent.model_router import invoke
    from pure_multi_agent.tools.github_tools import github_evidence
    ctx = runtime.context["ctx"]
    tools = build_all_tools(ctx)
    messages = state["messages"]
    human_indices = [i for i, message in enumerate(messages) if message.type == "human"]
    if len(human_indices) > 12:
        messages = messages[human_indices[-12]:]
    prompt = runtime.context["prompt"]
    if ctx.get("canonical_student_id"):
        status = github_evidence(ctx["canonical_student_id"])
        prompt += "\nLIVE GITHUB STATUS (authoritative, refreshed this step): " + json.dumps(status, default=str)[:12000]
    if ctx.get('document_availability'):
        prompt += '\nDOCUMENT AVAILABILITY: ' + json.dumps(ctx['document_availability']) + '. If a document is unavailable, explicitly say you have not seen it. Do not critique its contents or imply missing profile fields prove the student lacks experience. Offer conditional suggestions and ask for the document.'
    if ctx.get('model_steps', 0) >= 11:
        prompt += "\nTool budget exhausted. Summarize supported findings and remaining unknowns; do not request more tools."
        tools = []
    reply = invoke([SystemMessage(content=prompt), *messages], tools, force_claude=ctx.get('tool_errors', 0) >= 2)
    ctx['model_steps'] = ctx.get('model_steps', 0) + 1
    ctx['last_provider'] = reply.response_metadata.get('routing_provider', '')
    return {"messages": [reply]}


def _tools(state: MessagesState, runtime: Runtime[dict]):
    tools = {tool.name: tool for tool in build_all_tools(runtime.context["ctx"])}
    results = []
    # Profile-changing tools run sequentially within a student's turn. Different
    # students run in different jobs; no request context is stored on the graph.
    import json
    import logging
    ctx = runtime.context['ctx']
    for call in state["messages"][-1].tool_calls:
        try:
            result = tools[call['name']].invoke(call['args'])
        except (ValueError, KeyError) as exc:
            ctx['tool_errors'] = ctx.get('tool_errors', 0) + 1
            result = {'error': str(exc)[:300], 'action': 'Correct the arguments or ask the student for missing details.'}
        except Exception:
            logging.getLogger(__name__).exception('Student tool failed: %s', call['name'])
            ctx['tool_errors'] = ctx.get('tool_errors', 0) + 1
            result = {'error': 'This tool is temporarily unavailable. Explain the limitation, use another available source, or ask for missing evidence. Do not invent results.'}
        text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
        results.append(ToolMessage(content=text[:45000], tool_call_id=call['id']))
    return {"messages": results}


def _graph(checkpointer):
    with _graph_lock:
        if checkpointer not in _graphs:
            builder = StateGraph(MessagesState, context_schema=dict)
            builder.add_node("agent", _reason)
            builder.add_node("tools", _tools)
            builder.add_edge(START, "agent")
            builder.add_conditional_edges("agent", lambda state: "tools" if state["messages"][-1].tool_calls else END)
            builder.add_edge("tools", "agent")
            _graphs[checkpointer] = builder.compile(checkpointer=checkpointer)
        return _graphs[checkpointer]


class StudentSession:
    def __init__(self, graph, ctx, prompt):
        self.graph = graph
        self.context = {"ctx": ctx, "prompt": prompt}

    def invoke(self, inputs, config=None):
        return self.graph.invoke(inputs, config=config, context=self.context)

    async def ainvoke(self, inputs, config=None):
        return await self.graph.ainvoke(inputs, config=config, context=self.context)

    def update_state(self, config, values):
        return self.graph.update_state(config, values)


def build_student_agent(ctx: Dict[str, Any], system_prompt: str, checkpointer):
    return StudentSession(_graph(checkpointer), ctx, system_prompt)
