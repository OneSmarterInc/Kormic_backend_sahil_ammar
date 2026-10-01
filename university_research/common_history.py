"""Student-scoped history for the shared agent, separate from university accounts."""
from .models import CommonAgentMessage


def history(ctx, university):
    rows = list(CommonAgentMessage.objects.filter(student__uuid=ctx['canonical_student_id'],
        university=university, kind__in=['request', 'reply']).order_by('-created_at', '-id')[:16])
    return [{'speaker': 'Common University Agent' if row.actor == 'university_agent' else 'Student Agent',
             'content': row.content, 'timestamp': row.created_at.isoformat()} for row in reversed(rows)]


def message(ctx, university, actor, content, *, kind='message', metadata=None):
    from django_api.models import StudentProfile
    from pure_multi_agent.telemetry import current, safe_data, emit
    trace = current()
    row = CommonAgentMessage.objects.create(
        student=StudentProfile.objects.get(uuid=ctx['canonical_student_id']), university=university,
        actor=actor, content=str(content)[:45000], kind=kind,
        metadata=safe_data({**{key:trace[key] for key in ('run_id','exchange_id') if trace.get(key)}, **(metadata or {})}))
    sender = 'Common University Agent' if actor == 'university_agent' else 'Student Agent'
    recipient = 'Student Agent' if actor == 'university_agent' else 'Common University Agent'
    emit('AGENT_CONVERSATION_MESSAGE', recipient, actor=sender,
        student_id=ctx['canonical_student_id'], outputs={
            'message_id':row.pk, 'kind':kind, 'content':row.content, 'university':university.name,
            'public_university_id':str(university.pk)})
    return row
