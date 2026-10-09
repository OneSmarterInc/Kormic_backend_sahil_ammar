"""Shared, bounded Claude requests for SDK and LangChain callers."""
import json
import os
import time
from uuid import uuid4
from pure_multi_agent.telemetry import emit, current
from pure_multi_agent.qwen_context import ContextBudgetExceeded


def model_name():
    model = os.getenv('KORMIC_CLAUDE_MODEL', 'claude-haiku-4-5-20251001')
    if not model.startswith('claude-'):
        raise ValueError('KORMIC_CLAUDE_MODEL must be a Claude model.')
    return model


def estimate(value):
    if isinstance(value, list):
        return sum(estimate(item) for item in value)
    if isinstance(value, dict):
        if value.get('type') == 'document':
            return 16000
        if value.get('type') in {'image', 'image_url'}:
            return 6000
        return sum(estimate(k) + estimate(v) for k, v in value.items())
    return len(str(value).encode('utf-8')) // 3 + 1


def guard(payload):
    maximum = int(os.getenv('KORMIC_CLAUDE_MAX_INPUT_TOKENS', '32000'))
    if estimate(payload) + 512 > maximum:
        raise ContextBudgetExceeded('The Claude request exceeds the configured input budget. Retrieve smaller evidence pages or shorten the conversation context.')


def cached_system(system):
    # Cache only a long, stable system prefix; Anthropic decides eligibility
    # using actual tokenization. Do not pad short prompts to reach a threshold.
    if isinstance(system, str) and len(system) >= 20000:
        return [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}]
    return system


def prepare_sdk(kwargs):
    kwargs = dict(kwargs)
    kwargs['model'] = model_name()
    kwargs['max_tokens'] = min(int(kwargs.get('max_tokens', 2400)), 4000)
    guard({key: kwargs.get(key) for key in ('system', 'messages', 'tools')})
    if kwargs.get('system'):
        kwargs['system'] = cached_system(kwargs['system'])
    return kwargs


def prepare_langchain(messages, tools):
    guard({'messages': [{'role': m.type, 'content': m.content, 'tool_calls': getattr(m, 'tool_calls', [])} for m in messages],
           'tools': [{'name': t.name, 'description': t.description, 'schema': t.args_schema.model_json_schema() if t.args_schema else {}} for t in tools]})
    return [m.model_copy(update={'content': cached_system(m.content)}) if m.type == 'system' else m for m in messages]


def measured_create(messages, kwargs):
    identity = {'call_id': str(uuid4()), 'provider': 'claude', 'model': kwargs['model']}
    actor = current().get('actor') or 'Claude API'
    start = time.monotonic()
    emit('MODEL_REQUEST', 'Claude SDK request', inputs=identity, actor=actor)
    try:
        response = messages.create(**kwargs)
    except Exception as exc:
        emit('MODEL_USAGE', 'Claude SDK request', inputs=identity, actor=actor,
             outputs={'status': 'failed', 'usage_available': False, 'error_type': type(exc).__name__})
        raise
    usage = response.usage.model_dump() if hasattr(response.usage, 'model_dump') else None
    emit('MODEL_USAGE', 'Claude SDK request', inputs=identity, actor=actor,
         outputs={'status': 'completed', 'usage_available': usage is not None, 'usage': usage,
                  'provider_request_id': response.id, 'seconds': time.monotonic() - start})
    return response
