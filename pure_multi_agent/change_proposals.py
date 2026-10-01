"""Transactional persistence for model-proposed edits. No conversational replies.

The model interprets intent; this boundary enforces ownership, exact snapshots,
later-turn consent and idempotency. Rejected student values never enter profiles.
"""
import hashlib
import json
import math
from copy import deepcopy
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from django_api.models import AgentChangeProposal, StudentProfile, UniversityKnowledgeEntry
from django_api.services import profile_row_to_dict, _apply_dict_to_profile

STUDENT_FIELDS = {
    'name', 'institution', 'major', 'program', 'gpa', 'gpa_scale', 'gre_quant',
    'gre_verbal', 'toefl', 'ielts', 'budget', 'graduation_year', 'work_months',
    'research', 'skills', 'projects', 'career_goals', 'research_interests',
    'preferences.preferred_intake', 'preferences.preferred_locations', 'preferences.funding_required',
}
UNIVERSITY_FIELDS = {'description', 'tagline', 'location', 'contact_email', 'contact_phone',
    'admissions_office_address', 'eligibility_criteria', 'best_fit_notes', 'not_best_fit_notes',
    'name', 'website_url', 'agent_name', 'scrape_urls', 'tone_descriptors', 'communication_style_notes',
    'never_do_notes', 'min_fit_score_threshold', 'priority_tier_bounds'}


def get_value(profile, key):
    if key.startswith('preferences.'):
        return (profile.get('preferences') or {}).get(key.split('.', 1)[1])
    return profile.get(key)


def with_values(profile, values):
    result = deepcopy(profile)
    for key, value in values.items():
        if key.startswith('preferences.'):
            result.setdefault('preferences', {})[key.split('.', 1)[1]] = value
        else:
            result[key] = value
    return result


def same(a, b):
    # The DB stores GPA scale as text; 4, 4.0 and "4.0" represent one scale.
    if a == b:
        return True
    if a is None or b is None or isinstance(a, (bool, list, dict)) or isinstance(b, (bool, list, dict)):
        return False
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def validate_student(values, profile):
    if not values or set(values) - STUDENT_FIELDS:
        raise ValueError('Provide supported profile fields only.')
    limits = {'gpa': (0, 100), 'gpa_scale': (1, 100), 'gre_quant': (130, 170), 'gre_verbal': (130, 170),
        'toefl': (0, 120), 'ielts': (0, 9), 'budget': (0, 100000000), 'work_months': (0, 1200),
        'graduation_year': (1900, 2200)}
    for key, value in values.items():
        if key in limits:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not limits[key][0] <= value <= limits[key][1]:
                raise ValueError(f'Invalid {key}; expected {limits[key]}. Ask the student to clarify.')
        elif key == 'preferences.funding_required':
            if not isinstance(value, bool):
                raise ValueError('funding_required must be true or false.')
        elif key in {'skills', 'projects', 'career_goals', 'research_interests', 'preferences.preferred_locations'}:
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or len(v) > 1000 for v in value):
                raise ValueError(f'{key} must be a list of at most 100 short strings.')
        elif not isinstance(value, str) or not value.strip() or len(value) > (6000 if key == 'research' else 255):
            raise ValueError(f'Invalid {key}.')
    merged = with_values(profile, values)
    gpa, scale = merged.get('gpa'), merged.get('gpa_scale')
    if {'gpa', 'gpa_scale'} & values.keys() and gpa is not None and scale:
        try:
            numeric_scale = 100 if str(scale).casefold() in ('percentage', '%') else float(scale)
            if float(gpa) > numeric_scale:
                raise ValueError('GPA exceeds its scale. Ask for the GPA and scale together.')
        except (TypeError, ValueError) as exc:
            raise ValueError('GPA and scale are inconsistent. Ask for both values.') from exc


def officer_university(ctx, lock=False):
    from accounts.models import Account
    from universities.models import University
    actor = ctx.get('actor_id')
    if not actor or not Account.objects.filter(user_id=actor, user__is_active=True,
            role=Account.Role.UNIVERSITY, university__uuid=ctx['university_id']).exists():
        raise ValueError('University officer access is no longer available.')
    rows = University.objects.select_for_update() if lock else University.objects
    return rows.get(uuid=ctx['university_id'])


