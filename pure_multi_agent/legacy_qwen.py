"""Qwen-first transport for older Anthropic-shaped agent call sites."""
import uuid
import base64
import io
import logging
from urllib.parse import urlsplit

import httpx
from anthropic.types import Message
from django.conf import settings
from jsonschema import validate
from github_profiles.scheduling import model_slot
from kormic_backend.ollama_config import qwen_keep_alive
from pure_multi_agent.qwen_context import select_context, needs_expansion

logger = logging.getLogger(__name__)


def create(**kwargs):
    messages, call_names = [], {}
    has_document = False
    if kwargs.get('system'):
        messages.append({'role': 'system', 'content': kwargs['system']})
    for message in kwargs['messages']:
        content = message['content']
        if isinstance(content, str):
            messages.append(message)
            continue
        text, calls = [], []
        for block in content:
            kind = block.get('type')
            if kind == 'text':
                text.append(block['text'])
            elif kind == 'tool_use':
                call_names[block['id']] = block['name']
                calls.append({'function': {'name': block['name'], 'arguments': block['input']}})
            elif kind == 'tool_result':
                messages.append({'role': 'tool', 'tool_name': call_names.get(block['tool_use_id'], ''),
                                 'content': str(block['content'])})
            elif kind == 'document' and block.get('source', {}).get('media_type') == 'application/pdf':
                has_document = True
                from pypdf import PdfReader
                document = PdfReader(io.BytesIO(base64.b64decode(block['source']['data'])))
                extracted = '\n'.join(page.extract_text() or '' for page in document.pages)
                if not extracted.strip():
                    raise ValueError('Scanned document requires vision fallback')
                text.append(extracted)
            else:
                raise ValueError('This input requires a vision-capable fallback provider')
        if text or calls:
            row = {'role': message['role'], 'content': '\n'.join(text)}
            if calls:
                row['tool_calls'] = calls
            messages.append(row)
    tools = kwargs.get('tools', []) if kwargs.get('tool_choice', {}).get('type') != 'none' else []
    formatted_tools = [{'type': 'function', 'function': {'name': t['name'],
        'description': t.get('description', ''), 'parameters': t['input_schema']}} for t in tools]
    budget = select_context(messages, formatted_tools,
        profile='document' if has_document else 'general',
        output_tokens=kwargs.get('max_tokens') or 2400)
    body = {'model': settings.GITHUB_OLLAMA_MODEL, 'messages': messages,
            'stream': False, 'think': False, 'keep_alive': qwen_keep_alive(),
            'options': {'num_ctx': budget.num_ctx, 'num_predict': budget.num_predict, 'temperature': 0}}
    if formatted_tools:
        body['tools'] = formatted_tools
    base = settings.GITHUB_OLLAMA_BASE_URL.rstrip('/')
    if urlsplit(base).hostname not in settings.GITHUB_OLLAMA_ALLOWED_HOSTS:
        raise ValueError('Ollama host is not allowed')
    while True:
        body['options']['num_ctx'] = budget.num_ctx
        with model_slot('qwen', None, budget.input_estimate + budget.num_predict):
            response = httpx.post(base + '/api/chat', json=body, trust_env=False,
                                  timeout=httpx.Timeout(120, connect=2))
            response.raise_for_status()
        payload = response.json()
        if payload.get('done_reason') == 'length':
            raise ValueError('Qwen reached the requested output limit before finishing this response')
        if not needs_expansion(budget, payload.get('prompt_eval_count')):
            logger.info('Legacy Qwen context profile=%s window=%s input_estimate=%s prompt_tokens=%s output_reserve=%s',
                budget.profile, budget.num_ctx, budget.input_estimate,
                payload.get('prompt_eval_count'), budget.num_predict)
            break
        budget = select_context(messages, formatted_tools,
            profile=budget.profile, output_tokens=budget.num_predict,
            min_context=budget.num_ctx * 2)
    result = payload['message']
    blocks = []
    if result.get('content'):
        blocks.append({'type': 'text', 'text': result['content']})
    mapping = {t['name']: t for t in tools}
    for call in result.get('tool_calls', []):
        fn = call['function']
        validate(fn['arguments'], mapping[fn['name']]['input_schema'])
        blocks.append({'type': 'tool_use', 'id': 'tool_' + uuid.uuid4().hex,
                       'name': fn['name'], 'input': fn['arguments']})
    if not blocks or (kwargs.get('tool_choice', {}).get('type') in ('any', 'tool') and not result.get('tool_calls')):
        raise ValueError('Qwen did not return the required response')
    return Message(id='qwen_' + uuid.uuid4().hex, type='message', role='assistant',
        model=settings.GITHUB_OLLAMA_MODEL, content=blocks,
        stop_reason='tool_use' if result.get('tool_calls') else 'end_turn',
        usage={'input_tokens': payload.get('prompt_eval_count', 0),
               'output_tokens': payload.get('eval_count', 0)})
