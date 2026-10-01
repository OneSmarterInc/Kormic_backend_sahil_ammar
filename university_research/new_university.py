"""Single paid research fallback, authorised only after a local directory miss."""
import json
from urllib.parse import urlsplit


def research(ctx, website, question, source_page=None):
    from url_discovery.domain_policy import root_domain
    from .research_budget import cached, claim, complete, preserve_response, raw_response
    from .catalogue import Catalogue, CATALOGUE_INSTRUCTION
    from pure_multi_agent.model_router import invoke
    from langchain_core.messages import HumanMessage, SystemMessage
    domain = root_domain(urlsplit(website).hostname or '')
    if domain not in ctx.get('new_university_domains', []):
        raise ValueError('Claude research is available only for a newly discovered university.')
    previous = cached(ctx, domain)
    if previous:
        return previous
    stored = raw_response(ctx, domain)
    if stored:
        raw = stored['raw']
    else:
        claim(ctx, domain)
        reply = invoke([SystemMessage(content=(
            'Answer the EXACT requested institution, programme, degree level and intake first. '
            'Prioritize the requested fields. Do not replace programme names with broad degree categories like Masters Programs, MPhil or PhD. '
            'Do not include undergraduate or doctoral programmes in a masters-only request. '
            'Do not invent generic deadlines, convert unknown costs or call work permits scholarships. '
            'Keep tuition separate from student-union/semester contributions and living expenses. '
            'Provide university information for the specified institution and request. '
            'Return JSON only: {"identity":{"name":"","website":"","country":""},"catalogue":{...}}. '
            'Do not browse or call tools. Use the supplied official website identity; do not substitute another university. '
            'Provide only information you know; keep unknown fields empty and explain coverage gaps. '
            'Keep fees with currency, period, programme and academic year. Never infer seats or deadlines. '
            'Your response is model-provided research, not proof that a website was fetched or independently verified. '
            + CATALOGUE_INSTRUCTION)), HumanMessage(content=json.dumps({
                'university':ctx.get('research_university_name',''), 'official_website':website,
                'question':question[:1200], 'available_page':(source_page or {}).get('content','')[:8000]}))],
            force_claude=True, single_attempt=True, research=True)
        raw = reply.content
        if isinstance(raw, list):
            raw = ''.join(b.get('text','') for b in raw if b.get('type') == 'text')
        preserve_response(ctx, domain, raw, reply.response_metadata.get('routing_model','claude'), question)
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[1].rsplit('```',1)[0]
    data = json.loads(raw)
    identity = data.get('identity') or {}
    if root_domain(urlsplit(identity.get('website') or website).hostname or '') != domain:
        raise ValueError('Research returned a different institution website.')
    catalogue = Catalogue.model_validate(data.get('catalogue',data)).model_dump()
    name = ctx.get('research_university_name') or identity.get('name') or domain
    page = {'url':website,'title':name,'content':json.dumps(catalogue,ensure_ascii=False),
            'links':[], 'catalogue':catalogue, 'provider_identity':{**identity,'name':name,'website':website},
            'evidence_provider':'claude_research','grounded_in_source':False}
    complete(ctx,domain,page)
    return page
