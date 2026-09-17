"""
Institutes are the local orgs (schools, coaching centers, agents' partner
institutions) that upload student lists for the claim flow -- distinct from
`universities.University`, which is the destination school with an AI officer
agent, personas, and fit scoring. An institute never gets an agent; it only
ever needs an identity for provenance and one admin login to upload lists.
"""
import uuid

from django.db import models


class Institute(models.Model):
    # Public, non-guessable identifier used in API URLs and cross-table string
    # references. The integer auto `id` stays internal. Replaces the old slug PK.
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    name = models.CharField(max_length=500)

    contact_email = models.CharField(max_length=255, blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")
    address = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"Institute({self.id}, {self.name})"
