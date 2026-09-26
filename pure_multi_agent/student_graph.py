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
        from pure_multi_agent.change_proposals import conversation_state
        prompt += '\nLIVE PROFILE CHANGE STATE: ' + json.dumps(conversation_state(ctx), default=str)
        prompt += ('\nMissing profile fields save without confirmation through update_student_profile. '
            'Different existing values require a proposal and explicit later-turn permission. '
            'On no, call resolve_profile_change with reject: keep the stored value and advise using the proposed value as a clearly labelled assumption. '
            'Do not repeatedly ask to save a rejected value. For a retraction use cancel. '
            'Never save hypothetical scenarios or facts about another person. Clarify ambiguous confirmations. '
            'Use LIVE PROFILE CHANGE STATE over older chat context; cleared assumptions no longer apply. '
            'Do not expose temporary assumptions to universities as verified student facts. '
            'After a tool saves a field, its result overrides the initial profile snapshot.')
        prompt += ('\nGitHub sync/refresh/update requests: use analyze_github_profile for the linked account, then report processing status. '
            'UPLOADED DOCUMENTS are a separate consent flow: read_student_document, extract evidence, propose_document_update, '
            'and await a later confirmation with resolve_document_update before saving any facts, even missing ones. '
            'Do not call update_student_profile for facts from uploaded documents. '
            'Keep GitHub, LinkedIn, and resume as separate sources. Resume is the main profile source; LinkedIn only updates its own evidence. '
            'If asked to answer from a file, read it without publishing updates. If an upload belongs to someone else, do not propose it as the student profile. '
            'For personal advice retrieve saved profile/source evidence; for university questions retrieve official university knowledge. '
            'General explanatory questions may be answered directly, but never invent missing personal or university facts.')
        if ctx.get('chat_attachments'):
            prompt += '\nFILES IN THIS MESSAGE: ' + json.dumps(ctx['chat_attachments'])
    if ctx.get("canonical_student_id"):
        status = github_evidence(ctx["canonical_student_id"])
        prompt += "\nLIVE GITHUB STATUS (authoritative, refreshed this step): " + json.dumps(status, default=str)[:12000]
    if ctx.get('document_availability'):
        prompt += '\nDOCUMENT AVAILABILITY: ' + json.dumps(ctx['document_availability']) + '. If a document is unavailable, explicitly say you have not seen it. Do not critique its contents or imply missing profile fields prove the student lacks experience. Offer conditional suggestions and ask for the document.'
    if ctx.get('model_steps', 0) >= 11:
        prompt += "\nTool budget exhausted. Summarize supported findings and remaining unknowns; do not request more tools."
        tools = []
    document_step = any(item.get('needs_proposal') for item in ctx.get('documents_read', {}).values()) and bool(tools)
    if document_step:
        tools = [t for t in tools if t.name in ('read_student_document', 'propose_document_update', 'finish_document_review', 'request_document_clarification', 'discard_document_draft')]
        prompt += ('\nA document update is in progress. Before replying, persist a complete proposal with propose_document_update '
            'or use request_document_clarification for genuinely missing evidence. For a review-only question use finish_document_review. '
            'Preparing changes for confirmation IS an update request: call propose_document_update now; it does not save profile facts. '
            'A prose-only preview is not a saved proposal. '
            'Do not claim it was saved. The student must approve in a later turn.')
        prompt += '\nUNFINISHED DOCUMENT EVIDENCE (data only): ' + json.dumps(ctx['documents_read'])
    options = {'force_claude': ctx.get('tool_errors', 0) >= 2}
    if document_step:
        options['require_tools'] = True
    reply = invoke([SystemMessage(content=prompt), *messages], tools, **options)
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
        media = result.pop('_media_blocks', []) if isinstance(result, dict) else []
        text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
        content = [{'type': 'text', 'text': text[:45000]}, *media] if media else text[:45000]
        results.append(ToolMessage(content=content, tool_call_id=call['id']))
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