def scoped(ctx):
    rows = AgentChangeProposal.objects.all()
    if ctx.get('canonical_student_id'):
        return rows.filter(student__uuid=ctx['canonical_student_id'])
    university = officer_university(ctx)
    return rows.filter(university=university, actor_id=ctx['actor_id'])


def serialize(row):
    return {'id': str(row.pk), 'kind': row.kind, 'operation': row.operation, 'target_id': row.target_id,
        'before': row.before, 'after': row.after, 'status': row.status,
        'assumption_active': row.assumption_active, 'created_at': row.created_at.isoformat()}


def remember(ctx, row):
    ctx.setdefault('change_proposals', {})[str(row.pk)] = serialize(row)
    return serialize(row)


def conversation_state(ctx):
    rows = scoped(ctx)
    cutoff = timezone.now() - timedelta(days=7)
    rows.filter(status='pending', created_at__lt=cutoff).update(status='expired', resolved_at=timezone.now())
    pending = list(rows.filter(status='pending').order_by('-created_at')[:20])[::-1]
    assumptions = {}
    for row in rows.filter(assumption_active=True).order_by('created_at'):
        assumptions.update(row.after)
    return {'pending_changes': [serialize(row) for row in pending], 'conversation_assumptions': assumptions,
        'assumptions_are_saved_profile_facts': False}


def effective_profile(ctx):
    assumptions = conversation_state(ctx)['conversation_assumptions']
    return with_values(ctx['student_profile'], assumptions), assumptions


def _new(ctx, kind, operation, target, before, after, **scope):
    data = [scope.get('student').pk if scope.get('student') else None,
        scope.get('university').pk if scope.get('university') else None, ctx.get('actor_id'),
        ctx['turn_id'], kind, operation, str(target), after]
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    existing = AgentChangeProposal.objects.filter(fingerprint=fingerprint).first()
    if existing:
        return existing
    if scoped(ctx).filter(turn_id=ctx['turn_id']).count() >= 20:
        raise ValueError('Too many edits in one message; split the changes into smaller requests.')
    return AgentChangeProposal.objects.create(**scope, actor_id=ctx.get('actor_id'), kind=kind,
        operation=operation, target_id=str(target), before=before, after=after, turn_id=ctx['turn_id'],
        source_message=ctx.get('current_message', ''), fingerprint=fingerprint)


def _refresh_student(ctx, row):
    pending_roadmap = ctx.get('student_profile', {}).get('roadmap')
    roadmap_changed = pending_roadmap != ctx.get('profile_baseline', {}).get('roadmap')
    fresh = profile_row_to_dict(row)
    # Direct writes have already committed. Do not replay them during context merge.
    ctx['student_profile'] = fresh
    ctx['profile_baseline'] = deepcopy(fresh)
    if roadmap_changed:
        ctx['student_profile']['roadmap'] = pending_roadmap


def _clear_assumed_fields(ctx, keys):
    for row in scoped(ctx).select_for_update().filter(assumption_active=True):
        remaining = {k: v for k, v in row.after.items() if k not in keys}
        # Preserve the audit snapshot; all assumptions from an atomic proposal
        # retire together when any of its values is subsequently corrected.
        if len(remaining) != len(row.after):
            row.assumption_active = False
            row.save(update_fields=['assumption_active'])


