"""Native LangChain tool calling, Qwen first and Claude on provider failure.

The DB provider pools are shared with GitHub extraction across all processes.
Capacity exhaustion is backpressure, never an unbounded burst of paid fallback.
"""
import logging
import os
from functools import lru_cache
from urllib.parse import urlsplit

import httpx
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama
from django.conf import settings
from github_profiles.scheduling import model_slot, provider_blocked, block_provider, CapacityBusy

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def qwen():
    base = os.getenv('STUDENT_OLLAMA_BASE_URL', settings.GITHUB_OLLAMA_BASE_URL).rstrip('/')
    if urlsplit(base).hostname not in settings.GITHUB_OLLAMA_ALLOWED_HOSTS:
        raise ValueError('Ollama host is not in the configured allowlist')
    return ChatOllama(model=os.getenv('STUDENT_OLLAMA_MODEL', settings.GITHUB_OLLAMA_MODEL),
        base_url=base, temperature=0, reasoning=False, num_ctx=32768, num_predict=2400,
        client_kwargs={'timeout': httpx.Timeout(90, connect=2), 'trust_env': False})


@lru_cache(maxsize=1)
def claude():
    return ChatAnthropic(model=os.getenv('STUDENT_CLAUDE_MODEL', 'claude-haiku-4-5-20251001'),
        max_tokens=6000, timeout=90, max_retries=0)


def _validate(reply, tools, *, validate_arguments=True):
    if reply.invalid_tool_calls:
        raise ValueError('Malformed model tool call')
    mapping = {t.name: t for t in tools}
    if len(reply.tool_calls) > 6:
        raise ValueError('Too many parallel tool calls')
    for call in reply.tool_calls:
        if call['name'] not in mapping:
            raise ValueError('Unknown model tool')
        if validate_arguments:
            mapping[call['name']].args_schema.model_validate(call['args'])
    if not reply.tool_calls and not reply.content:
        raise ValueError('Empty model response')
    return reply


def invoke(messages, tools=(), *, force_claude=False):
    # Text-only Qwen cannot interpret an uploaded image. Claude can.
    vision = any(isinstance(m.content, list) and any(isinstance(b, dict) and b.get('type') in ('image', 'image_url') for b in m.content) for m in messages)
    estimate = max(1500, sum(len(str(m.content)) for m in messages) // 3 + sum(len(str(t.args)) for t in tools) // 3 + 6000)
    if not (force_claude or vision or provider_blocked('qwen')):
        try:
            with model_slot('qwen', None, estimate):
                model = qwen().bind_tools(tools) if tools else qwen()
                reply = _validate(model.invoke(messages), tools)
                reply.response_metadata['routing_provider'] = 'qwen'
                return reply
        except CapacityBusy:
            raise
        except Exception as exc:
            if getattr(exc, 'status_code', None) in (429, 503):
                raise CapacityBusy('Qwen is busy', 5) from exc
            block_provider('qwen', 60)
            logger.info('Qwen unavailable or invalid tool response; using Claude (%s)', type(exc).__name__)
    with model_slot('claude', None, estimate):
        try:
            from pure_multi_agent.capacity import model_slot as distributed_claude_slot
            with distributed_claude_slot():
                model = claude().bind_tools(tools) if tools else claude()
                # LangChain tools validate arguments before execution. Return
                # errors to Claude through the graph so it can repair a call.
                reply = _validate(model.invoke(messages), tools, validate_arguments=False)
                reply.response_metadata['routing_provider'] = 'claude'
                return reply
        except Exception as exc:
            if getattr(exc, 'status_code', None) in (429, 529):
                raise CapacityBusy('Claude is busy', 15) from exc
            raise
