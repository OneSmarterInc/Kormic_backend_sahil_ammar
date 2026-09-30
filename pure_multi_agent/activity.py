"""Public task labels only; never expose prompts, arguments, or reasoning."""
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from datetime import timedelta
from uuid import uuid4
from django.utils import timezone
from agent_queries.models import AgentActivity

_current = ContextVar('agent_activity', default=None)

def publish(label, status='working'):
    current = _current.get()
    if current:
        AgentActivity.objects.filter(owner_key=current[0], run_id=current[1]).update(
            label=label, status=status, updated_at=timezone.now())

def tool_activity(name):
    labels = [
        (('ask_university',), 'Asking the university agent…'),
        (('ask_student_agent',), 'Asking the student agent…'),
        (('clarification',), 'Raising a query for missing information…'),
        (('resolve_', 'update_', 'save_'), 'Applying your confirmed changes…'),
        (('propose_',), 'Preparing changes for your confirmation…'),
        (('github',), 'Checking GitHub information…'),
        (('document', 'resume', 'linkedin'), 'Reading your document information…'),
        (('search', 'research', 'official'), 'Searching official university information…'),
        (('compare', 'fit', 'recommend', 'course'), 'Comparing courses and university fit…'),
        (('student', 'profile', 'evidence'), 'Reading profile information…'),
        (('university', 'scholarship', 'requirement'), 'Checking university information…'),
    ]
    publish(next((label for words, label in labels if any(word in name for word in words)), 'Using a tool to check information…'))

def track_activity(role):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            subject_id = next(iter(signature(fn).bind(*args, **kwargs).arguments.values()))
            key, run = f'{role}:{subject_id}', uuid4()
            AgentActivity.objects.update_or_create(owner_key=key, defaults={'run_id': run, 'status': 'working', 'label': 'Thinking…'})
            token = _current.set((key, run))
            try:
                from pure_multi_agent.telemetry import scope, emit, current
                actor = 'Student Agent' if role == 'student' else 'University Agent'
                sid = str(subject_id) if role == 'student' else str(kwargs.get('subject_student_id') or '')
                with scope(actor, sid, run):
                    bound = signature(fn).bind(*args, **kwargs).arguments
                    current()['message_id'] = bound.get('message_id') or (bound.get('turn_id') if role != 'student' else None)
                    emit('RUN_START', fn.__name__, inputs={'owner_key': key,
                        'message': signature(fn).bind(*args, **kwargs).arguments.get('message', '')})
                    try:
                        result = fn(*args, **kwargs)
                    except Exception as exc:
                        emit('RUN_ERROR', fn.__name__, outputs={'error': str(exc)})
                        raise
                    emit('RUN_COMPLETE', 'Student' if role == 'student' else 'University officer', outputs={'result': result})
                publish('Reply ready', 'completed')
                return result
            except Exception:
                publish('Unable to finish this response', 'failed')
                raise
            finally:
                _current.reset(token)
        return wrapped
    return decorate

def read_activity(key):
    row = AgentActivity.objects.filter(owner_key=key, updated_at__gte=timezone.now()-timedelta(minutes=30)).first()
    if not row:
        return {'status': 'idle'}
    return {'status': row.status, 'label': row.label, 'run_id': str(row.run_id), 'updated_at': row.updated_at.isoformat()}
