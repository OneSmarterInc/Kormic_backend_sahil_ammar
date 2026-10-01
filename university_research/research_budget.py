"""One paid research request per chat job, including worker recovery."""
from contextlib import contextmanager
from contextvars import ContextVar

research_execution = ContextVar('research_execution', default=None)


@contextmanager
def state(ctx):
    from pure_multi_agent.job_recovery import execution, ExecutionLost
    current = execution.get()
    research = research_execution.get()
    if current is None and research is not None:
        from django.db import transaction
        from .models import ResearchRun
        with transaction.atomic():
            run = ResearchRun.objects.select_for_update().filter(pk=research[0], lease_token=research[1], status='running').first()
            if run is None:
                raise ExecutionLost('Research worker ownership changed')
            value = dict(run.state.get('_claude_research', {}))
            yield value
            run.state = {**run.state, '_claude_research':value}
            run.save(update_fields=['state'])
            ctx['claude_research'] = value
        return
    if current is None:
        yield ctx.setdefault('claude_research', {})
        return
    from django.db import transaction
    from django_api.models import AgentJob
    with transaction.atomic():
        job = AgentJob.objects.select_for_update().filter(
            pk=current[0], execution_token=current[1], status='processing').first()
        if job is None:
            raise ExecutionLost('Worker ownership changed')
        value = dict(job.payload.get('claude_research', {}))
        yield value
        job.payload = {**job.payload, 'claude_research': value}
        job.save(update_fields=['payload'])
        ctx['claude_research'] = value


def cached(ctx, domain):
    with state(ctx) as value:
        return value.get('page') if value.get('domain') == domain else None


def claim(ctx, domain):
    with state(ctx) as value:
        if value.get('attempted'):
            raise ValueError('The single Claude research call for this task has already been used. Use the available information; do not retry Claude.')
        value.update(attempted=True, domain=domain)
    # Clear the same-turn automatic crawl even if parsing or persistence fails.
    from urllib.parse import urlsplit
    from url_discovery.domain_policy import root_domain
    from .models import PublicUniversity
    pending = set(ctx.get('research_after_reply', []))
    ctx['research_after_reply'] = pending
    for row in PublicUniversity.objects.filter(pk__in=list(pending)):
        if root_domain(urlsplit(row.website).hostname or '') == domain:
            pending.discard(str(row.pk))


def raw_response(ctx, domain):
    with state(ctx) as value:
        return value.get('response') if value.get('domain') == domain else None


from github_profiles.scheduling import retry_database


@retry_database
def preserve_response(ctx, domain, raw, model, topics):
    with state(ctx) as value:
        value.update(domain=domain, response={'raw':raw, 'model':model, 'topics':topics})


def complete(ctx, domain, page):
    with state(ctx) as value:
        value.update(domain=domain, page=page)
