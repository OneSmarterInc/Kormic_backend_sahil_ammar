"""Resolve a short selection against the immediately preceding offered choices."""
import re
from datetime import timedelta
from django.utils import timezone


def selection_number(text):
    words={'first':1,'second':2,'third':3,'fourth':4,'fifth':5}
    text=text.lower().strip().rstrip('.!')
    match=re.fullmatch(r'(?:the |option |number )?(first|second|third|fourth|fifth|[1-9]|10)(?: one| option)?',text)
    if not match:
        return None
    return words.get(match[1]) or int(match[1])


def restore_selection(ctx, messages):
    number=selection_number(ctx.get('current_message',''))
    if number is None or not ctx.get('canonical_student_id'):
        return None
    humans=[i for i,m in enumerate(messages) if m.type=='human']
    if len(humans)<2:
        return None
    previous=messages[humans[-2]+1:humans[-1]]
    replies=[m.content for m in previous if m.type=='ai' and isinstance(m.content,str) and m.content and not m.tool_calls]
    if not replies or 'Which university do you mean?' not in replies[-1]:
        return None
    shown=replies[-1]
    from university_research.models import UniversitySearch
    matches=[]
    for search in UniversitySearch.objects.filter(student__uuid=ctx['canonical_student_id'],created_at__gte=timezone.now()-timedelta(days=1)).order_by('-created_at')[:20]:
        choices=search.candidates.get('universities',[])
        if len(choices)>1 and all(c.get('website') and c['website'] in shown for c in choices):
            matches.append(search)
    if not matches:
        return None
    # Only searches whose actual options were shown in the preceding answer.
    search=matches[0]
    if not 1<=number<=len(search.candidates['universities']):
        return None
    ctx['university_search_id']=str(search.pk)
    original=next((messages[i].content for i in reversed(humans[:-1])
        if isinstance(messages[i].content,str) and selection_number(messages[i].content) is None), search.query)
    ctx['university_question']=original
    ctx['university_resolution_turn']=ctx.get('turn_id')
    ctx.pop('university_clarification',None)
    return number


def restore_university_followup(ctx, active=None):
    """Reuse only the immediately previous completed turn's single selection."""
    text=ctx.get('current_message','')
    intent = ctx.get('turn_intent', {})
    if intent.get('institutions') or intent.get('comparison'):
        return False
    if not intent.get('followup') and not re.search(r'\b(that university|that college|that degree|that course|that program|that programme|their|they|its)\b',text,re.I):
        return False
    # An explicit new institution must go through normal resolution.
    if re.search(r'\b(university of|college of|institute of)\b',text,re.I):
        return False
    if re.search(r'\b[A-Z][\w.-]+(?: [A-Z][\w.-]+){0,5} (?:University|College|Institute)\b|\bat [A-Z]',text):
        return False
    if active and active.get('id'):
        uid = active['id']
        ctx['university_question'] = text
    else:
        mid=ctx.get('current_message_id')
        if not mid:
            return False
        from django_api.models import AgentJob
        job=AgentJob.objects.filter(student_id=ctx.get('canonical_student_id'),status='completed',
            payload__message_id__lt=mid).order_by('-created_at').first()
        if not job:
            return False
        saved=job.payload.get('resume_state',{})
        candidates=saved.get('university_candidates',[])
        if len(candidates)!=1:
            return False
        uid=candidates[0]
    from university_research import services
    from university_research.models import PublicUniversity
    row=PublicUniversity.objects.filter(pk=uid.split(':',1)[1]).first() if uid.startswith('public:') else services.registered().filter(uuid=uid).first()
    if row is None:
        return False
    services.add_reference(ctx,row)
    ctx['university_candidates']=[uid]
    ctx['university_resolution_turn']=ctx.get('turn_id')
    ctx['university_evidence_required']=True
    return True
