# url_discovery/group_mapping.py
# Maps the 10 url_discovery classifier categories (url_discovery/rules/
# categories.yaml) onto the 4 Fall-pilot KnowledgeGroup slugs (A1, see
# universities.models.KnowledgeGroup) so an approved cluster's facts land in
# the same group an escalated question about that topic would route to.

from __future__ import annotations

from typing import Optional

from universities.models import KnowledgeGroup

CATEGORY_TO_GROUP_SLUG = {
    "admissions": KnowledgeGroup.Slug.ADMISSIONS,
    "programs": KnowledgeGroup.Slug.ADMISSIONS,
    "careers": KnowledgeGroup.Slug.ADMISSIONS,
    "deadlines": KnowledgeGroup.Slug.ADMISSIONS,
    "documents": KnowledgeGroup.Slug.ADMISSIONS,
    "contact": KnowledgeGroup.Slug.ADMISSIONS,
    "international": KnowledgeGroup.Slug.INTERNATIONAL,
    "fees": KnowledgeGroup.Slug.MONEY,
    "campus_life": KnowledgeGroup.Slug.CAMPUS_LIFE,
    "support": KnowledgeGroup.Slug.CAMPUS_LIFE,
}

# Discovery rows may contain the classifier's display label rather than its
# rules key. Preserve those stored labels while resolving their review group.
CATEGORY_LABEL_TO_KEY = {
    "admissions & application": "admissions",
    "international students & visa": "international",
    "programs & academics": "programs",
    "fees, tuition & financial aid": "fees",
    "housing, dining & campus life": "campus_life",
    "student support services": "support",
    "careers & internships": "careers",
    "deadlines & academic calendar": "deadlines",
    "forms, documents & downloads": "documents",
    "contact & help": "contact",
}


def group_slug_for_category(category: Optional[str]) -> str:
    """Best-guess KnowledgeGroup slug for a discovery category, defaulting
    to ADMISSIONS for an unrecognized or missing category -- same fallback
    universities.knowledge_groups.classify_group_slug uses."""
    key = str(category or "").strip().lower()
    key = CATEGORY_LABEL_TO_KEY.get(key, key)
    return CATEGORY_TO_GROUP_SLUG.get(key, KnowledgeGroup.Slug.ADMISSIONS)
