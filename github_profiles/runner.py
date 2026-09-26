"""One short collection/agent slice per claim; users rotate fairly through workers."""
import base64
import hashlib
import json
import logging
from collections import Counter
from urllib.parse import quote
from django.conf import settings
from django.utils import timezone
from django.db import OperationalError
from django_api.models import GitHubRepository, GitHubRepositoryReport
from . import sync
from .agent import RepositoryAgent, ANALYSIS_VERSION
from .academic import recommend_masters
from .errors import ServiceError
from .github import PROFILE_FIELDS, REPO_FIELDS
from .overview import factual_overview, synthesize_overview
from .redaction import redact
from .scheduling import fenced, owned, release, LeaseLost, CapacityBusy

logger = logging.getLogger(__name__)


def refresh_facts(run):
    snapshot = run.profile
    repos = list(snapshot.repositories.filter(active=True).order_by('id'))
    reports = {r.pk: r for r in GitHubRepositoryReport.objects.filter(repository__profile=snapshot,
        pk__in=list(run.work.get('reports', {}).values()))}
    languages, topics, tech, domains = Counter(), Counter(), Counter(), Counter()
    projects = []
    for repo in repos:
        languages.update(repo.languages.keys() or ([repo.metadata['language']] if repo.metadata.get('language') else []))
        topics.update(repo.metadata.get('topics') or [])
        project = {**repo.metadata, 'id': repo.pk, 'name': repo.name, 'full_name': repo.full_name,
            'owner_login': repo.owner_login, 'fork': repo.fork}
        report = reports.get(run.work.get('reports', {}).get(str(repo.pk)))
        if report and report.repository_id == repo.pk:
            project.update(analysis=report.data, analysis_sha=report.sha)
            tech.update(s['name'] for s in report.data.get('skills', []))
            domains.update(report.data.get('domains', []))
        projects.append(project)
    snapshot.statistics = {'repositories': len(repos), 'owned': sum(r.owner_login.lower() == snapshot.identity['login'].lower() for r in repos),
        'private': sum(r.private for r in repos), 'forks': sum(r.fork for r in repos), 'stars': sum(r.metadata.get('stargazers_count') or 0 for r in repos)}
    snapshot.languages = [{'name': k, 'repositories': v} for k, v in languages.most_common()]
    snapshot.topics = [{'name': k, 'repositories': v} for k, v in topics.most_common()]
    snapshot.technologies = [{'name': k, 'projects': v} for k, v in tech.most_common()]
    snapshot.domains = [{'name': k, 'projects': v} for k, v in domains.most_common()]
    snapshot.coverage.update(source_projects_analyzed=len(reports), summary_projects_used=len(projects), readmes_truncated=sum(r.readme_truncated for r in repos))
    snapshot.summary = factual_overview(snapshot.identity, snapshot.statistics, snapshot.languages, projects, snapshot.coverage)
    snapshot.summary_kind = 'factual'
    snapshot.synced_at = timezone.now()
    sync.save_snapshot(run)
    return repos, projects


def initialize(run, gh):
    identity = gh.get('/user')
    if identity.get('id') != run.profile.github_user_id:
        raise ServiceError('GitHub account changed. Reconnect before syncing.')
    run.profile.identity = {k: identity.get(k) for k in PROFILE_FIELDS}
    run.profile.warnings = []
    run.profile.academic_guidance = {}
    run.profile.coverage = {'repository_list_complete': False, 'readme_character_limit': 12000,
        'source_file_limit': 10, 'source_character_budget': 18000, 'agent': 'langgraph',
        'note': 'All accessible repositories are inventoried. Source investigation is sampled and budgeted. Project technologies do not establish personal mastery or authorship. Code and tests are not executed.'}
    run.work = {'page': 1, 'seen': [], 'reports': {}, 'cursor': 0}
    sync.save_snapshot(run)
    run.stage = 'inventory'


