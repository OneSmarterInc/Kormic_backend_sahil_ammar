"""Collect -> commit -> common university agent -> student agent."""
from urllib.parse import urlsplit
from pure_multi_agent.telemetry import traced_operation, emit
from university_research import services


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

    ref = services.reference(row)
    if not row.coverage.get('catalogue_saved') and not ref['stale'] and (row.facts.exists() or row.courses.exists()):
        # Existing local research must not be scraped again just because it
        # predates the shared catalogue format.
        from university_research.catalogue import save_catalogue
        page = {'url':row.website, 'evidence_provider':'scraper_extracted', 'catalogue':{
            'facts':list(row.facts.values('topic','content')),
            'courses':list(row.courses.values('name','level','duration','study_mode','tuition','currency','seats','academic_year','requirements')),
            'intakes':list(row.intakes.values('course_name','term','year','deadline','applicant_scope'))}}
        save_catalogue(row, page)
        row.refresh_from_db()
    if not row.coverage.get('catalogue_saved') or ref['stale']:
        from university_research.claude_fallback import search_official_evidence
        from university_research.catalogue import save_catalogue
        from url_discovery.domain_policy import root_domain
        domain = root_domain(urlsplit(row.website).hostname or '')
        page = next((p for p in ctx.get('read_web_pages', {}).values()
            if root_domain(urlsplit(p.get('url','')).hostname or '') == domain), None)
        topics = 'university identity, official website, complete known course catalog, fees and seats, scholarships, admissions, eligibility, intakes, housing, amenities, contacts'
        if page is None:
            from university_research.web import read_page_once
            try:
                page = read_page_once(row.website)
            except Exception as exc:
                emit('AGENT_PROGRESS', 'collect_university_information', outputs={
                    'summary':'The website could not be read. Requesting the university catalogue from Claude.', 'reason':str(exc)})
                page = search_official_evidence(row.website, topics)
        if not page.get('catalogue'):
            # A readable page is extracted, not returned as raw text. Legacy
            # Claude-only cached replies are converted to the same schema.
            page = search_official_evidence(row.website, topics,
                source_page=page if page.get('content') and not page.get('provider_answer') else None)
        if not any(page.get('catalogue', {}).get(key) for key in ('description','facts','courses','intakes')):
            raise ValueError('No usable university information was returned. Please retry the request.')
        canonical = save_catalogue(row, page)
        row.refresh_from_db()
    else:
        canonical = row.registered_university
    # Remove superseded delayed publication so an old acknowledgement cannot
    # overwrite this committed catalogue or queue duplicate research.
    ctx['university_cache_after_reply'] = {key:value for key,value in ctx.get('university_cache_after_reply', {}).items()
        if value['university_id'] != str(row.pk)}
    ctx.setdefault('research_after_reply', set()).discard(str(row.pk))
    services.add_reference(ctx, row)
    from pure_multi_agent.registered_adviser import _consult
    return _consult(ctx, canonical, question, public_row=row)
