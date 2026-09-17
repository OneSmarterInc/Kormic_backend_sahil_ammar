"""Backward-compatible claim URLs for clients configured without /api.

The canonical endpoints remain /api/claim/*. This compatibility include lets
an older or locally misconfigured student build reach the same views at
/claim/* instead of receiving an HTML 404 page. It can be removed after all
released clients are confirmed to use an API base ending in /api.
"""
from django.urls import path

from . import claim_views, views

urlpatterns = [
    path("start/", claim_views.start_claim, name="claim-start-compat"),
    path("verify/", views.verify_claim, name="claim-verify-compat"),
    path("confirm/", views.confirm_claim, name="claim-confirm-compat"),
]
