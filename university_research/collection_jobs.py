"""Coalesce public website collection without sharing student advice or history."""

import hashlib
import json
import re
import time
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import PublicCollectionJob


def research_scope(row, question, context):
    from pure_multi_agent.answer_context import study_focus

    focus = study_focus(context.get('study_focus') or {}, question)
    degree = str(focus.get('target_degree') or '').casefold()
    subject = str(focus.get('target_subject') or '').casefold()
    if re.search(r'\bCS\b', question, re.I):
        subject = 'computer science'
    elif re.search(r'\bAI\b', question, re.I):
        subject = 'artificial intelligence'
    degree = 'masters' if re.fullmatch(r"m[sc]|mtech|master(?:'s|s)?", degree) else degree
    programme = ' '.join(part for part in (degree, subject) if part)
    # An unknown programme must not absorb a different programme's research.
    explicit_programme = re.search(
        r"\b(?:MS|MSc|MTech|Masters?|Master's|PhD)\s+(?:in\s+)?([A-Za-z][A-Za-z &-]{2,60})",
        question, re.I)
    if explicit_programme:
        named = re.split(r'\b(?:requirements?|eligibility|tuition|fees?|deadlines?|with|for|in fall|in spring)\b',
            explicit_programme.group(1), maxsplit=1, flags=re.I)[0].strip(' ?,-').casefold()
        if named and named not in ('program', 'programme', 'degree'):
            programme = degree + ' ' + named
    if not degree or not programme.strip() or (not subject and not explicit_programme):
        programme = 'unspecified:' + uuid.uuid4().hex
    intake = str(focus.get('target_intake') or '').casefold() or 'unspecified'
    lower = question.casefold()
    profile = context.get('student_profile') or {}
    if re.search(r'\binternational\b', lower):
        category = 'international'
    elif re.search(r'\bdomestic\b', lower):
        category = 'domestic'
    else:
        student_country = str(profile.get('country') or '').upper()
        university_country = str(row.country or '').upper()
        category = ('domestic' if student_country == university_country else 'international') if (
            len(student_country) == len(university_country) == 2) else 'unspecified'
    tags = []
    for label, pattern in (
        ('english-general', r'\benglish\b'),
        ('ielts', r'\bielts\b'),
        ('toefl', r'\btoefl\b'),
        ('gre', r'\bgre\b'),
        ('gmat', r'\bgmat\b'),
        ('gpa', r'\b(?:gpa|grades?|marks?)\b'),
        ('admissions', r'\b(?:admission|eligib|prerequisite|requirement)'),
        ('tuition', r'\b(?:tuition|fees?)\b'),
        ('living-cost', r'\b(?:living costs?|cost of living)\b'),
        ('funding', r'\b(?:scholarship|funding|financial aid)\b'),
        ('assistantship', r'\b(?:assistantship|teaching assistant|research assistant)\b'),
        ('deadlines', r'\b(?:deadline|intake|application date)\b'),
        ('housing', r'\b(?:housing|hostel|accommodation)\b'),
    ):
        if re.search(pattern, lower):
            tags.append(label)
    if re.search(r'\b(?:typical|average|admitted students?)\b', lower):
        tags.append('admitted-profile')
    topic = ','.join(tags) if tags else 'unspecified:' + uuid.uuid4().hex
    return {'programme': programme, 'intake': intake,
            'applicant_category': category, 'requested_topic': topic}


def _scope_key(scope):
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def public_collection_query(scope, question):
    """Use only public scope fields for shared work when the scope is known."""
    from pure_multi_agent.turn_policy import public_research_query

    programme = scope.get('programme', '')
    topic = scope.get('requested_topic', '')
    if programme.startswith('unspecified:') or topic.startswith('unspecified:'):
        # Ambiguous requests are not coalesced; retain their public wording.
        return public_research_query(question)
    return ' '.join(str(scope.get(field, '')).replace(',', ' ')
        for field in ('programme', 'intake', 'applicant_category', 'requested_topic')
        if scope.get(field) and scope[field] != 'unspecified')[:300]


def _claim(row, scope):
    key = _scope_key(scope)
    for _ in range(3):
        now = timezone.now()
        token = uuid.uuid4()
        try:
            with transaction.atomic():
                job = PublicCollectionJob.objects.select_for_update().filter(
                    university=row, scope_key=key).first()
                if job is None:
                    job = PublicCollectionJob.objects.create(university=row, scope_key=key,
                        scope=scope, status='running', lease_token=token,
                        lease_expires_at=now + timedelta(minutes=5))
                    return job, token, 'owned'
                if job.status == 'completed' and job.completed_at and job.completed_at > now - timedelta(days=1):
                    return job, None, 'recent'
                if job.status == 'running' and job.lease_expires_at and job.lease_expires_at > now:
                    return job, None, 'joined'
                if job.status == 'failed' and job.lease_expires_at and job.lease_expires_at > now:
                    return job, None, 'failed'
                job.scope = scope
                job.status = 'running'
                job.lease_token = token
                job.lease_expires_at = now + timedelta(minutes=5)
                job.completed_at = None
                job.save(update_fields=['scope', 'status', 'lease_token', 'lease_expires_at', 'completed_at', 'updated_at'])
                return job, token, 'owned'
        except IntegrityError:
            continue
    raise RuntimeError('Could not claim public research scope')


def collect_once(row, scope, collect, *, wait_seconds=25):
    """Return outcome; only the owner runs network/model work and persists it."""
    deadline = time.monotonic() + wait_seconds
    while True:
        job, token, status = _claim(row, scope)
        if status == 'owned':
            try:
                def owned():
                    return PublicCollectionJob.objects.filter(pk=job.pk, status='running',
                        lease_token=token, lease_expires_at__gt=timezone.now()).exists()
                saved = bool(collect(owned))
            except BaseException as exc:
                from github_profiles.scheduling import CapacityBusy
                from pure_multi_agent.capacity import AgentBusy
                cooldown = 5 if isinstance(exc, (CapacityBusy, AgentBusy)) else 30
                PublicCollectionJob.objects.filter(pk=job.pk, lease_token=token).update(
                    status='failed', lease_token=None,
                    lease_expires_at=timezone.now() + timedelta(seconds=cooldown))
                raise
            updated = PublicCollectionJob.objects.filter(pk=job.pk, lease_token=token).update(
                status='completed' if saved else 'failed', lease_token=None,
                completed_at=timezone.now() if saved else None,
                lease_expires_at=None if saved else timezone.now() + timedelta(seconds=30))
            if not updated:
                # A replacement worker owns an expired lease; this caller must
                # not report its stale work as the shared result.
                return 'busy'
            return 'completed' if saved else 'failed'
        if status in ('recent', 'failed'):
            return status
        if time.monotonic() >= deadline:
            return 'busy'
        time.sleep(min(0.8, max(0, deadline - time.monotonic())))
