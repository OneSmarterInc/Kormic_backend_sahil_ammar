"""One accounting event per provider request, including repair attempts."""
import time
from uuid import uuid4
from pure_multi_agent.telemetry import emit


def measured_invoke(model, messages, provider, model_name):
    call_id = str(uuid4())
    started = time.monotonic()
    identity = {'call_id': call_id, 'provider': provider, 'model': model_name}
    emit('MODEL_REQUEST', 'Provider request', inputs=identity)
    try:
        reply = model.invoke(messages)
    except Exception as exc:
        emit('MODEL_USAGE', 'Provider request', inputs=identity,
             outputs={'status': 'failed', 'usage_available': False,
                      'error_type': type(exc).__name__, 'seconds': time.monotonic() - started})
        raise
    usage = getattr(reply, 'usage_metadata', None)
    if isinstance(usage, dict):
        details = usage.get('input_token_details') or {}
        usage = {**usage, 'cache_read_input_tokens': details.get('cache_read', 0),
                 'cache_creation_input_tokens': details.get('cache_creation', 0)}
    metadata = getattr(reply, 'response_metadata', {}) or {}
    emit('MODEL_USAGE', 'Provider request', inputs=identity, outputs={
        'status': 'completed', 'usage_available': usage is not None,
        'usage': usage, 'provider_request_id': metadata.get('id') or getattr(reply, 'id', None),
        'seconds': time.monotonic() - started})
    return reply
