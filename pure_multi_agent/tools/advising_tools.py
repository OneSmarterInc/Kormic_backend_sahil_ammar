"""Evidence and durable plans; the LangGraph model writes all advice."""
import json
from typing import Literal, Optional
from langchain_core.tools import tool


def profile_evidence(ctx, focus='overall'):
    from django_api.models import ResumeUpload
    from .github_tools import github_evidence
    profile = ctx['student_profile']
    facts = {k: v for k, v in profile.items() if v not in (None, '', [], {}, 0) and k not in ('profile_image_path', 'email', 'student_id', 'github_assessment', 'github_profile_intelligence', 'technical_intelligence', 'academic_intelligence', 'overall_profile', 'overall_profile_score')}
    resume = ResumeUpload.objects.filter(student__uuid=ctx['canonical_student_id']).first()
    ctx['document_availability'] = {'resume_uploaded': bool(resume), 'linkedin_evidence_saved': bool(profile.get('linkedin_profile'))}
    return {'focus': focus, 'document_availability': ctx['document_availability'], 'profile': facts, 'github': github_evidence(ctx['canonical_student_id']),
        'resume': {'data': resume.extracted_data, 'uploaded_at': resume.created_at.isoformat()} if resume else None,
        'linkedin': profile.get('linkedin_profile') or None,
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

    return [review_student_profile, recommend_courses, search_study_resources, save_advising_artifact, get_saved_advice]
