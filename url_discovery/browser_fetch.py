"""Bounded rendering for public HTML shells; never a bypass for HTTP denials."""
import asyncio
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx

from .domain_policy import resolves_to_public_ip
from .safe_fetch import request_with_policy


class PublicResourcePolicy:
    """CDN resources may cross domains, but every redirect must remain public."""
    def is_allowed(self, url):
        parts = urlsplit(url)
        return parts.scheme in ('https', 'http') and resolves_to_public_ip(parts.hostname or '')


def fetch_resource(url, policy, timeout, max_bytes):
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        return request_with_policy(client, url, policy, max_bytes=max_bytes)


def needs_rendering(body, content_type):
    if 'html' not in content_type:
        return False
    soup = BeautifulSoup(body, 'html.parser')
    scripts = bool(soup.find('script'))
    for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
        tag.decompose()
    root = soup.find('main') or soup.body or soup
    return scripts and len(root.get_text(' ', strip=True)) < 180


async def render_html(url, policy, *, timeout=20, max_bytes=5 * 1024 * 1024):
    from playwright.async_api import async_playwright

    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers='block', accept_downloads=False)
            resource_count = 0
            resource_bytes = 0

            async def guard(route):
                nonlocal resource_count, resource_bytes
                request = route.request
                # Scripts, styles and data may be hosted on a public CDN. Main
                # documents and frames must remain inside the university scope.
                parts = urlsplit(request.url)
                allowed = parts.scheme in ('https', 'http') and request.method in ('GET', 'HEAD')
                if request.is_navigation_request():
                    allowed = allowed and policy.is_in_scope(request.url)
                resource_count += 1
                if not allowed or request.resource_type in ('image', 'media', 'font') or resource_count > 80 or resource_bytes >= 20 * 1024 * 1024:
                    await route.abort()
                else:
                    # Browser auto-redirects must never bypass the URL policy.
                    # Fetch every hop through the same bounded transport as HTML.
                    resource_policy = policy if request.is_navigation_request() else PublicResourcePolicy()
                    try:
                        status, headers, body, _ = await asyncio.to_thread(
                            fetch_resource, request.url, resource_policy, timeout, max_bytes)
                        resource_bytes += len(body)
                        if resource_bytes > 20 * 1024 * 1024:
                            await route.abort()
                            return
                        clean_headers = {key: value for key, value in headers.items()
                                         if key.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')}
                        await route.fulfill(status=status, headers=clean_headers, body=body)
                    except Exception:
                        await route.abort()

            await context.route('**/*', guard)
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            response = await page.goto(url, wait_until='domcontentloaded', timeout=timeout * 1000)
            if not response or response.status >= 400:
                raise ValueError('Browser navigation did not return a successful page')
            try:
                await page.wait_for_load_state('networkidle', timeout=min(timeout, 5) * 1000)
            except Exception:
                pass  # Some public sites keep analytics requests open.
            if not await asyncio.to_thread(policy.is_allowed, page.url):
                raise ValueError('Blocked rendered destination')
            html = (await page.content()).encode('utf-8')
            if len(html) > max_bytes:
                raise ValueError('Rendered document exceeds byte limit')
            return response.status, {'content-type': 'text/html'}, html, page.url
        finally:
            await browser.close()
