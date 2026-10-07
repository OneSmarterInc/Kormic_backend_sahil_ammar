"""Conservative Qwen context admission for both local model transports.

The initial estimate is intentionally padded because Ollama has no token-count
endpoint. After dispatch, callers use Ollama's prompt_eval_count to verify
the full prompt plus output reserve and retry at a larger window if needed.
"""

import json
import os
from dataclasses import dataclass


PROFILE_WINDOWS = {'routing': 4096, 'general': 8192, 'evidence': 16384, 'document': 16384}
PROFILE_OUTPUT = {'routing': 512, 'general': 2400, 'evidence': 2400, 'document': 3200}


class ContextBudgetExceeded(ValueError):
    """The complete input plus output reserve exceeds the configured maximum."""


@dataclass(frozen=True)
class ContextBudget:
    profile: str
    num_ctx: int
    num_predict: int
    input_bound: int
    input_estimate: int


def _bytes(value):
    return len(json.dumps(value, default=str, ensure_ascii=False).encode('utf-8'))


def _message_payload(message):
    if isinstance(message, dict):
        return message
    payload = {'role': getattr(message, 'type', ''), 'content': getattr(message, 'content', '')}
    for field in ('tool_calls', 'tool_call_id', 'name'):
        value = getattr(message, field, None)
        if value:
            payload[field] = value
    return payload


def _tool_payload(tool):
    if isinstance(tool, dict):
        return tool
    schema = getattr(tool, 'args_schema', None)
    return {'name': getattr(tool, 'name', ''),
            'description': getattr(tool, 'description', ''),
            'parameters': schema.model_json_schema() if schema is not None else getattr(tool, 'args', {})}


def select_context(messages, tools=(), *, profile='general', output_tokens=None,
                   format_schema=None, max_context=None, min_context=0):
    if profile not in PROFILE_WINDOWS:
        raise ValueError('Unknown Qwen request profile')
    output = PROFILE_OUTPUT[profile] if output_tokens is None else int(output_tokens)
    maximum = int(max_context if max_context is not None else os.getenv('KORMIC_QWEN_MAX_CONTEXT', '16384'))
    if output < 1 or maximum < 2048:
        raise ValueError('Qwen context and output limits must be positive')
    # Include serialized roles, tool-call arguments, tool schemas, JSON format,
    # and room for Ollama's template/special tokens. No source is removed.
    input_bound = (512 + sum(_bytes(_message_payload(message)) + 32 for message in messages)
                   + sum(_bytes(_tool_payload(tool)) + 64 for tool in tools)
                   + (_bytes(format_schema) if format_schema is not None else 0))
    # JSON/tool schemas average several bytes per token. The independent byte
    # bound is retained for calls where actual usage telemetry is unavailable.
    input_estimate = 256 + (input_bound + 1) // 2
    required = input_estimate + output
    if required > maximum:
        raise ContextBudgetExceeded(
            f'Estimated Qwen request needs {required} context tokens, '
            f'above the configured {maximum}. Increase KORMIC_QWEN_MAX_CONTEXT '
            'if the host can support it, or retrieve the relevant source in smaller stages.')
    if min_context > maximum:
        raise ContextBudgetExceeded('The complete Qwen request cannot be verified inside the configured context maximum.')
    preferred = min(max(PROFILE_WINDOWS[profile], min_context), maximum)
    window = preferred
    while window < required:
        window = min(window * 2, maximum)
    return ContextBudget(profile, window, output, input_bound, input_estimate)


def needs_expansion(budget, prompt_tokens):
    """Verify Ollama evaluated enough of the prompt to leave output room."""
    if prompt_tokens is None:
        if budget.input_bound + budget.num_predict + 128 > budget.num_ctx:
            raise ContextBudgetExceeded(
                'Ollama did not report prompt token usage, so this request cannot be verified without risking source truncation.')
        return False
    return int(prompt_tokens) + budget.num_predict + 128 >= budget.num_ctx
