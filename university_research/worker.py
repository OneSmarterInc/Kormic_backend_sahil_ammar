import hashlib
import logging
import uuid
from datetime import timedelta

from django.db import transaction, close_old_connections
from django.db.models import Q
from django.utils import timezone
from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict
from github_profiles.scheduling import CapacityBusy, database_write_lock, retry_database
from pure_multi_agent.capacity import AgentBusy
from .models import ResearchRun, UniversityPage, UniversityFact, UniversityCourse, UniversityIntake
from .agent import graph_for

logger = logging.getLogger(__name__)


@retry_database
def claim():
    now = timezone.now()
    eligible = Q(status='queued', available_at__lte=now) | Q(status='running', lease_expires_at__lte=now)
    for pk in ResearchRun.objects.filter(eligible).order_by('available_at', 'created_at').values_list('pk', flat=True)[:20]:
        token = uuid.uuid4()
        with database_write_lock(), transaction.atomic():
            if ResearchRun.objects.filter(pk=pk).filter(eligible).update(status='running', lease_token=token, lease_expires_at=now+timedelta(seconds=300), updated_at=now):
                return ResearchRun.objects.select_related('university').get(pk=pk, lease_token=token)
    return None


def owned(run):
    return ResearchRun.objects.filter(pk=run.pk, status='running', lease_token=run.lease_token, lease_expires_at__gt=timezone.now())


@retry_database
def release(run, **changes):
    with database_write_lock():
        return owned(run).update(lease_token=None, lease_expires_at=None, updated_at=timezone.now(), **changes)


@retry_database
def publish(run, state):
    now = timezone.now()
    result, row = state['result'], run.university
    with database_write_lock(), transaction.atomic():
        if not owned(run).select_for_update().exists():
            return False
        for url, data in state['pages'].items():
            page, _ = UniversityPage.objects.update_or_create(university=row, url=url, defaults={
                'title': data['title'], 'content': data['content'], 'content_hash': hashlib.sha256(data['content'].encode()).hexdigest(), 'fetched_at': now, 'run': run})
            # Only replace records on successfully visited pages; unvisited data
            # retains its original date and remains visibly stale.
            UniversityFact.objects.filter(page=page).delete()
            UniversityCourse.objects.filter(page=page).delete()
            UniversityIntake.objects.filter(page=page).delete()
            for key, model in [('facts', UniversityFact), ('courses', UniversityCourse), ('intakes', UniversityIntake)]:
                for item in result[key]:
                    if item['source_url'] == url:
                        values = {k: v for k, v in item.items() if k != 'source_url'}
                        model.objects.create(university=row, page=page, fetched_at=now, **values)
                        if key != 'facts':
                            UniversityFact.objects.create(university=row, page=page, fetched_at=now,
                                topic=(key + ': ' + (item.get('name') or item.get('course_name') or item.get('term', '')))[:200],
                                content='; '.join(f'{k}: {v}' for k, v in values.items() if v and k != 'source_quote'), source_quote=item['source_quote'])
        row.fetched_at = now
        row.coverage = {'partial': True, 'pages_read': len(state['pages']), 'notes': result['coverage_notes'],
            'courses_extracted': len(result['courses']), 'intakes_extracted': len(result['intakes']), 'embedding_status': 'pending'}
        row.save(update_fields=['fetched_at', 'coverage'])
        state['phase'] = 'index'
        return bool(release(run, status='queued', state=state, available_at=now, progress='Indexing researched university information'))


def index_facts(run):
    from knowledge.vectors import enabled, embed_texts, MODEL
    row = run.university
    coverage = dict(row.coverage)
    if enabled():
        facts = list(row.facts.filter(embedding__isnull=True)[:32])
        if facts:
            vectors = embed_texts([f'{f.topic}\n{f.content}' for f in facts])
            with database_write_lock(), transaction.atomic():
                if not owned(run).select_for_update().exists():
                    return
                for fact, vector in zip(facts, vectors, strict=True):
                    UniversityFact.objects.filter(pk=fact.pk, content=fact.content).update(embedding=vector, embedding_model=MODEL)
                release(run, status='queued', available_at=timezone.now(), progress='Building pgvector search index')
            return
        coverage['embedding_status'] = 'indexed'
    else:
        coverage['embedding_status'] = 'lexical_development_mode'
    with database_write_lock(), transaction.atomic():
        if not owned(run).select_for_update().exists():
            return
        type(row).objects.filter(pk=row.pk).update(coverage=coverage)
        release(run, status='completed', completed_at=timezone.now(), progress='Research complete; sourced information is available')


def work_once():
    close_old_connections()
    run = claim()
    if run is None:
        return False
    try:
        state = dict(run.state)
        if state.get('phase') == 'index':
            index_facts(run)
            return True
        if run.steps >= 24 or run.created_at < timezone.now()-timedelta(hours=24):
            draft = state.get('draft', {})
            if any(draft.get(key) for key in ('facts', 'courses', 'intakes')):
                state['result'] = {key: draft.get(key, []) for key in ('facts', 'courses', 'intakes')}
                state['result']['coverage_notes'] = 'Processing limit reached. Only validated records were saved; unsupported records were excluded. ' + draft.get('coverage_notes', '')[:1700]
                publish(run, state)
            else:
                release(run, status='failed', error='Research reached its processing limit without supported records. Try updating later.', completed_at=timezone.now())
            return True
        if not state:
            state = {'messages': messages_to_dict([HumanMessage(content='Research official programs, admissions, fees, intakes and contacts; submit supported records.')]), 'pages': {}, 'result': {}, 'errors': 0}
        state['messages'] = messages_from_dict(state['messages'])
        output = graph_for(run.university.website, run.university.name).invoke(state)
        output['messages'] = messages_to_dict(output['messages'])
        if output.get('result'):
            publish(run, output)
        else:
            release(run, status='queued', state=output, steps=run.steps+1, attempts=0, available_at=timezone.now(),
                progress=f"Researching official pages ({len(output.get('pages', {}))}/8 read)")
    except (CapacityBusy, AgentBusy) as exc:
        release(run, status='queued', available_at=timezone.now()+timedelta(seconds=getattr(exc, 'delay', 10)), progress='Waiting for AI capacity; collected evidence is saved')
    except Exception:
        logger.exception('University research failed run=%s', run.pk)
        if run.attempts < 2:
            release(run, status='queued', attempts=run.attempts+1, available_at=timezone.now()+timedelta(seconds=30), progress='Retrying university research')
        else:
            release(run, status='failed', error='Research could not complete. Previously saved information is retained; try updating later.', completed_at=timezone.now())
    finally:
        close_old_connections()
    return True
