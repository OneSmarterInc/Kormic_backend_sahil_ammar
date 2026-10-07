"""Collect -> commit -> common university agent -> student agent."""
from urllib.parse import urlsplit
from pure_multi_agent.telemetry import traced_operation, emit
from university_research import services


def useful_catalogue(page, question):
    """A successful HTTP fetch or coverage note alone is not research content."""
    import re
    data = page.get('catalogue') or {}
    if not any(data.get(key) for key in ('description', 'facts', 'courses', 'intakes')):
        return False
    groups = (
        (r'courses?|programmes?|programs?|degrees?', ('courses',), r'courses?|programmes?|programs?|degrees?'),
        (r'fees?|tuition|costs?|seats?', ('courses',), r'fees?|tuition|costs?|seats?'),
        (r'admissions?|deadlines?|timeline|intakes?|eligibility|requirements?', ('intakes', 'eligibility_criteria'), r'admissions?|deadlines?|intakes?|eligibility|requirements?'),
        (r'scholarships?|funding|housing|hostels?|amenities', (), r'scholarships?|funding|housing|hostels?|amenities'),
    )
    requested = [g for g in groups if re.search(r'\b(?:'+g[0]+r')\b', question, re.I)]
    if not requested:
        return True
    text = ' '.join([data.get('description', ''), *[f.get('topic','')+' '+f.get('content','') for f in data.get('facts',[])]])
    return any(any(data.get(key) for key in keys) or re.search(r'\b(?:'+pattern+r')\b', text, re.I)
        for _, keys, pattern in requested)


@traced_operation('Common University Agent')
def consult(ctx, row, question):
    # Enrollment always wins, including a university enrolled after discovery.
    enrolled = services.registered().filter(pk=row.registered_university_id).first()
    if not enrolled:
        matches = services.search_registered(row.name)
        enrolled = matches.first() if matches.count() == 1 else None
    if enrolled:
        from pure_multi_agent.registered_adviser import consult as registered_consult
        return registered_consult(ctx, enrolled, question)

    from university_research.catalogue import save_catalogue
    from university_research.web import read_page_once, search_official_site
    from university_research.claude_fallback import extract_catalogue_locally
    from pure_multi_agent.registered_adviser import _consult
    from url_discovery.domain_policy import root_domain
    from github_profiles.scheduling import CapacityBusy
    from pure_multi_agent.capacity import AgentBusy
    from pure_multi_agent.model_router import AIServiceUnavailable
    ref = services.reference(row)
    cached = services.retrieve(row, question)
    has_saved = bool(cached.get('facts') or cached.get('courses') or cached.get('intakes') or row.coverage.get('catalogue_saved'))
    domain = root_domain(urlsplit(row.website).hostname or '')
    ctx['research_university_name'] = row.name
    # Cache reads are the default. Refresh only when stale or a requested topic is absent.
    from pure_multi_agent.answer_context import requested_topic_missing
    needs_collection = not has_saved or ref['stale'] or requested_topic_missing(cached, question)
    if needs_collection and str(row.pk) not in ctx.setdefault('collection_attempted', []):
        ctx['collection_attempted'].append(str(row.pk))
        from university_research.collection_jobs import collect_once, research_scope, public_collection_query
        scope = research_scope(row, question, ctx)
        public_question = public_collection_query(scope, question)
        def collect_public_evidence(owned):
            page = next((p for p in ctx.get('read_web_pages', {}).values()
                if root_domain(urlsplit(p.get('url','')).hostname or '') == domain), None)
            try:
                if page is None:
                    candidates = search_official_site(row.website, public_question)
                    target = candidates[0]['url'] if candidates else row.website
                    page = read_page_once(target, base_url=row.website)
                if not page.get('catalogue'):
                    # Save readable partial evidence even if local extraction fails.
                    services.save_live_page(row, page, public_question)
                    page = extract_catalogue_locally(row.website, public_question, page)
                if not useful_catalogue(page, public_question):
                    raise ValueError('The page does not cover the requested information.')
            except (CapacityBusy, AgentBusy):
                raise
            except Exception as exc:
                ctx['university_topic_gap'] = 'Some requested information could not be collected. Answer the available saved details and explain the missing part.'
                emit('AGENT_PROGRESS','university_collection',outputs={'status':'partial','error_type':type(exc).__name__})
                if domain in ctx.get('new_university_domains', []):
                    from university_research.new_university import research
                    try:
                        page = research(ctx, row.website, public_question, page)
                    except (CapacityBusy, AgentBusy):
                        raise
                    except Exception as research_error:
                        page = None
                        emit('AGENT_PROGRESS','university_research',outputs={'status':'unavailable','error_type':type(research_error).__name__})
                else:
                    page = None
            if page and page.get('catalogue'):
                # Save failures propagate distinctly; never claim publication.
                if not owned():
                    raise CapacityBusy('Public research ownership expired; retry this collection.', 5)
                save_catalogue(row, page)
                return True
            return False
        outcome = collect_once(row, scope, collect_public_evidence)
        row.refresh_from_db()
        if outcome == 'busy':
            ctx['university_topic_gap'] = ('Official research for this programme and topic is still being collected. '
                'Use existing evidence only and explain what remains unverified.')
        elif outcome == 'failed':
            ctx['university_topic_gap'] = ('Official research for this programme and topic could not be completed yet. '
                'Use existing evidence only and explain what remains unverified.')
        ctx.setdefault('research_after_reply', set()).discard(str(row.pk))
    ctx.setdefault('research_after_reply', set()).discard(str(row.pk))
    ctx['university_cache_after_reply'] = {key:value for key,value in ctx.get('university_cache_after_reply',{}).items() if value['university_id'] != str(row.pk)}
    services.add_reference(ctx,row)
    return _consult(ctx,row,question,public_row=row)
