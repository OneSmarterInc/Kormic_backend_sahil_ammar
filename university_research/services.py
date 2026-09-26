import hashlib
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import InformationPolicy, PublicUniversity, ResearchRun, UniversityFact


def refresh_days():
    return InformationPolicy.objects.get_or_create(pk=1)[0].refresh_days


def registered():
    from universities.models import University
    return University.objects.filter(accounts__role='university', accounts__user__is_active=True).distinct()


def search_registered(query='', country='', location=''):
    rows = registered()
    if country:
        rows = rows.filter(country__iexact=country[:2])
    if location:
        rows = rows.filter(Q(location__icontains=location) | Q(admissions_office_address__icontains=location))
    if query:
        exact = rows.filter(name__iexact=query)
        if exact.exists():
            rows = exact
        else:
            terms = [t for t in re.findall(r'[\w-]+', query) if t.casefold() not in {'university', 'of', 'the', 'college', 'in'}]
            for term in terms[:8]:
                rows = rows.filter(Q(name__icontains=term) | Q(description__icontains=term))
    return rows.order_by('name')


def reference(row):
    if isinstance(row, PublicUniversity):
        dates = [row.fetched_at] if row.fetched_at else []
        oldest = row.facts.order_by('fetched_at').values_list('fetched_at', flat=True).first()
        if oldest:
            dates.append(oldest)
        updated = min(dates) if dates else None
        active = row.runs.filter(status__in=['queued', 'running']).first()
        listed = bool(row.registered_university_id and registered().filter(pk=row.registered_university_id).exists())
        return {'id': 'public:' + str(row.pk), 'name': row.name, 'listed': listed, 'source': 'researched',
            'url': row.website, 'address': row.address, 'country': row.country,
            'updated_at': updated.isoformat() if updated else None,
            'stale': not updated or updated < timezone.now()-timedelta(days=refresh_days()),
            'processing': bool(active), 'progress': active.progress if active else '',
            'coverage': row.coverage}
    result = {'id': str(row.uuid), 'name': row.name, 'listed': registered().filter(pk=row.pk).exists(),
        'source': 'registered', 'url': row.website_url, 'address': row.admissions_office_address or row.location,
        'country': row.country, 'stale': False, 'processing': False}
    researched = PublicUniversity.objects.filter(registered_university=row).first()
    if researched:
        website_info = reference(researched)
        result.update({key: website_info.get(key) for key in ('updated_at', 'stale', 'processing', 'progress', 'coverage')})
        result['research_id'] = website_info['id']
    return result


def public_for_registered(row):
    """Keep official website evidence separate from officer-managed records."""
    if not row.website_url:
        return None
    result, _ = PublicUniversity.objects.get_or_create(registered_university=row, defaults={
        'identity_key': hashlib.sha256(('registered:' + str(row.uuid)).encode()).hexdigest(),
        'name': row.name, 'website': row.website_url, 'address': row.admissions_office_address or row.location,
        'country': row.country, 'discovery_sources': [{'url': row.website_url, 'source': 'enrolled_university_website'}]})
    return result


def add_reference(ctx, row):
    ref = reference(row)
    refs = ctx.setdefault('university_references', {})
    refs[ref['id']] = ref
    return ref


def queue_research(row, user=None):
    active = row.runs.filter(status__in=['queued', 'running']).first()
    if active:
        return active
    try:
        with transaction.atomic():
            # Serialize admission by institution; no network/model I/O in this lock.
            PublicUniversity.objects.select_for_update().get(pk=row.pk)
            active = row.runs.filter(status__in=['queued', 'running']).first()
            if active:
                return active
            recent = row.runs.filter(created_at__gte=timezone.now()-timedelta(minutes=5)).order_by('-created_at').first()
            if recent:
                return recent
            if ResearchRun.objects.filter(status__in=['queued', 'running']).count() >= 200:
                raise ValueError('Research capacity is full. Try refreshing later.')
            return ResearchRun.objects.create(university=row, requested_by=user, available_at=timezone.now())
    except IntegrityError:
        return row.runs.get(status__in=['queued', 'running'])


def public_from_candidate(candidate):
    from .web import canonical_url
    url = canonical_url(candidate['website'])
    key = hashlib.sha256((urlsplit(url).netloc.removeprefix('www.') + '|' + candidate['name'].casefold().strip() + '|' + candidate.get('address', '').casefold().strip()).encode()).hexdigest()
    return PublicUniversity.objects.get_or_create(identity_key=key, defaults={
        'name': candidate['name'], 'website': url, 'address': candidate.get('address', ''),
        'country': candidate.get('country', ''), 'discovery_sources': candidate.get('sources', [])})[0]


def retrieve(row, question=''):
    from knowledge.vectors import enabled, embed_texts, MODEL
    from pgvector.django import CosineDistance
    facts = row.facts.select_related('page').order_by('-fetched_at')
    selected = []
    if enabled() and question:
        try:
            with transaction.atomic():
                vector = embed_texts([question], query=True)[0]
                selected = list(facts.filter(embedding_model=MODEL, embedding__isnull=False).annotate(distance=CosineDistance('embedding', vector)).filter(distance__lt=.7).order_by('distance')[:10])
        except Exception:
            selected = []
    if not selected:
        tokens = re.findall(r'\w{3,}', question)[:15]
        query = Q()
        for token in tokens:
            query |= Q(topic__icontains=token) | Q(content__icontains=token)
        selected = list(facts.filter(query)[:10]) if tokens else list(facts[:10])
    return {'university': reference(row), 'facts': [{'topic': f.topic, 'content': f.content,
        'source_url': f.page.url, 'source_quote': f.source_quote, 'fetched_at': f.fetched_at.isoformat()} for f in selected],
        'courses': list(row.courses.values('name', 'level', 'duration', 'study_mode', 'tuition', 'currency', 'requirements', 'page__url', 'fetched_at')[:30]),
        'intakes': list(row.intakes.values('course_name', 'term', 'year', 'deadline', 'applicant_scope', 'page__url', 'fetched_at')[:30]),
        'limits': 'Website coverage is partial. Only documented details are known; empty fields are unknown.'}
