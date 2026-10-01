"""Typed public catalogues, shared persistence, and no student data in the cache."""
import hashlib
import json
from pydantic import BaseModel, Field, field_validator
from django.db import transaction
from django.utils import timezone


class CatalogueRecord(BaseModel):
    @field_validator('*', mode='before')
    @classmethod
    def normalize_scalar(cls, value, info):
        field = cls.model_fields[info.field_name]
        if field.annotation is str:
            if value is None and not field.is_required():
                return field.default
            if type(value) in (int, float):
                return str(value)
        return value


class CourseData(CatalogueRecord):
    name: str = Field(min_length=1, max_length=400)
    level: str = Field(default='N/A', max_length=100)
    duration: str = Field(default='N/A', max_length=150)
    study_mode: str = Field(default='N/A', max_length=150)
    tuition: str = Field(default='N/A', max_length=500)
    currency: str = Field(default='N/A', max_length=20)
    seats: str = Field(default='N/A', max_length=300)
    academic_year: str = Field(default='N/A', max_length=100)
    requirements: str = Field(default='N/A', max_length=4000)


class FactData(CatalogueRecord):
    topic: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=6000)


class IntakeData(CatalogueRecord):
    course_name: str = Field(default='', max_length=400)
    term: str = Field(max_length=200)
    year: int | None = Field(default=None, ge=2000, le=2200)
    deadline: str = Field(default='N/A', max_length=300)
    applicant_scope: str = Field(default='N/A', max_length=300)


class ScholarshipData(CatalogueRecord):
    name: str = Field(min_length=1,max_length=400)
    category: str = Field(default='scholarship',max_length=100)
    amount: str = Field(default='N/A',max_length=500)
    eligibility: str = Field(default='N/A',max_length=2000)
    deadline: str = Field(default='N/A',max_length=300)
    application_url: str = Field(default='N/A',max_length=1000)


class Catalogue(CatalogueRecord):
    description: str = Field(default='', max_length=12000)
    contact_email: str = Field(default='', max_length=255)
    contact_phone: str = Field(default='', max_length=50)
    eligibility_criteria: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    courses: list[CourseData] = Field(default_factory=list, max_length=100)
    facts: list[FactData] = Field(default_factory=list, max_length=80)
    intakes: list[IntakeData] = Field(default_factory=list, max_length=100)
    coverage_notes: str = Field(default='', max_length=3000)
    scholarships: list[ScholarshipData] = Field(default_factory=list,max_length=100)


CATALOGUE_INSTRUCTION = (
    'Also return catalogue: {description, contact_email, contact_phone, eligibility_criteria: [{criterion, detail}], '
    'courses: [{name, level, duration, study_mode, tuition, currency, seats, academic_year, requirements}], '
    'facts: [{topic, content}], intakes: [{course_name, term, year, deadline, applicant_scope}], coverage_notes}. '
    'Include all known university information: courses and degrees, programme fees and seats with year/category, '
    'admission requirements, scholarships and financial aid, housing, amenities, contacts and deadlines. '
    'Use facts for scholarships, housing, amenities and other information. Use N/A only in structured missing fields, '
    'null for unknown intake year, and empty arrays when no records are known. Never invent values or claim the catalog is exhaustive. '
    'Country must be a two-letter ISO country code. Do not put personal student information in the catalogue. '
    'For scholarship requests also return catalogue.scholarships: [{name, category, amount, eligibility, deadline, application_url}]. '
    'Distinguish scholarships from need-based aid, assistantships and employer benefits. Do not claim an exhaustive list without evidence. '
)


from github_profiles.scheduling import retry_database


@retry_database
@transaction.atomic
def save_catalogue(row, page):
    """Commit a catalogue before any adviser can read it; retries merge by identity."""
    from .models import PublicUniversity, UniversityPage, UniversityFact, UniversityCourse, UniversityIntake
    from . import services
    from pure_multi_agent.telemetry import emit
    if page.get('evidence_provider') == 'claude_direct':
        raise ValueError('Model-memory catalogue data cannot be saved as university evidence.')
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
    row.country = country
    provider = page.get('evidence_provider', 'scraper')
    now = timezone.now()
    # Structured content is stored alongside its provider, never relabeled as a
    # scraped page or human-verified officer knowledge.
    # Keep the actual document for later source checks. A model's structured
    # output is not a verbatim source and must never replace it.
    content = page.get('content') or ''
    if not content:
        raise ValueError('A university catalogue requires the fetched source document.')
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
    coverage = dict(row.coverage or {})
    coverage.pop('provider_answer', None)
    if coverage.get('catalogue_provider') == 'claude_direct':
        coverage.pop('catalogue_profile', None)
    profile = dict(coverage.get('catalogue_profile', {}))
    if data.scholarships:
        existing={item['name']:item for item in profile.get('scholarships',[])}
        existing.update({item.name:item.model_dump() for item in data.scholarships})
        profile['scholarships']=list(existing.values())
    for field in ('description', 'contact_email', 'contact_phone', 'eligibility_criteria'):
        if getattr(data, field):
            profile[field] = getattr(data, field)
    coverage['catalogue_profile'] = profile
    coverage.update(catalogue_saved=True, catalogue_provider=provider, coverage_notes=data.coverage_notes)
    if page.get('provider_answer'):
        coverage['provider_answer'] = page['provider_answer']
    row.coverage, row.fetched_at = coverage, now
    row.save(update_fields=['country', 'coverage', 'fetched_at'])
    emit('AGENT_STEP_RESULT', 'save_university_catalogue', outputs={
        'summary':'Saved university information before the university agent consultation.',
        'university':row.name, 'courses':len(data.courses), 'facts':len(data.facts), 'intakes':len(data.intakes), 'provider':provider})
    return row
