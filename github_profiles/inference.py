"""Schema-validated Claude inference with bounded per-sync budgets."""
import json
from contextlib import nullcontext
from urllib.parse import urlparse

import httpx
from django.conf import settings
from jsonschema import validate, ValidationError
from kormic_backend.ollama_config import qwen_keep_alive

from .errors import ServiceError
from .scheduling import CapacityBusy, model_slot
from .prompt_context import compact_json


class InvalidResponse(ServiceError):
    """A completed model response needs individual investigation/validation."""


class Inference:
    # One instance per sync: an offline Qwen is checked once, not for every repo.
    def __init__(self, run=None):
        self.run = run
        self.qwen_available = True
        self.providers = set()
        self.unavailable = False

    @staticmethod
    def validated(content, schema):
        text = content.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        result = json.loads(text)
        validate(result, schema)
        return json.dumps(result, ensure_ascii=False)

    def chat(self, messages, schema, *, max_tokens=2400):
        if self.unavailable:
            raise ServiceError('AI analysis is temporarily unavailable. Collected GitHub facts remain saved; sync again to retry.')
        max_tokens = min(max_tokens, 4000)
        estimated_tokens = (len(compact_json(messages)) + len(compact_json(schema))) // 3 + max_tokens
        def capacity(provider):
            return model_slot(provider, self.run, estimated_tokens) if self.run else nullcontext()
        try:
            # Reuse the project's credentials, request limits and bounded client.
            from agents.github_agent import _get_anthropic_client
            from pure_multi_agent.claude_policy import model_name
            model = model_name()
            system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
            with capacity('claude'):
                response = _get_anthropic_client().messages.create(
                    _skip_qwen=True,
                    model=model, max_tokens=max_tokens,
                    system=system + '\nReturn the result using the structured_response tool.',
                    tools=[{'name': 'structured_response', 'description': 'Return the requested validated structured result.', 'input_schema': schema}],
                    tool_choice={'type': 'tool', 'name': 'structured_response'},
                    messages=[m for m in messages if m['role'] != 'system'])
            # Record provider usage even if the completed answer fails validation.
            # This is a diagnostic event, not a second billable MODEL_USAGE event.
            from pure_multi_agent.telemetry import emit
            usage = getattr(response, 'usage', None)
            usage_fields = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
            actual = {key: getattr(usage, key) for key in usage_fields
                      if type(getattr(usage, key, None)) is int}
            if self.run:
                emit('GITHUB_INFERENCE_USAGE', 'Repository analysis', actor='GitHub Agent',
                     run_id=self.run.pk, student_id=self.run.profile.student.uuid,
                     inputs={'model': model, 'stage': self.run.stage,
                             'prompt_characters': sum(len(m['content']) for m in messages)},
                     outputs={'provider_request_id': getattr(response, 'id', None),
                              'usage_available': 'input_tokens' in actual and 'output_tokens' in actual,
                              'usage': actual})
            structured = next((b.input for b in response.content if getattr(b, 'type', '') == 'tool_use' and getattr(b, 'name', '') == 'structured_response'), None)
            content = self.validated(json.dumps(structured) if structured is not None else ''.join(getattr(b, 'text', '') for b in response.content), schema)
            self.providers.add('claude')
            return {'content': content, 'provider': 'claude', 'model': model}
        except CapacityBusy:
            raise
        except ServiceError:
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            raise InvalidResponse('Claude returned an invalid structured result.') from exc
        except Exception as exc:
            from pure_multi_agent.capacity import AgentBusy
            if isinstance(exc, AgentBusy) or getattr(exc, 'status_code', None) in (429, 529):
                raise CapacityBusy('Claude is busy; queued for another attempt', 30) from exc
            # Do not expose provider payloads, excerpts or credentials in API errors.
            self.unavailable = True
            raise ServiceError('AI analysis is temporarily unavailable. Collected GitHub facts remain saved; sync again to retry.') from exc
