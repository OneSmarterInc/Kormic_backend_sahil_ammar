# universities/knowledge_groups.py
# Routing logic for the four Fall-pilot knowledge groups (A1). Kept separate
# from universities/services.py since this is specifically about *which
# group a question belongs to*, not general university profile/KB plumbing.

from __future__ import annotations

import re
from typing import Dict, List, Optional

from universities.models import KnowledgeGroup, University

# Keyword sets used to classify an escalated question into one of the four
# launch groups. Checked in priority order (INTERNATIONAL/MONEY/CAMPUS_LIFE
# before the ADMISSIONS catch-all) since a question like "does my
# assistantship cover the SEVIS fee" should route to Money before its
# generic admissions-shaped wording would match anything else.
GROUP_KEYWORDS: Dict[str, List[str]] = {
    KnowledgeGroup.Slug.INTERNATIONAL: [
        "visa", "i-20", "i20", "sevis", "cpt", "opt", "toefl", "ielts", "duolingo",
        "international student", "passport", "f-1", "f1 visa", "j-1", "dso",
    ],
    KnowledgeGroup.Slug.MONEY: [
        "tuition", "fee", "fees", "cost", "scholarship", "assistantship", "stipend",
        "funding", "financial aid", "bursar", "payment", "loan", "waiver", "billing",
    ],
    KnowledgeGroup.Slug.CAMPUS_LIFE: [
        "housing", "dorm", "dormitory", "dining", "meal plan", "health insurance",
        "orientation", "campus life", "roommate", "parking", "gym", "move-in",
    ],
    KnowledgeGroup.Slug.ADMISSIONS: [
        "admission", "application", "deadline", "gpa", "gre", "gmat", "requirement",
        "eligibility", "transcript", "recommendation letter", "sop", "essay", "apply",
    ],
}

# Classification priority: first keyword set that matches wins. ADMISSIONS
# is deliberately last -- it's also the fallback when nothing matches at all.
_CLASSIFICATION_ORDER = [
    KnowledgeGroup.Slug.INTERNATIONAL,
    KnowledgeGroup.Slug.MONEY,
    KnowledgeGroup.Slug.CAMPUS_LIFE,
    KnowledgeGroup.Slug.ADMISSIONS,
]

_COMPILED_KEYWORDS: Dict[str, List["re.Pattern[str]"]] = {
    slug: [re.compile(r"\b" + re.escape(keyword) + r"\b") for keyword in keywords]
    for slug, keywords in GROUP_KEYWORDS.items()
}


def classify_group_slug(question: str) -> str:
    """Best-guess launch-group slug for a question's text, defaulting to
    ADMISSIONS when no keyword matches."""
    text = str(question or "").lower()

    for slug in _CLASSIFICATION_ORDER:
        if any(pattern.search(text) for pattern in _COMPILED_KEYWORDS[slug]):
            return slug

    return KnowledgeGroup.Slug.ADMISSIONS


def resolve_group_for_question(university_id: str, question: str) -> Optional[KnowledgeGroup]:
    """The KnowledgeGroup an escalation for this question should route to,
    or None if the university hasn't configured any groups yet -- routing
    stays best-effort rather than blocking escalation creation on setup."""
    slug = classify_group_slug(question)

    group = KnowledgeGroup.objects.filter(university__uuid=university_id, slug=slug).first()
    if group is not None:
        return group

    return KnowledgeGroup.objects.filter(university__uuid=university_id).order_by("slug").first()


def ensure_default_groups(university: University) -> List[KnowledgeGroup]:
    """Create any of the four launch groups this university doesn't have
    yet, all defaulting to the university's general contact_email until an
    officer sets a per-group contact. Idempotent -- safe to call on every
    GET of the group list."""
    existing_slugs = set(university.knowledge_groups.values_list("slug", flat=True))
    fallback_email = university.contact_email or ""

    created = []
    for slug, label in KnowledgeGroup.Slug.choices:
        if slug in existing_slugs:
            continue
        created.append(
            KnowledgeGroup.objects.create(
                university=university,
                slug=slug,
                escalation_contact_name=f"{label} Office",
                escalation_contact_email=fallback_email,
            )
        )

    return created


def escalation_counts_by_group(university_id: str) -> Dict[str, int]:
    """Per-group escalation (PendingQuery) counts for one university,
    including zero counts for configured groups with none yet."""
    from django.db.models import Count

    from django_api.models import PendingQuery

    counts = {
        slug: 0
        for slug in KnowledgeGroup.objects.filter(university__uuid=university_id).values_list("slug", flat=True)
    }

    rows = (
        PendingQuery.objects.filter(university_id=university_id, group__isnull=False)
        .values("group__slug")
        .annotate(count=Count("id"))
    )
    for row in rows:
        counts[row["group__slug"]] = row["count"]

    return counts
