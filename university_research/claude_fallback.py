"""Official-source extraction with a bounded Claude parsing fallback."""
from urllib.parse import urlsplit
from url_discovery.domain_policy import root_domain


def citation_pages(response, domain):
    pages = {}
    for block in response.content:
        if getattr(block, 'type', '') != 'text':
            continue
        for citation in getattr(block, 'citations', []) or []:
            url = getattr(citation, 'url', '')
            quote = getattr(citation, 'cited_text', '')
            if getattr(citation, 'type', '') != 'web_search_result_location' or not quote:
                continue
            if urlsplit(url).scheme != 'https' or root_domain(urlsplit(url).hostname or '') != domain:
                continue
            page = pages.setdefault(url, {'url':url, 'title':getattr(citation, 'title', ''), 'content':'', 'links':[], 'evidence_provider':'claude_web_search'})
            if quote not in page['content']:
                page['content'] += ('\n' if page['content'] else '') + quote
    return list(pages.values())


def search_official_evidence(url, topics='courses fees seats', *, source_page=None, ctx=None):
    """Extract only fetched official evidence; never manufacture a catalogue from memory."""
    import json
    from langchain_core.messages import SystemMessage, HumanMessage
    from pure_multi_agent.model_router import invoke, InvalidLocalToolResponse
    from .web import require_institution_site, search_official_site, read_page_once
    from .catalogue import Catalogue, CATALOGUE_INSTRUCTION
    from .research_budget import cached, claim, complete
    ctx = ctx if ctx is not None else {}
    domain = root_domain(urlsplit(url).hostname or '')
    require_institution_site(url)
    previous = cached(ctx, domain)
    if previous and previous.get('grounded_in_source'):
        return previous
    pages = []
    if source_page and source_page.get('content') and source_page.get('evidence_provider') != 'claude_direct':
        pages.append(source_page)
    if not pages:
        results = search_official_site(url, topics)
        for target in list(dict.fromkeys([r['url'] for r in results[:3]] + [url]))[:3]:
            try:
                page = read_page_once(target, base_url=url)
            except (ValueError, OSError):
                continue
            if page.get('content'):
                pages.append(page)
                break
        if not pages:
            raise ValueError('No official source document could be fetched; research was not saved.')
    # Retain the actual URL rather than claiming the homepage contains programme facts.
    source = pages[0]
    try:
        result = extract_catalogue_locally(url, topics, source)
        if not any(result.get('catalogue', {}).get(key) for key in (
                'description', 'contact_email', 'contact_phone', 'eligibility_criteria',
                'facts', 'courses', 'intakes', 'scholarships')):
            raise ValueError('Local extraction returned no source information.')
        from pure_multi_agent.answer_checks import violations
        if violations(json.dumps(result.get('catalogue', {}), ensure_ascii=False), source):
            raise ValueError('Local extraction contains unsupported values.')
    except (ValueError, InvalidLocalToolResponse):
        # Keep verbatim fetched evidence when structured extraction fails. Never
        # replace a missing extraction with university details from model memory.
        result = {**source, 'catalogue': {'facts': [{
            'topic': (source.get('title') or 'Official university information')[:200],
            'content': source['content'][:6000],
        }], 'coverage_notes': 'Source excerpt retained; structured extraction was unavailable.'},
            'evidence_provider': 'scraper_excerpt'}
    result['grounded_in_source'] = True
    from pure_multi_agent.answer_checks import violations
    if violations(json.dumps(result.get('catalogue', {}), ensure_ascii=False), source):
        raise ValueError('Extracted catalogue contains unsupported numbers or URLs; it was not saved.')
    # Do not use the one-paid-call cache for free extractions of different topics.
    if ctx.get('claude_research', {}).get('attempted'):
        complete(ctx, domain, result)
    return result


def extract_catalogue_locally(url, topics, source_page):
    """Algorithmic source extraction; the historical function name is retained."""
    from .catalogue import Catalogue, CourseData
    from .extraction_cache import get_or_extract
    from knowledge.scraper import page_chunks
    content = source_page.get('content', '')
    if not content.strip():
        raise ValueError('A fetched source document is required for extraction.')
    def extract():
        facts = [{'topic': (source_page.get('title') or 'Official university information')[:200], 'content': chunk}
                 for chunk in page_chunks(content, size=5500, overlap=0)]
        courses, scholarships = [], []
        for entity in source_page.get('structured_entities', []):
            values = entity['values']
            if entity['kind'] == 'academics':
                courses.append({key: str(value) for key, value in values.items() if key in CourseData.model_fields})
            elif entity['kind'] == 'scholarships':
                limits = {'name': 400, 'amount': 500, 'eligibility': 2000, 'deadline': 300, 'application_url': 1000}
                scholarships.append({key: str(value)[:limit] for key, limit in limits.items() if (value := values.get(key))})
        return Catalogue(facts=facts[:80], courses=courses[:100], scholarships=scholarships[:100],
            coverage_notes='Algorithmic extraction of fetched source text; unconfirmed structured fields remain empty.').model_dump()
    catalogue, _ = get_or_extract(content=content, schema_version='algorithmic-catalogue-v2',
        instructions_version='source-dom-and-verbatim-chunks-v2', model_version='no-llm',
        source_url=source_page.get('url') or url, extract=extract, validate=lambda data: Catalogue.model_validate(data))
    return {**source_page, 'catalogue': catalogue, 'evidence_provider': 'scraper_extracted'}
