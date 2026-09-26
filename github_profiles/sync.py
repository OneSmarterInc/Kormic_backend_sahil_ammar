"""Durable collection and source analysis adapted from GitHub AI's sync worker."""
from collections import Counter
from datetime import timedelta
from django.conf import settings
from rest_framework.exceptions import Throttled

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.github_oauth import get_connection_for_student_id, get_valid_access_token, GitHubNotConnectedError
from django_api.models import (GitHubProfileSnapshot, GitHubSyncRun,
    GitHubAnalysis, StudentProfile)
from .errors import ServiceError
from .github import GitHub
from .inference import Inference
from .scheduling import heartbeat, owned, fenced, recover_legacy_runs



def expire_interrupted_runs():
    recover_legacy_runs()


def queue_sync(student_id):
    connection = get_connection_for_student_id(student_id)
    if connection is None:
        raise GitHubNotConnectedError('Connect your GitHub account before running analysis.')
    expire_interrupted_runs()
    try:
        with transaction.atomic():
            # A connection change creates a fresh ownership boundary.
            type(connection).objects.select_for_update().get(pk=connection.pk)
            existing = GitHubProfileSnapshot.objects.filter(student__uuid=student_id).first()
            if existing and (existing.connection_id != connection.pk or existing.github_user_id != connection.github_user_id):
                existing.delete()
            profile, _ = GitHubProfileSnapshot.objects.get_or_create(connection=connection,
                defaults={'student': StudentProfile.objects.get(uuid=student_id), 'github_user_id': connection.github_user_id})
            active = profile.runs.filter(status__in=['queued', 'running']).first()
            if active:
                return active
            if profile.runs.filter(created_at__gte=timezone.now()-timedelta(days=1)).count() >= settings.GITHUB_DAILY_SYNC_LIMIT:
                raise Throttled(wait=3600, detail='GitHub sync limit reached for this connected profile. Try again later.')
            return GitHubSyncRun.objects.create(profile=profile)
    except IntegrityError:
        return GitHubSyncRun.objects.get(profile__connection=connection, status__in=['queued', 'running'])


def pulse(run, progress=None):
    heartbeat(run)
    fields = {'updated_at': timezone.now()}
    if progress:
        fields['progress'] = progress[:300]
    changed = owned(run).update(**fields)
    if not changed:
        raise ServiceError('This GitHub sync is no longer active. Reconnect or sync again.')


def save_snapshot(run):
    pulse(run)
    snapshot = run.profile
    fields = ('identity', 'statistics', 'languages', 'topics', 'technologies', 'domains', 'organizations',
        'recent_activity', 'academic_guidance', 'summary', 'summary_kind', 'coverage', 'warnings', 'synced_at')
    with fenced(run):
        GitHubProfileSnapshot.objects.filter(pk=snapshot.pk).update(**{f: getattr(snapshot, f) for f in fields})


def finish_profile(run, repos, projects):
    """Keep the existing student/Aria assessment contract without copying raw source into chat."""
    snapshot = run.profile
    byte_counts = Counter()
    for repo in repos:
        byte_counts.update(repo.languages)
    total = sum(byte_counts.values()) or 1
    result = {'username': snapshot.identity['login'], 'name': snapshot.identity.get('name') or snapshot.identity['login'],
        'source': 'github', 'verified': True, 'verification_scope': 'OAuth identity and GitHub facts; inferred technologies are not verified personal mastery.',
        'generated_at': snapshot.synced_at.isoformat(), 'summary': snapshot.summary, 'admissions_summary': snapshot.summary,
        'aria_notes': 'Use source-backed GitHub findings as project evidence, not a skill rating or proof of personal authorship.',
        'primary_language': byte_counts.most_common(1)[0][0] if byte_counts else 'Unknown',
        'languages': [{'name': k, 'percent': round(v/total*100, 1)} for k, v in byte_counts.most_common()],
        'frameworks_and_tools': [r['name'] for r in snapshot.technologies], 'domains': snapshot.domains,
        'raw_signal_summary': snapshot.statistics, 'coverage': snapshot.coverage, 'warnings': snapshot.warnings,
        'academic_guidance': snapshot.academic_guidance,
        'strengths': [f"{r['name']} appears in inspected source in {r['projects']} projects." for r in snapshot.technologies[:4]],
        'honest_gaps': ['Source analysis is sampled; private work outside the OAuth grant and personal proficiency were not assessed.']}
    url = 'https://github.com/' + snapshot.identity['login']
    pulse(run)
    with fenced(run):
        student = StudentProfile.objects.select_for_update().get(pk=snapshot.student_id)
        skills = list(student.skills or [])
        added = [s for s in [r['name'] for r in result['languages']] + result['frameworks_and_tools'] if s not in skills]
        student.skills = list(dict.fromkeys(skills + added))[:80]
        student.github, student.github_assessment = url, result
        student.evidence = {**(student.evidence or {}), 'github': {'github_url': url, 'result': result}}
        student.save(update_fields=['github', 'github_assessment', 'skills', 'evidence', 'updated_at'])
        GitHubAnalysis.objects.create(student=student, github_url=url, result=result)
    return {'status': 'success', 'student_id': str(student.uuid), 'github_username': snapshot.identity['login'],
        'github_result': result, 'skills_added': list(dict.fromkeys(added))}


def execute_run(run_id):
    """Compatibility helper; production workers execute one fair slice at a time."""
    from .runner import execute_slice
    from .scheduling import claim
    for _ in range(10000):
        run = claim(run_id)
        if run is None:
            return
        execute_slice(run)
