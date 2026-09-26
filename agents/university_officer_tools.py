"""Read-only dashboard tools, exposed only to the authenticated officer chat.

The university scope is bound by the server, never supplied by the model.
Operational/student records must never be persisted in the public knowledge base.
"""
from __future__ import annotations

from collections import Counter

from django.core.exceptions import ValidationError


def _tool(name, description, properties=None, required=None):
    return {"name": name, "description": description, "input_schema": {
        "type": "object", "properties": properties or {}, "required": required or [],
        "additionalProperties": False,
    }}


PAGE = {"type": "integer", "minimum": 1, "description": "Page number; 25 records per page."}
TOOLS = [
    _tool("university_dashboard", "Read current university settings, eligibility criteria, student counts, knowledge groups, saved sources and latest scraping status."),
    _tool("interested_students", "Find students who expressed interest in THIS university. Use this for student names, eligible/qualified students, fit scores and counts. Qualification comes from configured eligibility rules, NOT fit score and NOT an admission decision. Empty results are a valid database answer.", {
        "qualification": {"type": "string", "enum": ["all", "qualified", "not_qualified", "unassessed"]},
        "name": {"type": "string", "description": "Optional student name search."}, "page": PAGE,
    }),
    _tool("interested_student_detail", "Read academic profile, goals, preferences, eligibility evidence and this university's assessment for one interested student. Get the student_id from interested_students.", {
        "student_id": {"type": "string"},
    }, ["student_id"]),
    _tool("university_queries", "Read student questions awaiting officer answers, resolved queries or ignored queries for this university, including routing contacts.", {
        "status": {"type": "string", "enum": ["all", "pending", "resolved", "ignored"]}, "page": PAGE,
    }),
    _tool("university_exchanges", "Read recent student-agent questions and university-agent answers for this university only.", {"page": PAGE}),
    _tool("search_university_knowledge", "Search this university's scraped, manual and verified knowledge for official university facts.", {
        "question": {"type": "string"},
    }, ["question"]),
]


class OfficerData:
    def __init__(self, university, kb):
        self.university = university
        self.university_id = str(university.uuid)
        self.kb = kb

    def _students(self):
        from django_api.services import get_shortlisted_profiles
        from django_api.models import StudentProfile

        entries = get_shortlisted_profiles(self.university_id, min_score=0)
        rows = {str(row.uuid): row for row in StudentProfile.objects.filter(uuid__in=[e["student_id"] for e in entries])}
        return [{**entry, "name": rows[entry["student_id"]].name,
                 "program": rows[entry["student_id"]].program,
                 "institution": rows[entry["student_id"]].institution}
                for entry in entries if entry["student_id"] in rows]

    @staticmethod
    def _page(rows, page):
        if type(page) is not int or page < 1:
            raise ValueError("page must be a positive integer")
        start = (page - 1) * 25
        return {"total": len(rows), "page": page, "page_size": 25,
                "has_next": start + 25 < len(rows), "records": rows[start:start + 25]}

    def execute(self, name, arguments):
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None or not isinstance(arguments, dict):
            return {"error": "Unknown tool or invalid arguments."}
        schema = spec["input_schema"]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            return {"error": "Invalid tool arguments. University scope cannot be changed."}
        try:
            return getattr(self, name)(**arguments)
        except (ValueError, TypeError, ValidationError):
            return {"error": "Invalid filter, page or student identifier."}

    def university_dashboard(self):
        from django_api.models import PendingQuery, UniversityKnowledgeEntry

        students = self._students()
        scrape = self.university.scrape_jobs.order_by("-created_at").values("status", "error_message", "completed_at").first()
        discovery = self.university.discovery_jobs.order_by("-created_at").values("status", "error_message", "pages_crawled").first()
        return {
            "university_id": self.university_id,
            "name": self.university.name,
            "eligibility_criteria": self.university.eligibility_criteria,
            "fit_score_threshold": self.university.min_fit_score_threshold,
            "interested_students": len(students),
            "qualification_counts": dict(Counter(row.get("qualification_status", "unassessed") for row in students)),
            "qualification_note": "Meeting recorded criteria is not an admission decision; missing rules/data require review. Fit scores are a separate signal.",
            "pending_queries": PendingQuery.objects.filter(university_id=self.university_id, status="pending").count(),
            "knowledge_entries": UniversityKnowledgeEntry.objects.filter(university_id=self.university_id).count(),
            "knowledge_groups": list(self.university.knowledge_groups.values("slug", "escalation_contact_name", "escalation_contact_email")),
            "scrape_urls": self.university.scrape_urls, "latest_scrape": scrape, "latest_discovery": discovery,
        }

    def interested_students(self, qualification="all", name="", page=1):
        if qualification not in {"all", "qualified", "not_qualified", "unassessed"} or not isinstance(name, str):
            raise ValueError("Invalid student filter")
        rows = self._students()
        counts = dict(Counter(row.get("qualification_status", "unassessed") for row in rows))
        rows = [row for row in rows if (qualification == "all" or row.get("qualification_status", "unassessed") == qualification)
                and name.casefold() in row["name"].casefold()]
        return {**self._page(rows, page), "qualification_counts": counts,
                "qualification_basis": self.university.eligibility_criteria,
                "note": "These are interested students; eligibility is based on recorded criteria, not an admission decision."}

    def interested_student_detail(self, student_id):
        from agents.student_context import university_context
        from django_api.models import StudentProfile
        from django_api.services import profile_row_to_dict

        row = StudentProfile.objects.filter(uuid=student_id,
            university_interest_events__university_id=self.university_id).first()
        if row is None:
            return {"error": "No such student in this university's dashboard."}
        assessment = next((entry for entry in self._students() if entry["student_id"] == str(row.uuid)), {})
        return {"profile": university_context(str(row.uuid), profile_row_to_dict(row)), "university_assessment": assessment}

    def university_queries(self, status="all", page=1):
        from django_api.models import PendingQuery

        if status not in {"all", "pending", "resolved", "ignored"}:
            raise ValueError("Invalid status")
        rows = PendingQuery.objects.filter(university_id=self.university_id)
        if status != "all":
            rows = rows.filter(status=status)
        return self._page(list(rows.order_by("-created_at", "-id").values(
            "id", "student_name", "question", "status", "priority", "answer", "routed_to_name", "routed_to_email")), page)

    def university_exchanges(self, page=1):
        from django_api.models import AgentConversationLog

        rows = AgentConversationLog.objects.filter(responder__owner_type="university", responder__owner_id=self.university_id)
        return self._page(list(rows.order_by("-created_at", "-id").values("question", "answer", "knowledge_source", "created_at")), page)

    def search_university_knowledge(self, question):
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question is required")
        return {"records": [entry.to_dict() for entry in self.kb.search(question[:4000], limit=12)]}
