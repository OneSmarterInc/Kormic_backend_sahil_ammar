"""Evidence and durable plans; the LangGraph model writes all advice."""
import json
from typing import Literal, Optional
from langchain_core.tools import tool


def profile_evidence(ctx, focus='overall'):
    from django_api.models import ResumeUpload
    from .github_tools import github_evidence
    from agent_queries.models import AgentQuery
    query_answers = list(AgentQuery.objects.filter(conversation__student__uuid=ctx['canonical_student_id'], status='answered').order_by('-answered_at').values('question', 'answer', 'direction', 'conversation__university__name')[:20])
    from pure_multi_agent.change_proposals import effective_profile
    profile, assumptions = effective_profile(ctx)
    facts = {k: v for k, v in profile.items() if v not in (None, '', [], {}, 0) and k not in ('profile_image_path', 'email', 'student_id', 'github_assessment', 'github_profile_intelligence', 'technical_intelligence', 'academic_intelligence', 'overall_profile', 'overall_profile_score')}
    resume = ResumeUpload.objects.filter(student__uuid=ctx['canonical_student_id']).first()
    ctx['document_availability'] = {'resume_uploaded': bool(resume), 'linkedin_evidence_saved': bool(profile.get('linkedin_profile'))}
    from pure_multi_agent.document_evidence import confirmed_evidence
    from pure_multi_agent.telemetry import saved_source
    resume_data = saved_source('CV Agent', ctx['canonical_student_id'], 'Read saved resume',
        lambda: {'data': resume.extracted_data, 'uploaded_at': resume.created_at.isoformat()} if resume else None)
    linkedin_data = saved_source('LinkedIn Agent', ctx['canonical_student_id'], 'Read saved LinkedIn profile',
        lambda: profile.get('linkedin_profile') or None)
    return {'answered_agent_queries': query_answers, 'focus': focus, 'confirmed_documents': confirmed_evidence(ctx['canonical_student_id']), 'conversation_assumptions': assumptions, 'assumptions_are_saved_profile_facts': False, 'document_availability': ctx['document_availability'], 'profile': facts, 'github': github_evidence(ctx['canonical_student_id']),
        'resume': resume_data,
        'linkedin': linkedin_data,
        'guidance': 'If resume_uploaded=false you have NOT SEEN THE RESUME. If linkedin_evidence_saved=false you have NOT SEEN LINKEDIN. Do not claim either document has gaps or is generic. Explain what you can infer from the saved profile and offer general improvement suggestions, clearly conditional on reviewing the actual documents. Missing evidence is unknown, never a negative finding. No invented achievements, metrics, scores or admission probabilities.'}


