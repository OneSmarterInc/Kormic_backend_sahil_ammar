"""
Institutes are universities or other entities outside the United States that
send students into Kormic and may upload student rosters for the claim flow.
They are distinct from `universities.University`, which represents a US
destination university with an AI officer agent and knowledge base. An
Institute never gets an agent; it has identity/contact data and an admin login
for roster management.
"""
import uuid

from django.db import models


class Institute(models.Model):
    # Public, non-guessable identifier used in API URLs and cross-table string
    # references. The integer auto `id` stays internal. Replaces the old slug PK.
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    name = models.CharField(max_length=500)
    country = models.CharField(max_length=2)

    contact_email = models.CharField(max_length=255, blank=True, default="")
    contact_phone = models.CharField(max_length=50, blank=True, default="")
    address = models.CharField(max_length=500, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"Institute({self.id}, {self.name})"