def inventory(run, gh):
    page = run.work['page']
    sync.pulse(run, f'Collecting repository page {page}')
    batch = gh.get('/user/repos', {'sort': 'updated', 'per_page': 100, 'page': page})
    if not isinstance(batch, list):
        raise ServiceError('GitHub returned an invalid repository page.')
    with fenced(run):
        for row in batch:
            repo, _ = GitHubRepository.objects.get_or_create(profile=run.profile, github_id=row['id'],
                defaults={'name': row['name'], 'full_name': row['full_name']})
            if repo.metadata.get('pushed_at') != row.get('pushed_at'):
                repo.details_complete = False
            repo.name, repo.full_name = row['name'], row['full_name']
            repo.owner_login = (row.get('owner') or {}).get('login', '')
            repo.private, repo.fork, repo.active = bool(row.get('private')), bool(row.get('fork')), True
            repo.metadata = {k: row.get(k) for k in REPO_FIELDS}
            repo.save()
            run.work['seen'].append(repo.github_id)
        run.work['page'] += 1
        if len(batch) < 100:
            run.profile.repositories.exclude(github_id__in=run.work['seen']).update(active=False)
            run.profile.coverage['repository_list_complete'] = True
            run.stage = 'organizations'
            run.work['organization_page'] = 1
            run.profile.organizations = []
        sync.save_snapshot(run)


def organizations(run, gh):
    page = run.work.get('organization_page', 1)
    rows = gh.get('/user/orgs', {'per_page': 100, 'page': page})
    saved = {r['id']: r for r in run.profile.organizations}
    saved.update({r['id']: {k: r.get(k) for k in ('id', 'login', 'description', 'url')} for r in rows})
    run.profile.organizations = list(saved.values())
    run.work['organization_page'] = page+1
    if len(rows) < 100:
        run.stage = 'activity'
    sync.save_snapshot(run)


def activity(run, gh):
    rows = gh.get('/users/' + quote(run.profile.identity['login'], safe='') + '/events', {'per_page': 100})
    run.profile.recent_activity = [{'id': r.get('id'), 'type': r.get('type'), 'repository': (r.get('repo') or {}).get('name'), 'created_at': r.get('created_at')} for r in rows]
    begin_details(run)


def begin_details(run):
    run.work['repositories'] = list(run.profile.repositories.filter(active=True).order_by('id').values_list('pk', flat=True))
    run.work['cursor'] = 0
    run.stage = 'details'
    refresh_facts(run)


def details(run, gh):
    ids, cursor = run.work['repositories'], run.work['cursor']
    if cursor >= len(ids):
        run.stage, run.work['cursor'] = 'agent', 0
        refresh_facts(run)
        return
    repo = run.profile.repositories.get(pk=ids[cursor])
    sync.pulse(run, f'Collecting repository {cursor+1}/{len(ids)}: {repo.full_name}')
    if not repo.details_complete:
        languages = gh.get('/repos/' + repo.full_name + '/languages')
        readme = gh.get('/repos/' + repo.full_name + '/readme', optional=True)
        text = redact(base64.b64decode(readme.get('content', '')).decode('utf-8', errors='replace')) if readme and readme.get('encoding') == 'base64' else ''
        with fenced(run):
            repo.languages = languages
            repo.readme, repo.readme_truncated = text[:12000], len(text) > 12000
            repo.readme_url, repo.details_complete = (readme or {}).get('html_url') or '', True
            repo.save()
    run.work['cursor'] += 1


def agent_step(run, gh):
    ids, cursor = run.work['repositories'], run.work['cursor']
    if cursor >= len(ids):
        run.stage = 'finalize'
        return
    repo = run.profile.repositories.get(pk=ids[cursor])
    sync.pulse(run, f'Agent investigating {cursor+1}/{len(ids)}: {repo.full_name}')
    if not run.work.get('sha'):
        run.work['sha'] = gh.get('/repos/' + repo.full_name + '/commits/' + quote(repo.metadata.get('default_branch') or 'main', safe=''))['sha']
        return  # Freeze commit before the first durable graph step.
    sha = run.work['sha']
    report = repo.reports.filter(sha=sha, analysis_version=ANALYSIS_VERSION).first()
    if not report:
        report = RepositoryAgent(run, repo, gh, sha).advance()
    if report:
        run.work['reports'][str(repo.pk)] = report.pk
        run.work['cursor'] += 1
        run.work.pop('sha', None)
        refresh_facts(run)


