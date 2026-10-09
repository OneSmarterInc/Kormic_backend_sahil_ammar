"""Provider-slot estimates, separate from actual model usage accounting."""

import json


CLAUDE_OUTPUT = {'routing': 512, 'general': 2400, 'evidence': 3200, 'document': 4000}


def claude_output_allowance(profile):
    return CLAUDE_OUTPUT.get(profile, 2400)


def _input_size(value):
    if isinstance(value, list):
        total = 0
        for part in value:
            if isinstance(part, dict) and part.get('type') == 'document':
                total += 16000
            elif isinstance(part, dict) and part.get('type') in ('image', 'image_url'):
                total += 6000
            else:
                total += _input_size(part)
        return total
    if isinstance(value, (dict, tuple)):
        return len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')) // 3
    return len(str(value).encode('utf-8')) // 3


def reservation_estimate(messages, tools=(), *, profile='general', format_schema=None):
    """Input, configured output, and 10% headroom; images use fixed allowances."""
    input_tokens = 256 + sum(32 + _input_size(message.content) for message in messages)
    for tool in tools:
        schema = getattr(tool, 'args_schema', None)
        input_tokens += 64 + _input_size({
            'name': getattr(tool, 'name', ''),
            'description': getattr(tool, 'description', ''),
            'parameters': schema.model_json_schema() if schema else getattr(tool, 'args', {}),
        })
    if format_schema is not None:
        input_tokens += _input_size(format_schema)
    output = claude_output_allowance(profile)
    return input_tokens + output + max(256, (input_tokens + output) // 10)
