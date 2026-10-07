"""Bound officer prompts without discarding the underlying evidence or tools."""
import hashlib
import json

from langchain_core.tools import tool

PAGE_CHARS = 4000
DEFERRED = {
    'propose_knowledge_change': 'Create or update a policy, scholarship, course or deadline.',
    'propose_university_information': 'Change university identity, profile, contacts or agent settings.',
    'propose_admission_requirement': 'Add or update a structured admission requirement.',
    'propose_department_contact': 'Change a department escalation contact.',
}


def prepare(ctx, messages, all_tools):
    # Rebuild from checkpointed messages on every step, including worker recovery.
    # Evidence never crosses conversation/university boundaries or enters a global cache.
    evidence = {}
    projected = []
    tool_positions = [i for i, message in enumerate(messages) if message.type == 'tool']
    latest = tool_positions[-1] if tool_positions else -1
    for index, message in enumerate(messages):
        if message.type != 'tool':
            projected.append(message)
            continue
        if message.name == 'read_officer_evidence':
            if index == latest:
                projected.append(message)
            else:
                page = json.loads(message.content)
                projected.append(message.model_copy(update={'content': json.dumps({
                    key: value for key, value in page.items() if key != 'content'
                })}))
            continue
        content = message.content if isinstance(message.content, str) else json.dumps(message.content)
        reference = hashlib.sha256(content.encode()).hexdigest()[:24]
        evidence[reference] = content
        if len(content) <= PAGE_CHARS and index == latest:
            projected.append(message)
            continue
        # Old tool output remains retrievable; keep call/result pairing intact.
        preview = content[:PAGE_CHARS] if index == latest else ''
        projected.append(message.model_copy(update={'content': json.dumps({
            'evidence_id': reference, 'total_characters': len(content),
            'content': preview, 'next_offset': len(preview),
            'instruction': 'Partial evidence. Use read_officer_evidence for remaining text before '
                           'making exhaustive claims. Do not infer absent facts from this preview.',
        })}))

    @tool
    def read_officer_evidence(evidence_id: str, offset: int = 0) -> dict:
        """Read an exact 4000-character page of a tool result from this conversation.
        Follow next_offset until null when all evidence is required; offsets are characters."""
        if evidence_id not in evidence:
            raise ValueError('Unknown evidence ID in this conversation.')
        content = evidence[evidence_id]
        if offset < 0 or offset > len(content):
            raise ValueError('Offset is outside the evidence.')
        end = min(offset + PAGE_CHARS, len(content))
        return {'evidence_id': evidence_id, 'offset': offset, 'total_characters': len(content),
                'content': content[offset:end], 'next_offset': end if end < len(content) else None}

    @tool
    def enable_officer_tool(name: str) -> dict:
        """Load a proposal tool's full validated schema for the next step.
        This only selects a tool; it never creates, approves or saves a change."""
        if name not in DEFERRED:
            raise ValueError('Choose a proposal tool from the catalog.')
        ctx['officer_proposal_tool'] = name
        return {'enabled': name, 'instruction': 'Read existing records, collect required details, '
                'then use this tool. Explicit later-turn approval is still required.'}

    selected = [item for item in all_tools if item.name not in DEFERRED
                or item.name == ctx.get('officer_proposal_tool')]
    return projected, [*selected, enable_officer_tool, read_officer_evidence]


CATALOG = ('\nPROPOSAL TOOL CATALOG: ' + json.dumps(DEFERRED)
           + '\nCall enable_officer_tool(name) to load the schema of the needed proposal tool. '
           'All proposal validation and later-turn consent rules still apply. '
           'Tool evidence may be paged; use read_officer_evidence to retrieve missing pages. '
           'For older conversation details use read_portal_tab(tab="assistant_chat", query=..., page=...).')
