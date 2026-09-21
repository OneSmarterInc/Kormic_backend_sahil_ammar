from __future__ import annotations

from urllib.parse import urljoin

import httpx

from url_discovery.domain_policy import DomainPolicy

DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 10
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def request_with_policy(
    client: httpx.Client,
    url: str,
    domain_policy: DomainPolicy,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> tuple[int, httpx.Headers, bytes, str]:
    """Fetch one public URL with policy validation on every redirect hop.

    Redirects are never followed implicitly. Every target is checked before
    opening a connection, and response bodies are streamed with a hard byte
    cap so discovery and knowledge ingestion share the same SSRF/resource
    boundary.
    """
    current = url

    for _hop in range(max_redirects + 1):
        if not domain_policy.is_allowed(current):
            raise ValueError(f"Blocked unsafe or out-of-policy URL: {current}")

        with client.stream("GET", current, follow_redirects=False) as response:
            status = response.status_code
            headers = response.headers
            response_url = str(response.url)

            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise ValueError(
                        f"Redirect response from {current} omitted Location"
                    )
                next_url = urljoin(response_url, location)
                if not domain_policy.is_allowed(next_url):
                    raise ValueError(f"Blocked unsafe redirect target: {next_url}")
                current = next_url
                continue

            if status >= 400:
                return status, headers, b"", response_url

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Response exceeds {max_bytes} byte limit")
                chunks.append(chunk)

            return status, headers, b"".join(chunks), response_url

    raise ValueError(f"Too many redirects while fetching {url}")
