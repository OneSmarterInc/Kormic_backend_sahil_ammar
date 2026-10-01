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


def turn_messages(messages):
    """Keep current tool protocol intact; older tool payloads are not chat memory."""
    from langchain_core.messages import AIMessage
    humans = [i for i, message in enumerate(messages) if message.type == 'human']
    if not humans:
        return messages
    latest = humans[-1]
    prior = []
    for message in messages[:latest]:
        if message.type == 'human':
            prior.append(message)
        elif message.type == 'ai' and isinstance(message.content, str) and message.content and not message.tool_calls:
            prior.append(AIMessage(content=message.content[:3000]))
    return [*prior[-6:], *messages[latest:]]


def _reason(state: MessagesState, runtime: Runtime[dict]):
    import json
    from pure_multi_agent.model_router import invoke
    ctx = runtime.context["ctx"]
    if ctx.get('university_clarification'):
        from langchain_core.messages import AIMessage
        choices = ctx['university_clarification']
        return {'messages':[AIMessage(content='Which university do you mean?\n\n' + '\n'.join(
            f"{i+1}. **{c['name']}** — {', '.join(filter(None,[c.get('address'),c.get('country')]))}\n{c['website']}"
            for i,c in enumerate(choices)))]}
    from pure_multi_agent.job_recovery import boundary
    boundary(ctx, ('turn_id', 'documents_read', 'chat_attachments', 'change_proposals', 'university_references',
        'university_candidates', 'university_resolution_turn', 'university_lookup_required', 'university_discovery_pending', 'university_evidence_required', 'university_answer_evidence', 'university_source_search_required', 'university_pages_pending', 'university_fetch_failures', 'university_identity_attempts', 'university_fallback_domains', 'university_discovery_blocked', 'last_provider', 'last_model', 'known_web_urls', 'read_web_pages', 'research_after_reply', 'university_cache_after_reply', 'university_missing_fields', 'completion_reviews', 'model_steps',
        'tool_errors', 'web_search_count', 'pages_read', 'university_reads', 'document_availability'), phase='model')
    if ctx.get('university_discovery_blocked'):
        from langchain_core.messages import AIMessage
        blocked = ctx['university_discovery_blocked']
        return {'messages':[AIMessage(content='I could not retrieve the requested university information just now. ' + blocked['reason'] + '\n\n[Official website](' + blocked['url'] + ')')]}
    tools = build_all_tools(ctx)
    if not ctx.get('university_candidates') or ctx.get('university_resolution_turn') != ctx.get('turn_id'):
        tools = [tool for tool in tools if tool.name not in ('ask_university', 'search_official_university_site', 'get_fit_assessment', 'request_university_refresh')]
    messages = turn_messages(state["messages"])
    prompt = runtime.context["prompt"]
    prompt += ('\nRESPONSE STYLE: Answer the current question directly in natural, professional language. '
        'Use short mobile-friendly paragraphs, concise bullets and brief bold headings when helpful. '
        'Do not dump tool output, raw JSON, scraped navigation, database fields or repetitive source lists. '
        'Never display N/A or null in chat; explain missing requested details naturally. '
        'Do not introduce unrelated missing fields. Keep numbers with their programme, year and currency. '
        'For profile advice, explain the relevant strengths, gaps and next steps, without inventing admission chances.')
    prompt += ("\nFor enrolled-university facts, use ask_university: the university agent owns that information. "
        "Do not bypass that handoff by asking the student for fees, requirements, deadlines or policies. "
        "If the university agent raises a missing-information query, tell the student it is awaiting the university's answer in Queries. "
        "University agents run server-side even when their human users are logged out. "
        "Call ask_university now and return its answer in this turn, never only promise to contact it later. "
        "For follow-ups like 'did they reply/come back?', call university_reply_status first to read actual saved answers and pending queries. "
        "Only an actual saved missing-information query or running research job may be described as waiting. "
        "You may clarify which university/program the student means. Public universities without a registered agent still use official-site research.")
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
        prompt += ('\nGitHub review/advice and requests for previous analysis: use get_github_processing_status and answer from saved_analysis or completed findings now. '
            'A running refresh does not invalidate previous completed evidence. Label its date and distinguish it from current progress. '
            'Only explicit requests for a NEW sync/refresh/rescan should call analyze_github_profile. Never requeue when the student says not to analyze again. '
            'A GitHub review is not a cross-source profile verification request: do not substitute check_profile_verification for it. '
            'Address the student directly. Never repeat tool instructions such as "let me check with the student" as your answer. '
            'If a relevant discrepancy exists, explain the actual expected/found values and ask a concrete question after answering the requested review. '
            'Do not promise proactive completion notifications unless a real notification mechanism was scheduled. '
            'UPLOADED DOCUMENTS are a separate consent flow: read_student_document, extract evidence, propose_document_update, '
            'and await a later confirmation with resolve_document_update before saving any facts, even missing ones. '
            'Do not call update_student_profile for facts from uploaded documents. '
            'Keep GitHub, LinkedIn, and resume as separate sources. Resume is the main profile source; LinkedIn only updates its own evidence. '
            'If asked to answer from a file, read it without publishing updates. If an upload belongs to someone else, do not propose it as the student profile. '
            'For personal advice retrieve saved profile/source evidence; for university questions retrieve official university knowledge. '
            'General explanatory questions may be answered directly, but never invent missing personal or university facts.')
        if ctx.get('chat_attachments'):
            prompt += '\nFILES IN THIS MESSAGE: ' + json.dumps(ctx['chat_attachments'])
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
    prompt += ('\nThe latest named institution overrides previous university and program context. Never equate different institutions because a saved website points elsewhere. Verify identity on the official homepage; report a mismatch instead of using unrelated evidence. For admission eligibility, ask which degree/program if unspecified. Search the resolved official site and read its admissions pages before asserting requirements. Cite the actual official page URLs.\n')
    prompt += ('\nFor a named university, first call list_universities with query set to the name the user supplied. '
        'Leave country and location empty unless the user explicitly supplied them for this search. '
        'Do not infer university location from the student profile. If a registered match is found, call ask_university with its ID. '
        'If a filtered search is empty, retry by name without location filters before concluding no match exists.')
    options = {'force_claude': ctx.get('tool_errors', 0) >= 2 or bool(ctx.get('university_resolution_turn')),
        'require_tools': bool(tools) and ctx.get('model_steps', 0) == 0}
    if ctx.get('university_lookup_required') and tools:
        tools = [tool for tool in tools if tool.name == 'list_universities']
        options.update(require_tools=True, force_claude=True)
        prompt += '\nThe previous consultation used an unresolved university ID. Resolve the institution in the LATEST user message now. Do not answer or offer a different institution.'
    elif ctx.get('university_discovery_pending') and tools:
        tools = [tool for tool in tools if tool.name in ('read_university_webpage', 'identify_university_candidates', 'select_university_candidate')]
        options['require_tools'] = True
        prompt += '\nInternet discovery is unfinished. First identify whether the name matches multiple distinct institutions. For multiple matches call identify_university_candidates with all choices immediately, without reading pages or fetching information; ask the student to select. For one match read its official homepage once, identify and select it. The reader asks Claude directly if scraping fails. Do not stop at the directory miss.'
    elif ctx.get('university_evidence_required') and tools:
        tools = [tool for tool in tools if tool.name == 'ask_university']
        options['require_tools'] = True
        prompt += '\nThe directory result identifies the university but is NOT course/fee/seat evidence. Call ask_university with the resolved ID now before answering. Use only returned sourced facts; missing fees and seats must remain unknown.'
    elif ctx.get('university_source_search_required') and tools:
        tools = [tool for tool in tools if tool.name == 'search_official_university_site']
        options['require_tools'] = True
        prompt += '\nSaved research does not cover the requested topic. Search the selected official domain now; do not end after the cache miss.'
    elif ctx.get('university_pages_pending') and tools:
        tools = [tool for tool in tools if tool.name == 'read_university_webpage']
        options['require_tools'] = True
        prompt += '\nRead the relevant official search result before answering. Search snippets are not verified source evidence.'
    elif ctx.get('university_answer_evidence', {}).get('agent_answer') and not ctx.get('chat_attachments'):
        # Once the requested source lookup has finished, publish the evidence.
        # An optional completion reviewer must not invent unrelated profile work.
        from langchain_core.messages import AIMessage
        from pure_multi_agent.university_grounding import grounded_reply
        return {'messages': [AIMessage(content=grounded_reply(ctx, ''))]}
    if not tools and any(ctx.get(key) for key in ('university_lookup_required', 'university_discovery_pending', 'university_evidence_required')):
        from langchain_core.messages import AIMessage
        return {'messages': [AIMessage(content='I could not retrieve the university details for this request. Please try again; I do not want to give you information about a different institution.')] }
    if document_step:
        options['require_tools'] = True
    from pure_multi_agent.activity import publish
    publish('Preparing your answer…' if ctx.get('model_steps', 0) else 'Thinking…')
    reply = invoke([SystemMessage(content=prompt), *messages], tools, **options)
    if tools and not reply.tool_calls and ctx.get('completion_reviews', 0) < 2:
        from pure_multi_agent.completion import review_completion
        decision = review_completion(messages, reply)
        ctx['completion_reviews'] = ctx.get('completion_reviews', 0) + 1
        if not decision['complete']:
            publish('Checking the information needed for your answer…')
            reply = invoke([SystemMessage(content=prompt + '\nPerform the missing action now: ' + decision['next_action']), *messages],
                tools, require_tools=True, force_claude=options['force_claude'])
    if not reply.tool_calls and isinstance(reply.content, str):
        from pure_multi_agent.university_grounding import grounded_reply
        reply = reply.model_copy(update={'content': grounded_reply(ctx, reply.content)})
    ctx['model_steps'] = ctx.get('model_steps', 0) + 1
    ctx['last_provider'] = reply.response_metadata.get('routing_provider', '')
    ctx['last_model'] = reply.response_metadata.get('routing_model', '')
    return {"messages": [reply]}


def _tools(state: MessagesState, runtime: Runtime[dict]):
    from pure_multi_agent.job_recovery import boundary
    boundary()
    tools = {tool.name: tool for tool in build_all_tools(runtime.context["ctx"])}
    results = []
    # Profile-changing tools run sequentially within a student's turn. Different
    # students run in different jobs; no request context is stored on the graph.
    import json
    import logging
    ctx = runtime.context['ctx']
    for call in state["messages"][-1].tool_calls:
        import time
        started = time.monotonic()
        try:
            from pure_multi_agent.activity import tool_activity
            tool_activity(call['name'])
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
        logging.getLogger(__name__).info('student_tool name=%s elapsed_seconds=%.3f', call['name'], time.monotonic() - started)
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
        return self.graph.invoke(inputs, config=config, context=self.context, durability='sync')

    async def ainvoke(self, inputs, config=None):
        return await self.graph.ainvoke(inputs, config=config, context=self.context, durability='sync')

    def update_state(self, config, values):
        return self.graph.update_state(config, values)


def build_student_agent(ctx: Dict[str, Any], system_prompt: str, checkpointer):
    return StudentSession(_graph(checkpointer), ctx, system_prompt)
