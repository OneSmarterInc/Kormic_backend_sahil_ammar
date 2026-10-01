"""Read the same durable records used by each university portal tab."""
from typing import Literal
from langchain_core.tools import tool
from django.db.models import Q
from pure_multi_agent.change_proposals import officer_university


def build_workspace_tools(ctx):
    @tool
    def read_portal_tab(tab: Literal['dashboard', 'university_profile', 'knowledge_sources',
            'knowledge_base', 'knowledge_groups', 'assistant_chat', 'student_profiles',
            'queries', 'escalated_queries', 'verified_knowledge'], query: str = '', page: int = 1,
            direction: Literal['all', 'student_to_university', 'university_to_student'] = 'all') -> dict:
        """Fetch saved records from a university sidebar tab. All records are scoped
        to this officer's university. Search and browse 10 rows per page; continue
        paging when has_next is true. Existing profile eligibility rules are
        authoritative admission requirements. Queries includes BOTH agent directions;
        Escalated Queries is the older human escalation inbox. Reads never mutate data."""
        from django_api.models import ChatMessage, UniversityKnowledgeEntry, PendingQuery, VerifiedAnswer
        from agent_queries.models import AgentQuery
        from pure_multi_agent.tools.officer_tools import build_tools
        university = officer_university(ctx)
        uid = str(university.uuid)
        if page < 1:
            raise ValueError('Page must be positive.')
        # Resolve delegated tools lazily; constructing tools does not execute them.
        def delegate(name, args):
            return next(t for t in build_tools(ctx) if t.name == name).invoke(args)
        if tab == 'university_profile':
            return delegate('read_university_record', {'section':'all'})
        if tab == 'dashboard':
            result = delegate('university_dashboard', {})
            result['agent_queries_unanswered'] = AgentQuery.objects.filter(conversation__university=university,status='unanswered').count()
            return result
        if tab == 'knowledge_sources':
            return delegate('read_university_record', {'section':'sources'})
        if tab == 'student_profiles':
            return delegate('interested_students', {'name':query,'page':page})
        if tab == 'knowledge_base':
            return delegate('list_university_knowledge', {'query':query,'page':page})
        if tab == 'knowledge_groups':
            rows = university.knowledge_groups.all()
            fields = ['id','slug','escalation_contact_name','escalation_contact_email','updated_at']
            if query: rows = rows.filter(Q(slug__icontains=query)|Q(escalation_contact_name__icontains=query))
        elif tab == 'assistant_chat':
            rows = ChatMessage.objects.filter(university_id=uid,channel='university')
            fields = ['id','sender','content','created_at']
            if query: rows = rows.filter(content__icontains=query)
        elif tab == 'queries':
            rows = AgentQuery.objects.filter(conversation__university=university)
            if direction != 'all': rows = rows.filter(direction=direction)
            if query: rows = rows.filter(Q(question__icontains=query)|Q(answer__icontains=query))
            fields = ['id','direction','question','answer','status','answer_scope',
                'conversation__student__name','raised_by_agent','recipient_agent','created_at','answered_at']
        elif tab == 'escalated_queries':
            rows = PendingQuery.objects.filter(university_id=uid)
            if query: rows = rows.filter(Q(question__icontains=query)|Q(answer__icontains=query))
            fields = ['id','question','answer','status','student_name','program','created_at','answered_at']
        else:
            rows = VerifiedAnswer.objects.filter(university_id=uid)
            if query: rows = rows.filter(Q(question__icontains=query)|Q(answer__icontains=query))
            fields = ['id','query_id','question','answer','source','confidence','created_at']
        total = rows.count()
        return {'tab':tab, 'university_id':uid, 'total':total, 'page':page, 'page_size':10,
            'has_next':page*10<total, 'records':list(rows.order_by('-pk').values(*fields)[(page-1)*10:page*10]),
            'instruction':'These are saved database records. Compose your answer from them; do not invent missing details.'}
    return [read_portal_tab]
