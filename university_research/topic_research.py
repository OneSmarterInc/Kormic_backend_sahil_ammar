"""Fill requested topic gaps without refreshing an entire university catalogue."""
import re
from django.utils import timezone
from datetime import timedelta
from .models import InformationPolicy


def scholarship_gap(row, question):
    if not re.search(r'\bscholarships?\b',question,re.I):
        return False
    checked=row.coverage.get('topic_checks',{}).get('scholarships')
    if checked:
        from django.utils.dateparse import parse_datetime
        date=parse_datetime(checked)
        days=InformationPolicy.objects.filter(pk=1).values_list('refresh_days',flat=True).first() or 30
        if date and date >= timezone.now()-timedelta(days=days):
            return False
    awards=row.coverage.get('catalogue_profile',{}).get('scholarships',[])
    return not awards or any(any(not item.get(key) or item[key]=='N/A' for key in
        ('name','amount','eligibility','deadline','application_url')) for item in awards)


def research_scholarships(ctx,row):
    from .web import search_official_site,read_page_once
    from .claude_fallback import search_official_evidence
    from .catalogue import save_catalogue
    from .research_budget import cached
    from url_discovery.domain_policy import root_domain
    from urllib.parse import urlsplit
    from pure_multi_agent.telemetry import emit
    topics='Scholarships: named awards, amounts and currency/year, eligibility, deadlines and official application links. Separate scholarships, need-based aid, assistantships and employer benefits. Explain unknown details and do not claim completeness.'
    domain=root_domain(urlsplit(row.website).hostname or '')
    page=cached(ctx,domain)
    if page is None:
        try:
            results=search_official_site(row.website,'scholarships awards eligibility amounts deadlines application')
            source=read_page_once(results[0]['url'] if results else row.website)
            page=search_official_evidence(row.website,topics,ctx=ctx,source_page=source)
        except Exception as exc:
            from github_profiles.scheduling import CapacityBusy
            from pure_multi_agent.capacity import AgentBusy
            if isinstance(exc,(CapacityBusy,AgentBusy)):
                raise
            emit('AGENT_PROGRESS','scholarship_research',outputs={'summary':'Official scholarship details unavailable; using the information fallback.'})
            page=None
        awards=(page or {}).get('catalogue',{}).get('scholarships',[])
        if not awards or any(any(not a.get(k) or a[k]=='N/A' for k in ('amount','eligibility','deadline','application_url')) for a in awards):
            page=search_official_evidence(row.website,topics,ctx=ctx)
    save_catalogue(row,page)
    row.refresh_from_db()
    row.coverage={**row.coverage,'topic_checks':{**row.coverage.get('topic_checks',{}),'scholarships':timezone.now().isoformat()}}
    row.save(update_fields=['coverage'])
