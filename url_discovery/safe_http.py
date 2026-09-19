"""Public-only fetches: fresh DNS, pinned socket, verified TLS, bounded bodies."""
import gzip
import http.client
import io
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit
import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


class FetchRejected(ValueError):
    pass


def decompress_gzip(data, limit=MAX_RESPONSE_BYTES):
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        body = stream.read(limit + 1)
    if len(body) > limit:
        raise FetchRejected('RESPONSE_TOO_LARGE: decompressed response exceeds the byte limit.')
    return body


class PublicClient:
    """No ambient proxies, cookies, redirect automation, or insecure retry path."""
    def __init__(self, *, policy, headers=None, timeout=20, max_bytes=MAX_RESPONSE_BYTES):
        self.policy, self.headers = policy, dict(headers or {})
        self.timeout, self.max_bytes = timeout, max_bytes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        deadline = time.monotonic() + self.timeout
        seen = set()
        for hop in range(MAX_REDIRECTS + 1):
            if url in seen:
                raise FetchRejected('REDIRECT_LOOP: source redirects in a loop.')
            seen.add(url)
            if not self.policy.is_allowed(url):
                raise FetchRejected('UNSAFE_SOURCE_URL: source or redirect is not an allowed public URL.')
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            try:
                addresses = sorted({item[4][0] for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)})
            except OSError:
                raise FetchRejected('DNS_UNAVAILABLE: source hostname could not be resolved.')
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise FetchRejected('UNSAFE_SOURCE_ADDRESS: source resolves to a non-public address.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchRejected('FETCH_TIMEOUT: source exceeded the request budget.')
            conn = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
            sock = None
            try:
                # The connection uses an IP literal from this DNS result, never a
                # second hostname lookup. Host header and TLS SNI retain the name.
                sock = socket.create_connection((addresses[0], port), timeout=remaining)
                if parsed.scheme == 'https':
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
                conn.sock = sock
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                conn.request('GET', path, headers={**self.headers, 'Accept-Encoding': 'identity'})
                response = conn.getresponse()
                headers = {k.lower(): v for k, v in response.getheaders()}
                if response.status in {301, 302, 303, 307, 308}:
                    if hop == MAX_REDIRECTS or not headers.get('location'):
                        raise FetchRejected('REDIRECT_LIMIT: source redirect could not be followed.')
                    target = urljoin(url, headers['location'])
                    if parsed.scheme == 'https' and urlsplit(target).scheme != 'https':
                        raise FetchRejected('TLS_DOWNGRADE: HTTPS source redirected to HTTP.')
                    url = target
                    continue
                if 'content-length' in headers:
                    try:
                        length = int(headers['content-length'])
                    except ValueError:
                        raise FetchRejected('INVALID_CONTENT_LENGTH')
                    if length < 0 or length > self.max_bytes:
                        raise FetchRejected('RESPONSE_TOO_LARGE: source exceeds the byte limit.')
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FetchRejected('FETCH_TIMEOUT: source exceeded the request budget.')
                    sock.settimeout(remaining)
                    chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise FetchRejected('RESPONSE_TOO_LARGE: source exceeds the byte limit.')
                encoding = headers.pop('content-encoding', '').lower()
                if encoding == 'gzip':
                    body = decompress_gzip(body, self.max_bytes)
                elif encoding not in {'', 'identity'}:
                    raise FetchRejected('UNSUPPORTED_CONTENT_ENCODING')
                return httpx.Response(response.status, headers=headers, content=bytes(body), request=httpx.Request('GET', url))
            except ssl.SSLError:
                raise FetchRejected('TLS_VERIFICATION_FAILED: secure connection could not be verified. Ask the website administrator to fix its certificate/TLS configuration.')
            finally:
                conn.close()
                if sock is not None:
                    sock.close()
        raise FetchRejected('REDIRECT_LIMIT')
