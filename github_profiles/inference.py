"""Schema-validated Qwen inference with the existing Claude API as fallback."""
import json
from contextlib import nullcontext
from urllib.parse import urlparse

import httpx
from django.conf import settings
from jsonschema import validate, ValidationError

from .errors import ServiceError
from .scheduling import CapacityBusy, model_slot, provider_blocked, block_provider


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

    def chat(self, messages, schema):
        if self.unavailable:
            raise ServiceError('AI analysis is temporarily unavailable. Collected GitHub facts remain saved; sync again to retry.')
        base = settings.GITHUB_OLLAMA_BASE_URL.rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in settings.GITHUB_OLLAMA_ALLOWED_HOSTS or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ServiceError('The GitHub Qwen endpoint must use an explicitly allowed Ollama host.')
        estimated_tokens = (len(json.dumps(messages)) + len(json.dumps(schema))) // 3 + 2400
        def capacity(provider):
            return model_slot(provider, self.run, estimated_tokens) if self.run else nullcontext()
        if self.qwen_available and not (self.run and provider_blocked('qwen')):
            try:
                with capacity('qwen'):
                    response = httpx.post(base + '/api/chat', trust_env=False,
                        timeout=httpx.Timeout(min(settings.GITHUB_OLLAMA_TIMEOUT, 240), connect=2),
                        json={'model': settings.GITHUB_OLLAMA_MODEL, 'messages': messages,
                              'stream': False, 'think': False, 'keep_alive': '10m', 'format': schema,
                              'options': {'num_ctx': 16384, 'num_predict': 1800, 'temperature': 0.2}})
                if response.status_code in (429, 503):
                    raise CapacityBusy('Qwen is busy; queued for another attempt', 15)
                response.raise_for_status()
                content = self.validated(response.json()['message']['content'], schema)
                self.providers.add('qwen')
                return {'content': content, 'provider': 'qwen', 'model': settings.GITHUB_OLLAMA_MODEL}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError):
                self.qwen_available = False
                if self.run:
                    block_provider('qwen')
        try:
            # Reuse the project's credentials, request limits and bounded client.
            from agents.github_agent import _get_anthropic_client, MODEL
            system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
            with capacity('claude'):
                response = _get_anthropic_client().messages.create(
                    model=MODEL, max_tokens=2400,
                    system=system + '\nReturn only JSON matching this schema: ' + json.dumps(schema),
                    tools=[{'name': 'structured_response', 'description': 'Return the requested validated structured result.', 'input_schema': schema}],
                    tool_choice={'type': 'tool', 'name': 'structured_response'},
                    messages=[m for m in messages if m['role'] != 'system'])
            structured = next((b.input for b in response.content if getattr(b, 'type', '') == 'tool_use' and getattr(b, 'name', '') == 'structured_response'), None)
            content = self.validated(json.dumps(structured) if structured is not None else ''.join(getattr(b, 'text', '') for b in response.content), schema)
            self.providers.add('claude')
            return {'content': content, 'provider': 'claude', 'model': MODEL}
        except CapacityBusy:
            raise
        except ServiceError:
            raise
        except Exception as exc:
            from pure_multi_agent.capacity import AgentBusy
            if isinstance(exc, AgentBusy) or getattr(exc, 'status_code', None) in (429, 529):
                raise CapacityBusy('Claude is busy; queued for another attempt', 30) from exc
            # Do not expose provider payloads, excerpts or credentials in API errors.
            self.unavailable = True
            raise ServiceError('AI analysis is temporarily unavailable. Collected GitHub facts remain saved; sync again to retry.') from exc
