"""Bounded public web access. Retrieved text is evidence, never instructions."""
import hashlib
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from django.core.cache import cache
from url_discovery.domain_policy import DomainPolicy, validate_public_base_url
from url_discovery.safe_fetch import request_with_policy

USER_AGENT = 'KormicUniversityResearch/1.0'


def require_institution_site(url):
    """Exclude intermediaries before fetching; identity is then verified from the homepage."""
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    intermediaries = ('wikipedia.org', 'wikidata.org', 'facebook.com', 'instagram.com', 'linkedin.com',
        'youtube.com', 'reddit.com', 'topuniversities.com', 'timeshighereducation.com', 'usnews.com',
        'shiksha.com', 'collegedunia.com', 'collegeboard.org', 'studyportals.com', 'mastersportal.com',
        'bachelorsportal.com', 'yocket.com', 'google.com', 'bing.com', 'duckduckgo.com')
    if any(host == domain or host.endswith('.' + domain) for domain in intermediaries):
        raise ValueError('Use the official university website. Third-party sources are not supported for university research.')


def canonical_url(url):
    parts = urlsplit(url.strip())
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError('Only public web pages on standard HTTP ports are allowed')
    validate_public_base_url(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def search_web(query, limit=10):
    from ddgs import DDGS
    if not query.strip() or len(query) > 500:
        raise ValueError('Use a concise university or course search')
    key = 'research-search:' + hashlib.sha256(query.casefold().encode()).hexdigest()
    saved = cache.get(key)
    if saved is not None:
        return saved
    # Explicit search-engine backends: auto also queries encyclopedia services.
    rows = DDGS(timeout=12).text(query, max_results=min(limit, 10), backend='duckduckgo,bing,yahoo')
    results, seen = [], set()
    for row in rows:
        url = row.get('href') or row.get('url') or ''
        if urlsplit(url).scheme not in ('https', 'http') or url in seen:
            continue
        try:
            require_institution_site(url)
        except ValueError:
            continue
        seen.add(url)
        results.append({'url': url[:1000], 'title': str(row.get('title', ''))[:400], 'snippet': str(row.get('body', ''))[:1200]})
    cache.set(key, results, 900)
    return results


def search_official_site(website, query):
    """Institution facts are searched ONLY inside the selected official domain."""
    from url_discovery.domain_policy import root_domain
    host = urlsplit(canonical_url(website)).hostname
    root = root_domain(host)
    results = search_web(f'site:{host} {query[:300]}')
    return [r for r in results if root_domain(urlsplit(r['url']).hostname or '') == root]


def read_page(url, base_url=None):
    require_institution_site(url)
    url = canonical_url(url)
    policy = DomainPolicy(base_url or url)
    if not policy.is_allowed(url):
        raise ValueError('Page must be on the selected official university domain')
    headers = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml'}
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), headers=headers, trust_env=False) as client:
        parts = urlsplit(url)
        robots_url = urlunsplit((parts.scheme, parts.netloc, '/robots.txt', '', ''))
        robots_key = 'research-robots:' + hashlib.sha256(robots_url.encode()).hexdigest()
        robots = cache.get(robots_key)
        if robots is None:
            status, _, body, _ = request_with_policy(client, robots_url, policy, max_bytes=150000)
            if status in (401, 403, 429) or status >= 500:
                raise ValueError('Website currently disallows automated access')
            robots = body.decode('utf-8', errors='replace') if status == 200 else ''
            cache.set(robots_key, robots, 3600)
        parser = RobotFileParser(robots_url)
        parser.parse(robots.splitlines())
        if not parser.can_fetch(USER_AGENT, url):
            raise ValueError('Website robots policy disallows this page')
        status, headers, body, final = request_with_policy(client, url, policy, max_bytes=2_000_000, max_redirects=5)
        if status != 200 or 'html' not in headers.get('content-type', '').lower():
            raise ValueError('This page is unavailable or is not HTML')
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.title.get_text(' ', strip=True)[:500] if soup.title else ''
    links, seen = [], set()
    for anchor in soup.find_all('a', href=True):
        target = urljoin(final, anchor['href']).split('#')[0]
        parts = urlsplit(target)
        # DNS is checked at fetch time, not once for every link on a page.
        if parts.scheme not in ('http', 'https') or not parts.hostname or target in seen:
            continue
        if (parts.hostname == urlsplit(final).hostname or parts.hostname.endswith('.' + policy.root)) and not re.search(r'\.(jpg|png|zip|mp4|pdf)$', parts.path, re.I):
            seen.add(target)
            links.append({'url': target[:1000], 'label': anchor.get_text(' ', strip=True)[:140]})
    for el in soup(['script', 'style', 'noscript', 'svg', 'footer', 'form']):
        el.decompose()
    content = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()[:20000]
    return {'url': final, 'title': title, 'content': content, 'links': links[:100], 'truncated': len(content) >= 20000}