def build_tools(ctx):
    from university_research.models import AdvisingArtifact

    @tool
    def review_student_profile(focus: Literal['overall', 'resume', 'linkedin', 'github', 'academic', 'career'] = 'overall') -> dict:
        """Retrieve current profile/resume/LinkedIn/GitHub evidence for a personalized improvement review, rewriting, skill-gap or career analysis. You synthesize the advice."""
        return profile_evidence(ctx, focus)

    @tool
    def recommend_courses(interests: str = '', university_ids: Optional[list[str]] = None) -> dict:
        """Get profile evidence and actual course records for course recommendations. Use university lookup first for uncached institutions; rank fit from goals, prerequisites and budget."""
        from .university_tools import university_evidence
        ids = (university_ids or ctx.get('university_candidates', []))[:5]
        return {'student': profile_evidence(ctx), 'interests': interests,
            'catalogs': [university_evidence(ctx, uid, 'courses programs prerequisites tuition ' + interests) for uid in ids],
            'instruction': 'Separate general learning directions from verified available programs. Explain tradeoffs and missing preferences; never claim a universal best course.'}

    @tool
    def calculate_study_budget(currency: Literal['INR', 'USD', 'GBP', 'EUR', 'CAD', 'AUD'],
                               tuition_total: float, living_monthly: float, months: int,
                               other_total: float = 0, budget_total: Optional[float] = None) -> dict:
        """Calculate whole-program costs in one currency from sourced amounts or explicitly labelled estimates. Tuition is the full programme, living is monthly. Never invent an exchange rate."""
        from decimal import Decimal
        from pure_multi_agent.advice_policy import budget_context
        import re
        duration = re.search(r'\b(one|two|three|four|five|six|\d+)\s*[- ]\s*years?\b', ctx.get('current_message', ''), re.I)
        if duration:
            word = duration[1].lower()
            years = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6}.get(word)
            months = (years if years is not None else int(word)) * 12
        values = [Decimal(str(v)) for v in (tuition_total, living_monthly, other_total)]
        if not all(v.is_finite() and v >= 0 for v in values) or not 1 <= months <= 120:
            return {'error': 'Use non-negative finite costs and a duration of 1-120 months.'}
        total = values[0] + values[1] * months + values[2]
        budget = budget_context(ctx['student_profile'])
        import re
        explicit = re.search(r'\bbudget\s*(?:is|of|:)?\s*' + currency + r'\s*([\d,]+(?:\.\d+)?)', ctx.get('current_message', ''), re.I)
        if explicit:
            budget_total = float(explicit[1].replace(',', ''))
        if budget_total is not None:
            import re
            supplied = Decimal(str(budget_total))
            text = ctx.get('current_message', '')
            amounts = [Decimal(n.replace(',', '')) for n in re.findall(r'(?<!\w)\d[\d,]*(?:\.\d+)?', text)]
            if not supplied.is_finite() or supplied < 0 or supplied not in amounts or currency not in text.upper():
                return {'error': 'budget_total must be explicitly supplied in the current question in the calculation currency.'}
            budget = {'amount': str(supplied), 'currency': currency, 'period': 'entire_program',
                      'source': 'current question; not saved to profile'}
        comparable = budget['currency'] == currency and budget['period'] == 'entire_program' and budget['amount'] is not None
        return {'currency': currency, 'period': 'entire_program', 'months': months,
                'tuition_total': str(values[0]), 'living_monthly': str(values[1]),
                'living_total': str(values[1] * months), 'other_total': str(values[2]),
                'total': str(total), 'student_budget': budget,
                'within_budget': total <= Decimal(str(budget['amount'])) if comparable else None,
                'remaining': str(Decimal(str(budget['amount'])) - total) if comparable else None,
                'instruction': 'Label estimated inputs. This calculation does not verify fees or establish sufficient visa funds. Unknown/mismatched currency or period prevents an affordability conclusion.'}

    @tool
    def search_study_resources(query: str, category: Literal['scholarships', 'courses', 'exams', 'careers', 'applications'] = 'courses') -> dict:
        """Search public learning resources, scholarships, exams, careers or applications. Verify eligibility and dates from sources; never include student personal data in search queries."""
        from university_research.web import search_web
        ctx['web_search_count'] = ctx.get('web_search_count', 0) + 1
        if ctx['web_search_count'] > 3:
            return {'error': 'Search limit reached for this turn; refine in a follow-up.'}
        rows = search_web(f'{query} {category}', limit=8)
        ctx.setdefault('known_web_urls', set()).update(r['url'] for r in rows)
        return {'results': rows, 'source_type': 'web_search_snippets', 'note': 'Search snippets are preliminary, not verified admission requirements.'}

    @tool
    def save_advising_artifact(kind: Literal['roadmap', 'profile_review', 'resume_draft', 'linkedin_draft', 'course_recommendation', 'shortlist', 'application_checklist', 'statement_draft'], title: str, content: dict, artifact_id: str = '') -> dict:
        """Save advice or a draft YOU composed from evidence, or update a saved plan. Include actions, rationale, priorities, dates when known, and unknowns. Does not change external profiles or submit applications."""
        if len(json.dumps(content)) > 60000 or len(title) > 300:
            return {'error': 'Advice is too large; save a concise plan.'}
        rows = AdvisingArtifact.objects.filter(student__uuid=ctx['canonical_student_id'])
        if artifact_id:
            row = rows.filter(pk=artifact_id).first()
            if not row:
                return {'error': 'Saved advice not found for this student.'}
            row.kind, row.title, row.content = kind, title, content
        else:
            if rows.count() >= 500:
                return {'error': 'Saved advice limit reached. Update an existing plan.'}
            from django_api.models import StudentProfile
            row = AdvisingArtifact(student=StudentProfile.objects.get(uuid=ctx['canonical_student_id']), kind=kind, title=title, content=content)
        row.sources = list(ctx.get('university_references', {}).values())
        row.save()
        if kind == 'roadmap':
            ctx['student_profile']['roadmap'] = content
        return {'saved': True, 'id': str(row.pk), 'kind': kind, 'title': title}

    @tool
    def get_saved_advice(kind: str = '', artifact_id: str = '') -> dict:
        """Read this student's saved recommendations, drafts, shortlist, checklist or roadmap. Use an artifact id to resume and update a plan."""
        rows = AdvisingArtifact.objects.filter(student__uuid=ctx['canonical_student_id'])
        if artifact_id:
            rows = rows.filter(pk=artifact_id)
        if kind:
            rows = rows.filter(kind=kind)
        return {'items': list(rows.order_by('-updated_at').values('id', 'kind', 'title', 'content', 'sources', 'updated_at')[:10])}

    return [review_student_profile, recommend_courses, calculate_study_budget, search_study_resources, save_advising_artifact, get_saved_advice]