@transaction.atomic
def update_student(ctx, values):
    if ctx.get('chat_attachments') or ctx.get('documents_read'):
        raise ValueError('Document updates always need confirmation. Use propose_document_update instead of saving document facts directly.')
    row = StudentProfile.objects.select_for_update().get(uuid=ctx['canonical_student_id'])
    profile = profile_row_to_dict(row)
    validate_student(values, profile)
    missing, conflicts, unchanged = {}, {}, []
    for key, value in values.items():
        old = get_value(profile, key)
        if same(old, value):
            unchanged.append(key)
        elif old is None or old == '' or old == [] or old == {}:
            missing[key] = value
        else:
            conflicts[key] = value
    # GPA and its scale form one fact; never save half of a conflicting pair.
    if {'gpa', 'gpa_scale'} & conflicts.keys():
        for key in ('gpa', 'gpa_scale'):
            if key in missing:
                conflicts[key] = missing.pop(key)
    applied = None
    if missing:
        applied = _new(ctx, 'student_profile', 'update', row.uuid,
            {k: get_value(profile, k) for k in missing}, missing, student=row)
        _apply_dict_to_profile(row, with_values(profile, missing))
        row.save()
        applied.status, applied.resolved_at = 'applied', timezone.now()
        applied.save(update_fields=['status', 'resolved_at'])
        _clear_assumed_fields(ctx, missing)
        remember(ctx, applied)
    _refresh_student(ctx, row)
    proposal = None
    if conflicts:
        for previous in scoped(ctx).select_for_update().filter(kind='student_profile', status='pending'):
            if set(previous.after) & set(conflicts) and previous.after != conflicts:
                previous.status, previous.resolved_at = 'superseded', timezone.now()
                previous.save(update_fields=['status', 'resolved_at'])
                remember(ctx, previous)
        proposal = _new(ctx, 'student_profile', 'update', row.uuid,
            {k: get_value(profile, k) for k in conflicts}, conflicts, student=row)
        remember(ctx, proposal)
    if unchanged:
        _clear_assumed_fields(ctx, unchanged)
    return {'saved_missing_fields': missing, 'unchanged_fields': unchanged,
        'confirmation_required': serialize(proposal) if proposal else None,
        'instruction': 'Explain exactly what was saved. Ask before replacing existing values. Do not call resolve_profile_change in the proposing turn.'}


def validate_university(values):
    if not values or set(values) - UNIVERSITY_FIELDS:
        raise ValueError('Only supported university information and requirements may be edited here.')
    for key, value in values.items():
        if key == 'eligibility_criteria':
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('Requirements must be a list of at most 100 criteria.')
            for item in value:
                if not isinstance(item, dict) or not isinstance(item.get('criterion'), str) or not isinstance(item.get('detail'), str):
                    raise ValueError('Each requirement needs criterion and detail text.')
                if len(json.dumps(item)) > 4000:
                    raise ValueError('Requirement is too long.')
        elif key in ('scrape_urls', 'tone_descriptors'):
            if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in value):
                raise ValueError(f'{key} requires at most 100 non-empty strings.')
        elif key == 'min_fit_score_threshold':
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError('Fit threshold must be an integer between 0 and 100.')
        elif key == 'priority_tier_bounds':
            if not isinstance(value, dict) or set(value) != {'high', 'medium', 'low'} or any(type(v) is not int for v in value.values()) or not 100 >= value['high'] >= value['medium'] >= value['low'] >= 0:
                raise ValueError('Priority bounds require 100 >= high >= medium >= low >= 0.')
        elif not isinstance(value, str) or len(value) > 12000:
            raise ValueError(f'Invalid {key}.')


