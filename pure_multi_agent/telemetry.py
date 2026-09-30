"""Administrator telemetry for real operations, source reads, and agent exchanges."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from uuid import uuid4
import logging

logger = logging.getLogger(__name__)
_current = ContextVar('agent_audit_context', default=None)


def safe_data(value):
    if isinstance(value, dict):
        return {str(k): '[redacted]' if any(word in str(k).lower() for word in
                ('password', 'token', 'secret', 'authorization', '_media_blocks'))
                else safe_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_data(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def current():
    return _current.get() or {}


def emit(action, target='', *, inputs=None, outputs=None, actor=None, student_id=None, run_id=None):
    ctx = current()
    if not ctx and not actor:
        return
    try:
        from django_api.models import AgentAuditLog
        from django.db import transaction
        # A failed audit insert must not poison an application's open transaction.
        with transaction.atomic():
            AgentAuditLog.objects.create(
                run_id=str(run_id or ctx.get('run_id') or uuid4()),
                student_id=str(student_id if student_id is not None else ctx.get('student_id', '')),
                actor_agent=str(actor or ctx.get('actor', 'Agent'))[:255],
                action_type=action, target=str(target)[:255],
                inputs=safe_data({**({'message_id': ctx['message_id']} if ctx.get('message_id') else {}), **({'exchange_id': ctx['exchange_id']} if ctx.get('exchange_id') else {}), **(inputs or {})}), outputs=safe_data(outputs or {}))
    except Exception:
        logger.exception('Could not persist agent telemetry')


@contextmanager
def scope(actor, student_id='', run_id=None):
    parent = current()
    token = _current.set({**parent, 'actor': actor, 'student_id': str(student_id or parent.get('student_id', '')),
        'run_id': str(run_id or parent.get('run_id') or uuid4()), 'recipient': parent.get('actor', 'Student')})
    try:
        yield current()
    finally:
        _current.reset(token)


@contextmanager
def operation(actor, target, *, student_id='', run_id=None, inputs=None, mode='execution'):
    parent = current()
    caller = parent.get('actor', 'Student')
    exchange_id = str(uuid4())
    metadata = {'exchange_id': exchange_id, 'operation': target, 'mode': mode}
    with scope(actor, student_id, run_id):
        current()['exchange_id'] = exchange_id
        emit('AGENT_COMMUNICATION_START', actor, actor=caller, inputs={**metadata, **(inputs or {})})
        result = {}
        try:
            yield result
        except Exception as exc:
            emit('AGENT_COMMUNICATION_ERROR', caller, outputs={**metadata, 'error': str(exc)})
            raise
        else:
            emit('AGENT_COMMUNICATION_REPLY', caller, outputs={**metadata, **result})


def traced_operation(actor, mode='execution'):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            bound = signature(fn).bind(*args, **kwargs).arguments
            # Passive HTTP status polling is not an agent handoff. Keep real
            # agent reads visible when they inherit an execution context.
            if bound.get('allow_analysis') is False and not current():
                return fn(*args, **kwargs)
            ctx = bound.get('ctx', {})
            sid = bound.get('student_id', ctx.get('canonical_student_id', ''))
            inputs = {k: v for k, v in bound.items() if k in ('question', 'query', 'focus', 'github_input', 'attachment_id')}
            upload = bound.get('uploaded_file')
            if upload is not None:
                inputs['filename'] = getattr(upload, 'name', '')
                inputs['size_bytes'] = getattr(upload, 'size', None)
            if 'uploaded_images' in bound:
                inputs['filenames'] = [getattr(item, 'name', '') for item in bound['uploaded_images']]
            standalone = not current()
            with operation(actor, fn.__name__, student_id=sid, mode=mode, inputs=inputs) as event:
                if standalone:
                    emit('RUN_START', fn.__name__, inputs={'activity_type': fn.__name__, **inputs}, outputs={'summary': actor + ' started ' + fn.__name__.replace('_', ' ') + '.'})
                result = fn(*args, **kwargs)
                if standalone:
                    emit('RUN_COMPLETE', fn.__name__, outputs={'summary': actor + ' completed ' + fn.__name__.replace('_', ' ') + '.'})
                event['result'] = ({'job_id': str(result.pk), 'status': result.status}
                    if mode == 'queue_request' and hasattr(result, 'pk') else result)
                return result
        return wrapped
    return decorate


def trace_github_slice(fn):
    @wraps(fn)
    def wrapped(run, *args, **kwargs):
        with scope('GitHub Agent', str(run.profile.student.uuid), str(run.pk)):
            emit('AGENT_STEP_START', run.stage, inputs={'job_id': str(run.pk)})
            try:
                result = fn(run, *args, **kwargs)
            except Exception as exc:
                emit('AGENT_STEP_ERROR', run.stage, outputs={'error': str(exc)})
                raise
            try:
                run.refresh_from_db(fields=['status', 'progress', 'error', 'stage'])
                emit('AGENT_STEP_ERROR' if run.status == 'failed' else 'AGENT_STEP_RESULT', run.stage,
                    outputs={'job_id': str(run.pk), 'status': run.status, 'progress': run.progress, 'error': run.error})
            except Exception:
                logger.exception('Could not record GitHub job status')
            return result
    return wrapped


def trace_document_job(fn):
    @wraps(fn)
    def wrapped(job, *args, **kwargs):
        actor = 'CV Agent' if job.kind == 'resume' else 'LinkedIn Agent'
        with scope(actor, job.student_id, job.pk):
            emit('AGENT_STEP_START', 'Document extraction', outputs={'summary': actor + ' is reading and extracting the uploaded files.'})
            try:
                result = fn(job, *args, **kwargs)
            except Exception as exc:
                from pure_multi_agent.capacity import ResumeTurnLater
                if isinstance(exc, ResumeTurnLater):
                    emit('AGENT_PROGRESS', 'Waiting for model capacity', outputs={'summary': actor + ' is waiting for model capacity; the upload is saved.'})
                else:
                    emit('RUN_ERROR', 'Document extraction', outputs={'error': 'The uploaded document could not be processed.'})
                raise
            emit('RUN_COMPLETE', 'Document extraction', outputs={'summary': actor + ' completed extraction and saved the result.'})
            return result
    return wrapped


def traced_step(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            emit('AGENT_STEP_START', name)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                emit('AGENT_STEP_ERROR', name, outputs={'error': str(exc)})
                raise
            emit('AGENT_STEP_RESULT', name, outputs={'result': result})
            return result
        return wrapped
    return decorate


def saved_source(actor, student_id, name, read):
    with operation(actor, name, student_id=student_id, mode='saved_evidence') as event:
        result = read()
        event['result'] = result
        return result


def trace_config():
    """Reuse inherited graph callbacks; install one for standalone specialist jobs."""
    from langchain_core.runnables.config import var_child_runnable_config
    config = var_child_runnable_config.get() or {}
    callbacks = config.get('callbacks')
    handlers = callbacks if isinstance(callbacks, list) else getattr(callbacks, 'handlers', [])
    from pure_multi_agent.tracing import GraphTraceLogger
    if any(isinstance(h, GraphTraceLogger) for h in handlers):
        return {}
    ctx = current()
    return {'callbacks': [GraphTraceLogger(label=ctx.get('student_id', ''),
        actor=ctx.get('actor', 'Agent'), root_run_id=ctx.get('run_id'))]} if ctx else {}
