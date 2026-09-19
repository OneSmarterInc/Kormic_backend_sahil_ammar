"""Sanitize forwarded HTTPS before SecurityMiddleware or request.is_secure()."""
from ipaddress import ip_address, ip_network
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        try:
            self.networks = tuple(ip_network(cidr) for cidr in settings.TRUSTED_PROXY_CIDRS)
        except ValueError as exc:
            raise ImproperlyConfigured("DJANGO_TRUSTED_PROXY_CIDRS must contain IP addresses or CIDRs") from exc

    def __call__(self, request):
        try:
            peer = ip_address(request.META.get("REMOTE_ADDR", ""))
            trusted = any(peer in network for network in self.networks)
        except ValueError:
            trusted = False
        if not trusted:
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
            request.META.pop("HTTP_X_FORWARDED_FOR", None)
        return self.get_response(request)