@transaction.atomic
def propose_university(ctx, kind, values, target_id=''):
    university = officer_university(ctx, lock=True)
    if kind == 'university_profile':
        validate_university(values)
        from django.core.validators import validate_email
        if values.get('contact_email'):
            validate_email(values['contact_email'])
        if 'eligibility_criteria' in values:
            from pure_multi_agent.entry_validation import CompleteRequirement
            old = university.eligibility_criteria or []
            for item in values['eligibility_criteria']:
                if item not in old:
                    CompleteRequirement.model_validate(item)
        if 'website_url' in values or 'scrape_urls' in values:
            from university_research.web import canonical_url, require_institution_site
            from url_discovery.domain_policy import root_domain
            from urllib.parse import urlsplit
            website = values.get('website_url', university.website_url)
            require_institution_site(canonical_url(website))
            for url in values.get('scrape_urls', university.scrape_urls or []):
                canonical_url(url)
                if root_domain(urlsplit(url).hostname) != root_domain(urlsplit(website).hostname):
                    raise ValueError('Research URLs must belong to the official university domain. Provide a matching source list when changing website.')
        # Respect the model's max lengths/validators before offering an edit.
        for key, value in values.items():
            university._meta.get_field(key).clean(value, university)
        before = {k: getattr(university, k) for k in values}
        target_id, operation = str(university.uuid), 'update'
    elif kind == 'university_knowledge':
        if set(values) != {'topic', 'content', 'group', 'details'} or not isinstance(values['topic'], str) or not values['topic'].strip() or len(values['topic']) > 500:
            raise ValueError('Provide a topic, content and knowledge group.')
        if not isinstance(values['content'], str) or not values['content'].strip() or len(values['content']) > 30000:
            raise ValueError('Policy content must contain 1–30000 characters.')
        if values['group'] not in ('admissions', 'international', 'money', 'campus_life'):
            raise ValueError('Choose a supported knowledge group.')
        from pure_multi_agent.entry_validation import validate_policy
        values['details'] = validate_policy(values['topic'], values['details'])
        before, operation = {}, 'create'
        if target_id:
            row = UniversityKnowledgeEntry.objects.select_for_update().filter(pk=target_id, university_id=str(university.uuid)).first()
            if not row:
                raise ValueError('Knowledge entry is not in this university.')
            before = {'topic': row.topic, 'content': row.content, 'group': row.group.slug if row.group else None, 'details': row.details}
            operation = 'update'
    elif kind == 'university_group':
        from universities.models import KnowledgeGroup
        from django.core.validators import validate_email
        if target_id not in KnowledgeGroup.Slug.values or set(values) != {'escalation_contact_name', 'escalation_contact_email'}:
            raise ValueError('Provide a valid department, contact name and email together.')
        if not values['escalation_contact_name'].strip() or len(values['escalation_contact_name']) > 255:
            raise ValueError('A contact name is required.')
        validate_email(values['escalation_contact_email'])
        group = KnowledgeGroup.objects.filter(university=university, slug=target_id).first()
        before = {key: getattr(group, key) if group else '' for key in values}
        operation = 'update'
    else:
        raise ValueError('Unsupported change type.')
    if before == values:
        return {'status': 'unchanged', 'instruction': 'These values are already saved.'}
    proposal = _new(ctx, kind, operation, target_id, before, values, university=university)
    return remember(ctx, proposal)


@transaction.atomic
def resolve(ctx, proposal_id, decision, confirmation_message):
    if decision not in ('approve', 'reject', 'cancel'):
        raise ValueError('Choose approve, reject or cancel.')
    # Require the actual later human message, not consent invented by a tool call.
    if not confirmation_message.strip() or confirmation_message.strip() != ctx.get('current_message', '').strip():
        raise ValueError('Quote the entire current human message as consent evidence.')
    try:
        row = scoped(ctx).select_for_update().get(pk=proposal_id)
    except (AgentChangeProposal.DoesNotExist, ValidationError):
        raise ValueError('Change proposal not found in this conversation.')
    if row.status != 'pending':
        return remember(ctx, row)
    if row.turn_id == ctx['turn_id']:
        raise ValueError('Show the exact proposal first; confirmation must come in a later user turn.')
    if row.created_at < timezone.now() - timedelta(days=7):
        row.status = 'expired'
    elif decision != 'approve':
        row.status = 'rejected' if decision == 'reject' else 'cancelled'
        if row.kind == 'student_profile':
            _clear_assumed_fields(ctx, row.after)
            row.assumption_active = decision == 'reject'
        if row.kind == 'student_document':
            from django_api.models import StudentDocumentEvidence
            StudentDocumentEvidence.objects.filter(pk=row.after['document_id'], student_id=row.student_id).exclude(status='confirmed').update(status='rejected' if decision == 'reject' else 'cancelled')
    elif row.kind == 'student_document':
        from pure_multi_agent.document_evidence import apply_document
        row.status = apply_document(ctx, row)
    elif row.student_id:
        profile_row = StudentProfile.objects.select_for_update().get(pk=row.student_id)
        profile = profile_row_to_dict(profile_row)
        if any(not same(get_value(profile, k), v) for k, v in row.before.items()):
            row.status = 'stale'
        else:
            validate_student(row.after, profile)
            _apply_dict_to_profile(profile_row, with_values(profile, row.after))
            profile_row.save()
            _clear_assumed_fields(ctx, row.after)
            _refresh_student(ctx, profile_row)
            row.status = 'applied'
    else:
        university = officer_university(ctx, lock=True)
        if row.kind == 'university_profile':
            if any(getattr(university, k) != v for k, v in row.before.items()):
                row.status = 'stale'
            else:
                validate_university(row.after)
                for key, value in row.after.items():
                    setattr(university, key, value)
                university.full_clean()
                university.save()
                _sync_university_profile(university, row.before)
                row.status = 'applied'
        elif row.kind == 'university_group':
            from universities.models import KnowledgeGroup
            group, _ = KnowledgeGroup.objects.select_for_update().get_or_create(university=university, slug=row.target_id)
            if any(getattr(group, k) != v for k, v in row.before.items()):
                row.status = 'stale'
            else:
                for key, value in row.after.items():
                    setattr(group, key, value)
                group.full_clean()
                group.save()
                row.status = 'applied'
        else:
            from universities.models import KnowledgeGroup
            entry = None
            if row.operation == 'update':
                entry = UniversityKnowledgeEntry.objects.select_for_update().filter(pk=row.target_id, university_id=str(university.uuid)).first()
                current = {'topic': entry.topic, 'content': entry.content, 'group': entry.group.slug if entry.group else None, 'details': entry.details} if entry else None
                if current != row.before:
                    row.status = 'stale'
            if row.status != 'stale':
                group, _ = KnowledgeGroup.objects.get_or_create(university=university, slug=row.after['group'])
                if entry is None:
                    entry = UniversityKnowledgeEntry(university_id=str(university.uuid))
                entry.topic, entry.content, entry.group = row.after['topic'], row.after['content'], group
                entry.details = row.after['details']
                entry.source_type, entry.source_url, entry.confidence = 'officer', None, 1.0
                entry.embedding, entry.embedding_hash, entry.embedding_model = None, '', ''
                entry.save()  # Existing signals invalidate retrieval and queue embedding work.
                row.target_id, row.status = str(entry.pk), 'applied'
    row.resolution_message, row.resolved_at = confirmation_message, timezone.now()
    row.save(update_fields=['status', 'assumption_active', 'resolution_message', 'resolved_at', 'target_id'])
    return remember(ctx, row)


