"""Bounded directory queries. Do not claim JSON/text matches verify eligibility."""
import re
from django.conf import settings
from django.db.models import Q, Case, When, Value, IntegerField
from universities.models import University


def search_universities(query="", country="", location="", limit=10):
    limit = max(1, min(int(limit), 20))
    rows = University.objects.all()
    if country:
        rows = rows.filter(country__iexact=country[:2])
    if location:
        rows = rows.filter(location__icontains=location[:100])
    tokens = re.findall(r"[\w-]+", query)[:8]
    if tokens:
        matched = Q()
        for token in tokens:
            matched |= Q(name__icontains=token) | Q(description__icontains=token) | Q(tagline__icontains=token)
        rows = rows.filter(matched)
    if query.strip():
        rows = rows.annotate(exact=Case(When(name__iexact=query.strip(), then=Value(0)), default=Value(1), output_field=IntegerField())).order_by("exact", "name", "pk")
    else:
        rows = rows.order_by("name", "pk")
    return [{"id": str(row.uuid), "name": row.name, "country": row.country,
             "location": row.location, "summary": row.description[:400]}
            for row in rows[:limit]]


def select_targets(ctx, university_ids=None):
    selected = list(dict.fromkeys(university_ids or ctx.get("university_candidates", [])))
    if not selected:
        profile = ctx.get("student_profile", {})
        selected = [u["id"] for u in search_universities(profile.get("program", ""), limit=settings.AGENT_MAX_UNIVERSITIES)]
    valid = set(str(pk) for pk in University.objects.filter(uuid__in=selected[:settings.AGENT_MAX_UNIVERSITIES]).values_list("uuid", flat=True))
    return [pk for pk in selected[:settings.AGENT_MAX_UNIVERSITIES] if pk in valid]


def allow_contact(ctx, university_id):
    contacts = ctx.setdefault("contacted_universities", [])
    if university_id not in contacts:
        if len(contacts) >= settings.AGENT_MAX_UNIVERSITIES:
            return False
        contacts.append(university_id)
    return True