def finalize(run):
    repos, projects = refresh_facts(run)
    model = sync.Inference(run)
    run.profile.academic_guidance = recommend_masters(run.profile.identity, projects)
    used_model = False
    def resumable_outline(messages, schema):
        nonlocal used_model
        key = hashlib.sha256(json.dumps([messages, schema], sort_keys=True).encode()).hexdigest()
        cache = run.work.setdefault('outline_cache', {})
        if key in cache:
            model.providers.add(cache[key]['provider'])
            return cache[key]
        if used_model:
            raise CapacityBusy('Continuing portfolio overview', delay=0)
        answer = model.chat(messages, schema)
        used_model = True
        cache[key] = answer
        with fenced(run):
            owned(run).update(work=run.work)
        return answer
    try:
        run.profile.summary = synthesize_overview(run.profile.identity, run.profile.statistics, run.profile.languages, projects, run.profile.coverage, resumable_outline)
        run.profile.summary_kind = '+'.join(sorted(model.providers)) or 'factual'
    except ServiceError as exc:
        run.profile.warnings.append({'resource': 'overview', 'detail': str(exc)})
    # Final student assessment and completion commit together; retries cannot duplicate it.
    with fenced(run):
        sync.save_snapshot(run)
        result = sync.finish_profile(run, repos, projects)
        owned(run).update(status='completed', progress='GitHub agent profile saved', result=result,
            stage='done', lease_token=None, lease_expires_at=None, updated_at=timezone.now())


def skip_failed_resource(run, error):
    run.profile.warnings.append({'resource': run.stage, 'detail': error})
    if run.stage == 'inventory':
        # Incomplete inventory must never retire previously saved repos.
        run.stage = 'organizations'
        run.work['organization_page'] = 1
        run.profile.organizations = []
    elif run.stage == 'organizations':
        run.stage = 'activity'
    elif run.stage == 'activity':
        begin_details(run)
    elif run.stage in ('agent', 'details'):
        run.work['cursor'] += 1
        run.work.pop('sha', None)
    else:
        raise ServiceError(error)
    sync.save_snapshot(run)


def execute_slice(run):
    try:
        sync.pulse(run)
        if (timezone.now()-run.created_at).total_seconds() > settings.GITHUB_RUN_MAX_SECONDS:
            raise ServiceError('Extraction time budget reached. Saved data is retained; sync again to continue.')
        if run.stage == 'finalize':
            finalize(run)
            return
        gh = sync.GitHub(sync.get_valid_access_token(run.profile.connection), progress=lambda: sync.pulse(run))
        step = {'collect': initialize, 'inventory': inventory, 'organizations': organizations,
            'activity': activity, 'details': details, 'agent': agent_step}[run.stage]
        try:
            step(run, gh)
        except LeaseLost:
            raise
        except ServiceError as exc:
            if run.stage == 'collect':
                raise
            # Retry transient resource errors with backoff before preserving partial data.
            if run.failures < 2 and run.stage != 'agent':
                release(run, delay=5*(run.failures+1), failures=run.failures+1, progress='Retrying GitHub collection')
                return
            skip_failed_resource(run, str(exc))
        release(run, stage=run.stage, work=run.work, failures=0)
    except CapacityBusy as exc:
        release(run, delay=exc.delay, progress=str(exc))
    except LeaseLost:
        pass  # A reclaimed lease must never change another worker's status.
    except OperationalError:
        # A short SQLite lock should not stall a local session for a full lease.
        # If the database itself is down, expiry still recovers the saved work.
        try:
            release(run, delay=2, progress='Waiting for database capacity')
        except (OperationalError, LeaseLost):
            pass
        raise
    except Exception as exc:
        logger.warning('GitHub extraction step failed: run=%s stage=%s type=%s', run.pk, run.stage, type(exc).__name__)
        try:
            if not isinstance(exc, ServiceError) and run.failures < 2:
                release(run, delay=5*(run.failures+1), failures=run.failures+1, progress='Retrying interrupted agent step')
            else:
                release(run, status='failed', error=str(exc) if isinstance(exc, ServiceError) else 'Extraction could not finish. Saved progress is retained; reconnect or sync again.')
        except LeaseLost:
            pass