def _sync_university_profile(university, before=None):
    """Publish confirmed profile facts without overwriting officer-created policies."""
    facts = {'Program Overview': university.description,
        'Contact Information': json.dumps({k: getattr(university, k) for k in ('contact_email', 'contact_phone', 'website_url', 'admissions_office_address')})}
    facts.update({f'Eligibility: {item["criterion"]}': json.dumps(item, ensure_ascii=False) for item in university.eligibility_criteria or [] if item.get('criterion')})
    legacy_topics = set(facts) | {f'Eligibility: {item["criterion"]}' for item in (before or {}).get('eligibility_criteria', []) if item.get('criterion')}
    UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), source_type='seed', topic__in=legacy_topics).delete()
    rows = UniversityKnowledgeEntry.objects.filter(university_id=str(university.uuid), source_type='university_profile')
    rows.exclude(topic__in=facts).delete()
    for topic, content in facts.items():
        if content:
            rows.update_or_create(topic=topic, defaults={'university_id': str(university.uuid), 'source_type': 'university_profile', 'content': content, 'confidence': 1.0})


def clear_conversation(*, student_id=None, university_id=None):
    rows = AgentChangeProposal.objects.filter(student__uuid=student_id) if student_id else AgentChangeProposal.objects.filter(university__uuid=university_id)
    rows.filter(status='pending').update(status='cancelled', resolved_at=timezone.now())
    rows.filter(assumption_active=True).update(assumption_active=False)
    if student_id:
        from django_api.models import StudentDocumentEvidence
        StudentDocumentEvidence.objects.filter(student__uuid=student_id).exclude(status='confirmed').update(status='cancelled')


def refresh_message_metadata(messages, *, student_id=None, university_id=None, actor_id=None):
    """Refresh card state in one scoped query when a saved transcript is loaded."""
    import uuid
    ids = set()
    for message in messages:
        for key in ('change_proposals', 'pending_changes'):
            for change in (message.meta or {}).get(key, []):
                try:
                    ids.add(uuid.UUID(change['id']))
                except (ValueError, KeyError, TypeError):
                    continue
    rows = AgentChangeProposal.objects.filter(pk__in=ids)
    rows = rows.filter(student__uuid=student_id) if student_id else rows.filter(university__uuid=university_id)
    state = {str(row.pk): serialize(row) for row in rows}
    for message in messages:
        message.meta = dict(message.meta or {})
        for key in ('change_proposals', 'pending_changes'):
            if key in message.meta:
                message.meta[key] = [state.get(change.get('id'), {**change, 'status': 'unavailable'}) for change in message.meta[key]]
