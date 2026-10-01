"""Typed public catalogues, shared persistence, and no student data in the cache."""
import hashlib
import json
from pydantic import BaseModel, Field
from django.db import transaction
from django.utils import timezone


class CourseData(BaseModel):
    name: str = Field(min_length=1, max_length=400)
    level: str = Field(default='N/A', max_length=100)
    duration: str = Field(default='N/A', max_length=150)
    study_mode: str = Field(default='N/A', max_length=150)
    tuition: str = Field(default='N/A', max_length=500)
    currency: str = Field(default='N/A', max_length=20)
    seats: str = Field(default='N/A', max_length=300)
    academic_year: str = Field(default='N/A', max_length=100)
    requirements: str = Field(default='N/A', max_length=4000)


class FactData(BaseModel):
    topic: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=6000)


class IntakeData(BaseModel):
    course_name: str = Field(default='', max_length=400)
    term: str = Field(max_length=200)
    year: int | None = Field(default=None, ge=2000, le=2200)
    deadline: str = Field(default='N/A', max_length=300)
    applicant_scope: str = Field(default='N/A', max_length=300)


class Catalogue(BaseModel):
    description: str = Field(default='', max_length=12000)
    contact_email: str = Field(default='', max_length=255)
    contact_phone: str = Field(default='', max_length=50)
    eligibility_criteria: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    courses: list[CourseData] = Field(default_factory=list, max_length=100)
    facts: list[FactData] = Field(default_factory=list, max_length=80)
    intakes: list[IntakeData] = Field(default_factory=list, max_length=100)
    coverage_notes: str = Field(default='', max_length=3000)


CATALOGUE_INSTRUCTION = (
    'Also return catalogue: {description, contact_email, contact_phone, eligibility_criteria: [{criterion, detail}], '
    'courses: [{name, level, duration, study_mode, tuition, currency, seats, academic_year, requirements}], '
    'facts: [{topic, content}], intakes: [{course_name, term, year, deadline, applicant_scope}], coverage_notes}. '
    'Include all known university information: courses and degrees, programme fees and seats with year/category, '
    'admission requirements, scholarships and financial aid, housing, amenities, contacts and deadlines. '
    'Use facts for scholarships, housing, amenities and other information. Use N/A only in structured missing fields, '
    'null for unknown intake year, and empty arrays when no records are known. Never invent values or claim the catalog is exhaustive. '
    'Country must be a two-letter ISO country code. Do not put personal student information in the catalogue. '
)


@transaction.atomic
def save_catalogue(row, page):
    """Commit a catalogue before any adviser can read it; retries merge by identity."""
    from universities.models import University
    from django_api.models import UniversityKnowledgeEntry
    from .models import PublicUniversity, UniversityPage, UniversityFact, UniversityCourse, UniversityIntake
    from . import services
    from pure_multi_agent.telemetry import emit
    data = Catalogue.model_validate(page.get('catalogue') or {})
    row = PublicUniversity.objects.select_for_update().get(pk=row.pk)
    canonical = row.registered_university
    if canonical and services.registered().filter(pk=canonical.pk).exists():
        # Officer-managed institutions must always use their dedicated agent.
        return canonical
    identity = page.get('provider_identity') or {}
    country = str(identity.get('country') or row.country or '').upper()
    if len(country) != 2:
        country = {'UNITED STATES':'US', 'INDIA':'IN', 'UNITED KINGDOM':'GB'}.get(country, '')
    if canonical is None:
        canonical = University.objects.create(name=row.name, country=country, record_origin='researched',
            website_url=row.website, location=row.address[:255], admissions_office_address=row.address)
        row.registered_university = canonical
    for field in ('description', 'contact_email', 'contact_phone', 'eligibility_criteria'):
        value = getattr(data, field)
        if value:
            setattr(canonical, field, value)
    canonical.save()
    provider = page.get('evidence_provider', 'scraper')
    now = timezone.now()
    # Structured content is stored alongside its provider, never relabeled as a
    # scraped page or human-verified officer knowledge.
    content = json.dumps(data.model_dump(), ensure_ascii=False)
    saved, _ = UniversityPage.objects.update_or_create(university=row, url=page['url'], defaults={
        'title':row.name, 'content':content, 'provider':provider,
        'content_hash':hashlib.sha256(content.encode()).hexdigest(), 'fetched_at':now})
    for item in data.facts:
        UniversityFact.objects.update_or_create(university=row, page=saved, topic=item.topic,
            defaults={'content':item.content, 'source_quote':item.content, 'fetched_at':now})
    for item in data.courses:
        values = item.model_dump()
        name, level = values.pop('name'), values.pop('level')
        UniversityCourse.objects.update_or_create(university=row, name=name, level=level,
            defaults={**values, 'page':saved, 'source_quote':json.dumps(item.model_dump(), ensure_ascii=False), 'fetched_at':now})
    for item in data.intakes:
        values = item.model_dump()
        key = {field:values.pop(field) for field in ('course_name', 'term', 'year', 'applicant_scope')}
        UniversityIntake.objects.update_or_create(university=row, **key,
            defaults={**values, 'page':saved, 'source_quote':json.dumps(item.model_dump(), ensure_ascii=False), 'fetched_at':now})
    UniversityKnowledgeEntry.objects.update_or_create(university_id=str(canonical.uuid),
        topic='University catalogue', source_type=provider, source_url=page['url'],
        defaults={'content':content, 'confidence':0.7 if provider=='claude_direct' else 1,
            'details':{'public_university_id':str(row.pk), 'provider':provider, 'catalogue':data.model_dump()}})
    coverage = dict(row.coverage or {})
    coverage.update(catalogue_saved=True, catalogue_provider=provider, coverage_notes=data.coverage_notes)
    if page.get('provider_answer'):
        coverage['provider_answer'] = page['provider_answer']
    row.coverage, row.fetched_at = coverage, now
    row.save(update_fields=['registered_university', 'coverage', 'fetched_at'])
    emit('AGENT_STEP_RESULT', 'save_university_catalogue', outputs={
        'summary':'Saved university information before the university agent consultation.',
        'university':row.name, 'courses':len(data.courses), 'facts':len(data.facts), 'intakes':len(data.intakes), 'provider':provider})
    return canonical
