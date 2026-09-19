"""Deny-by-default university capabilities, enforced for every university API."""
from rest_framework.permissions import SAFE_METHODS

GROUPS = {"admissions": "admissions", "international": "international", "financial_aid": "money", "campus_life": "campus_life"}

def staff_capabilities(account):
    role = account.university_role
    return {"role": role, "group": GROUPS.get(role), "can_manage_staff": role == "owner",
            "can_manage_university": role == "owner", "read_only": role == "viewer"}

def authorize_university_request(request, view, account):
    role = account.university_role
    if role == "owner":
        return True
    name = type(view).__name__
    read = request.method in SAFE_METHODS
    # Staff-management views additionally require the owner permission class.
    if name.startswith("UniversityStaff"):
        return False
    if role == "viewer":
        return read
    group = GROUPS.get(role)
    if not group:
        return False
    if name in ("UniversityProfileAPIView", "UniversityProfileCompletionAPIView", "KnowledgeGroupListAPIView"):
        return read
    if name in ("KnowledgeGroupDetailAPIView", "KnowledgeGroupFactsAPIView", "KnowledgeGroupEscalationsAPIView", "KnowledgeGroupEscalationNotifyAPIView"):
        # Only owners change escalation destinations (PII/email routing).
        if name == "KnowledgeGroupDetailAPIView":
            return False
        return view.kwargs.get("slug") == group
    if name == "KnowledgeFactListCreateAPIView":
        return read or request.data.get("group") == group
    if name == "KnowledgeFactDetailAPIView":
        from django_api.models import UniversityKnowledgeEntry
        entry = UniversityKnowledgeEntry.objects.filter(pk=view.kwargs.get("fact_id"), university_id=account.university_uuid, group__slug=group).first()
        return bool(entry) and ("group" not in request.data or request.data["group"] == group)
    if name in ("AnswerPendingQueryView", "EditPendingQueryView", "IgnorePendingQueryView"):
        from django_api.models import PendingQuery
        query_id = view.kwargs.get("query_id") or request.data.get("query_id")
        try:
            return PendingQuery.objects.filter(pk=int(query_id), university_id=account.university_uuid, group__slug=group).exists()
        except (TypeError, ValueError):
            return False
    return False
