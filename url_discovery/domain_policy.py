from __future__ import annotations

import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

try:
    import tldextract
except ImportError:  # The package is installed by requirements.txt in normal setup.
    tldextract = None


_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=()) if tldextract else None


def root_domain(hostname: str) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    if _EXTRACTOR is not None:
        extracted = _EXTRACTOR(host)
        return extracted.top_domain_under_public_suffix or host

    # Lightweight fallback for development environments where tldextract is not installed.
    labels = host.split(".")
    multi_part_suffixes = {
        "ac.in", "edu.in", "ac.uk", "edu.au", "ac.nz", "edu.sg", "ac.za",
        "edu.cn", "ac.jp", "edu.my", "edu.pk", "edu.bd", "ac.th",
    }
    if len(labels) >= 3 and ".".join(labels[-2:]) in multi_part_suffixes:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def is_private_or_local_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        return False


@lru_cache(maxsize=512)
def resolves_to_public_ip(hostname: str) -> bool:
    if is_private_or_local_host(hostname):
        return False
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return True  # DNS may be unavailable during project creation; crawler will report failures.
    if not addresses:
        return False
    for address in addresses:
        ip_text = address[4][0]
        try:
            if not ipaddress.ip_address(ip_text).is_global:
                return False
        except ValueError:
            return False
    return True


def validate_public_base_url(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are supported")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("The base URL must contain a hostname")
    if not resolves_to_public_ip(host):
        raise ValueError("Local, private, or non-public hostnames are not allowed")
    return host, root_domain(host)


class DomainPolicy:
    def __init__(
        self,
        base_url: str,
        allowed_domains: list[str] | None = None,
        include_subdomains: bool = True,
    ) -> None:
        base_host = (urlsplit(base_url).hostname or "").lower()
        self.base_host = base_host
        self.root = root_domain(base_host)
        self.include_subdomains = include_subdomains
        self.extra_domains = {
            (urlsplit(item).hostname or item).lower().strip().rstrip(".")
            for item in (allowed_domains or [])
            if item
        }

    def is_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            return False
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or is_private_or_local_host(host) or not resolves_to_public_ip(host):
            return False
        if host in self.extra_domains:
            return True
        if self.include_subdomains:
            return root_domain(host) == self.root
        return host == self.base_host
