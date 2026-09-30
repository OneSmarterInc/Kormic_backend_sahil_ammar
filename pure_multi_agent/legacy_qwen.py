"""Qwen-first transport for older Anthropic-shaped agent call sites."""
import json
import uuid
import base64
import io
from urllib.parse import urlsplit

import httpx
from anthropic.types import Message
from django.conf import settings
from jsonschema import validate
from github_profiles.scheduling import model_slot


def create(**kwargs):
    messages, call_names = [], {}
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
    body = {'model': settings.GITHUB_OLLAMA_MODEL, 'messages': messages,
            'stream': False, 'think': False, 'keep_alive': -1,
            'options': {'num_ctx': 16384, 'num_predict': kwargs.get('max_tokens', 2400), 'temperature': 0}}
    if tools:
        body['tools'] = [{'type': 'function', 'function': {'name': t['name'],
            'description': t.get('description', ''), 'parameters': t['input_schema']}} for t in tools]
    base = settings.GITHUB_OLLAMA_BASE_URL.rstrip('/')
    if urlsplit(base).hostname not in settings.GITHUB_OLLAMA_ALLOWED_HOSTS:
        raise ValueError('Ollama host is not allowed')
    with model_slot('qwen', None, max(1500, len(json.dumps(body)) // 3 + 2400)):
        response = httpx.post(base + '/api/chat', json=body, trust_env=False,
                              timeout=httpx.Timeout(120, connect=2))
        response.raise_for_status()
    result = response.json()['message']
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
        usage={'input_tokens': response.json().get('prompt_eval_count', 0),
               'output_tokens': response.json().get('eval_count', 0)})
