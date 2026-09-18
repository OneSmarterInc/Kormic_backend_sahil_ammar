"""Bounded, robots-aware recrawls. DNS is validated and the connection pinned to it."""
import hashlib
import http.client
import ipaddress
import socket
import ssl
from datetime import timedelta
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django_api.models import KnowledgeSource, UniversityKnowledgeEntry
from knowledge.retrieval import SCRAPED, sync_chunks

USER_AGENT = "KormicKnowledgeBot/1.0"


def fetch_public(url, redirects=0):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("UNSAFE_SOURCE_URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("UNSAFE_SOURCE_ADDRESS")
    conn = http.client.HTTPConnection(parsed.hostname, port, timeout=10)
    try:
        sock = socket.create_connection((sorted(addresses)[0], port), timeout=10)
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        conn.sock = sock
        conn.request("GET", parsed.path or "/" if not parsed.query else (parsed.path or "/") + "?" + parsed.query,
                     headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
        response = conn.getresponse()
        status, headers = response.status, dict(response.getheaders())
        if status in {301, 302, 303, 307, 308}:
            if redirects >= 3: raise ValueError("TOO_MANY_REDIRECTS")
            destination = urljoin(url, response.getheader("Location", ""))
            # Each approved page belongs to its origin. Cross-origin moves need approval.
            if urlsplit(destination).netloc != parsed.netloc: raise ValueError("SOURCE_MOVED_ORIGIN")
            return fetch_public(destination, redirects + 1)
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024: raise ValueError("SOURCE_TOO_LARGE")
        return status, {k.lower(): v for k, v in headers.items()}, data.decode("utf-8", errors="replace")
    finally:
        conn.close()


def robots_policy(url):
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    key = "kb:robots:" + hashlib.sha256(origin.encode()).hexdigest()
    record = cache.get(key)
    if record is None:
        status, headers, text = fetch_public(origin + "/robots.txt")
        if status == 404: text = "User-agent: *\nAllow: /"
        elif status != 200: raise ValueError("ROBOTS_UNAVAILABLE")
        record = text[:200000]
        cache.set(key, record, timeout=3600)
    parser = RobotFileParser(); parser.parse(record.splitlines())
    delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 2
    rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
    if rate: delay = max(delay, rate.seconds / max(rate.requests, 1))
    return parser.can_fetch(USER_AGENT, url), max(2, int(delay + 0.999)), origin


def retry_delay(headers):
    raw = headers.get("retry-after", "")
    try: return max(60, int(raw))
    except (ValueError, TypeError):
        try: return max(60, int((parsedate_to_datetime(raw) - timezone.now()).total_seconds()))
        except (ValueError, TypeError): return 3600


def recrawl(source_id):
    now = timezone.now()
    with transaction.atomic():
        source = KnowledgeSource.objects.select_for_update().select_related("university").get(pk=source_id)
        if not source.enabled or (source.lease_until and source.lease_until > now): return 0
        source.lease_until = now + timedelta(minutes=5)
        source.save(update_fields=["lease_until"])
    count = 0
    try:
        allowed, delay, origin = robots_policy(source.url)
        if not allowed:
            source.health = "robots_blocked"
            return 0
        key = "kb:origin:" + hashlib.sha256(origin.encode()).hexdigest()
        if not cache.add(key, True, timeout=delay):
            source.health = "rate_limited"
            source.next_fetch_at = now + timedelta(seconds=delay)
            return 0
        status, headers, html = fetch_public(source.url)
        source.last_fetched_at, source.http_status = now, status
        if status in {404, 410}:
            source.health = "deleted"
            UniversityKnowledgeEntry.objects.filter(university_id=str(source.university.uuid), source_url=source.url, source_type__in=SCRAPED).update(active=False)
            return 0
        if status == 429 or status == 503:
            source.health = "rate_limited"
            source.next_fetch_at = now + timedelta(seconds=retry_delay(headers))
            return 0
        if status != 200: raise ValueError("SOURCE_HTTP_ERROR")
        if not any(t in headers.get("content-type", "") for t in ("text/html", "text/plain", "application/xhtml")):
            raise ValueError("SOURCE_NOT_TEXT")
        soup = BeautifulSoup(html, "html.parser")
        for node in soup(["script", "style", "nav", "footer", "header", "noscript"]): node.decompose()
        text = " ".join(soup.get_text(" ", strip=True).split())
        if not text: raise ValueError("SOURCE_EMPTY")
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest != source.content_hash:
            # Old facts are unavailable immediately, even if extraction later fails.
            source.health = "changed_pending"
            source.save(update_fields=["health"])
            from knowledge.scraper import extract_facts_from_page
            facts = extract_facts_from_page(source.url, text[:30000], source.university.name)
            if not facts: raise ValueError("EXTRACTION_EMPTY")
            with transaction.atomic():
                UniversityKnowledgeEntry.objects.filter(university_id=str(source.university.uuid), source_url=source.url, source_type__in=SCRAPED).delete()
                for fact in facts[:100]:
                    topic, content = str(fact.get("topic", "")).strip(), str(fact.get("content", "")).strip()
                    if not topic or not content: continue
                    entry = UniversityKnowledgeEntry.objects.create(university_id=str(source.university.uuid),
                        topic=topic[:500], content=content[:10000], source_url=source.url, group_id=source.group_id,
                        source_type="scraped", confidence=min(0.9, max(0, float(fact.get("confidence", 0.8)))))
                    sync_chunks(entry); count += 1
                if not count: raise ValueError("EXTRACTION_EMPTY")
            source.content_hash, source.changed_at = digest, now
        else:
            UniversityKnowledgeEntry.objects.filter(university_id=str(source.university.uuid), source_url=source.url, source_type__in=SCRAPED).update(active=True)
        source.last_success_at, source.health, source.failures = now, "healthy", 0
    except Exception:
        # No page text, student data, URLs with credentials or exception payloads in logs.
        source.health = "changed_pending" if source.health == "changed_pending" else "fetch_error"
        source.failures += 1
    finally:
        source.lease_until = None
        if source.next_fetch_at <= now:
            source.next_fetch_at = now + timedelta(hours=min(source.recrawl_hours, 6) if source.failures else source.recrawl_hours)
        source.save()
    return count


def track_sources():
    from universities.models import University
    for university in University.objects.all().iterator():
        for url in university.scrape_urls or []:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                KnowledgeSource.objects.get_or_create(university=university, url=url.rstrip("/"))
    # Previously approved discovery URLs also need freshness records.
    for university_id, url in UniversityKnowledgeEntry.objects.filter(source_type__in=SCRAPED).exclude(source_url__isnull=True).exclude(source_url="").values_list("university_id", "source_url").distinct():
        try:
            university = University.objects.filter(uuid=university_id).first()
        except (ValueError, TypeError):
            continue
        if university:
            group_id = UniversityKnowledgeEntry.objects.filter(university_id=university_id, source_url=url, group__isnull=False).values_list("group_id", flat=True).first()
            source, created = KnowledgeSource.objects.get_or_create(university=university, url=url, defaults={"group_id": group_id})
            if not created and source.group_id is None and group_id:
                source.group_id = group_id; source.save(update_fields=["group_id"])
