# agents/profile_presenter.py

import json
import os
from typing import Dict, List, Optional, Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    raise ValueError("ANTHROPIC_API_KEY not found. Check your .env file.")

# Bounds every call so a hung upstream request can't hold the request
# worker indefinitely.
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120.0
client = anthropic.Anthropic(api_key=api_key, timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)

MODEL = os.getenv("PROFILE_PRESENTER_MODEL", "claude-haiku-4-5-20251001")


def _build_presenter_constitution(university_name: str, agent_name: str) -> str:
    return f"""
You are {agent_name}, {university_name}'s university-facing admissions profile presenter.

You ARE {university_name}. The person asking you questions is already logged in
as a {university_name} admissions officer -- that is the only way to reach this
chat. Never ask who they represent, never ask them to confirm or name their
university/program, and never treat "our university" or "my university" as
ambiguous -- it always means {university_name}.

You serve the university officer, not the student.
Your job is to explain whether a student is worth attention, and whether they
fit {university_name}'s own admission standards using the assessment data
provided below (which was generated specifically against {university_name}'s
knowledge base/criteria).

Rules:
- Be honest about weaknesses.
- Use only profile data and the fit assessment provided -- do not invent data.
- Keep answers short.
- Mention missing fields clearly.
- Help the officer decide whether to invite, reject, or ask follow-up questions.
"""


class ProfilePresenterAgent:
    def __init__(self, university_id: str):
        self.university_id = university_id

        from universities.models import University

        university = University.objects.filter(uuid=university_id).first()
        self.university_name = university.name if university else university_id
        self.agent_name = (university.agent_name if university else None) or university_id
        self.constitution = _build_presenter_constitution(self.university_name, self.agent_name)

    def _audit_failure(
        self,
        event: str,
        message: str,
        details: Optional[str] = None,
        profile_name: Optional[str] = None,
        question: Optional[str] = None,
    ):
        from django_api.models import PresenterAuditLog

        try:
            PresenterAuditLog.objects.create(
                university_id=self.university_id,
                event=event,
                message=message,
                details=details or "",
                profile_name=profile_name or "",
                question=question or "",
            )
        except Exception:
            print(f"[AUDIT FAILURE] {event}: {message} | {details}")

    def _detect_topic(self, question: str) -> str:
        q = question.lower()

        topics = {
            "gpa": ["gpa", "cgpa", "grade", "academic"],
            "research": ["research", "paper", "publication"],
            "funding": ["fund", "scholarship", "budget", "cost", "money"],
            "skills": ["skill", "github", "project", "code", "technical"],
            "interview": ["interview", "communication"],
            "work": ["work", "internship", "experience", "job"],
            "fit": ["fit", "match", "recommend", "suitable", "why"],
        }

        for topic, words in topics.items():
            if any(w in q for w in words):
                return topic

        return "general"

    def _log_question(self, question: str, profile: Dict[str, Any]):
        from django_api.models import UniversityQuestionLog

        UniversityQuestionLog.objects.create(
            university_id=self.university_id,
            student_name=profile.get("name", "Unknown"),
            question=question,
            topic=self._detect_topic(question),
        )

    def _missing_fields(self, profile: Dict[str, Any]) -> List[str]:
        important_fields = [
            "name",
            "email",
            "institution",
            "major",
            "graduation_year",
            "gpa",
            "gpa_scale",
            "skills",
            "projects",
            "research",
            "work_months",
            "budget",
        ]

        missing = []

        for field in important_fields:
            value = profile.get(field)

            if value is None or value == "" or value == []:
                missing.append(field)

        return missing

    def _profile_context(self, profile: Dict[str, Any]) -> str:
        assessment = profile.get("assessments", {}).get(self.university_id, {})

        compact_profile = {
            "name": profile.get("name"),
            "email": profile.get("email"),
            "institution": profile.get("institution"),
            "major": profile.get("major"),
            "graduation_year": profile.get("graduation_year"),
            "gpa": profile.get("gpa"),
            "gpa_scale": profile.get("gpa_scale"),
            "gre_quant": profile.get("gre_quant"),
            "gre_verbal": profile.get("gre_verbal"),
            "toefl": profile.get("toefl"),
            "budget": profile.get("budget"),
            "work_months": profile.get("work_months"),
            "research": profile.get("research"),
            "disciplines": profile.get("disciplines", []),
            "skills": profile.get("skills", [])[:15],
            "projects": profile.get("projects", [])[:5],
            "notes": profile.get("notes"),
            "missing_fields": self._missing_fields(profile),
            "assessment": assessment,
        }

        return json.dumps(compact_profile, indent=2, ensure_ascii=False)

    def answer(
        self,
        question: str,
        profile: Dict[str, Any],
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        conversation_history = conversation_history or []

        try:
            self._log_question(question, profile)

            response = client.messages.create(
                model=MODEL,
                max_tokens=350,
                system=(
                    self.constitution
                    + "\n\nPROFILE:\n"
                    + self._profile_context(profile)
                ),
                messages=conversation_history + [
                    {
                        "role": "user",
                        "content": question,
                    }
                ],
            )

            if not response.content or not response.content[0].text:
                self._audit_failure(
                    event="empty_claude_response",
                    message="Claude returned an empty response.",
                    profile_name=profile.get("name"),
                    question=question,
                )

                return (
                    "Audit error: Claude returned an empty response. "
                    "Please check the profile data or retry the request."
                )

            return response.content[0].text

        except anthropic.APIConnectionError as e:
            self._audit_failure(
                event="claude_connection_error",
                message="Claude API connection failed.",
                details=str(e),
                profile_name=profile.get("name"),
                question=question,
            )

            return "Audit error: Claude API connection failed. Please check internet/API availability."

        except anthropic.AuthenticationError as e:
            self._audit_failure(
                event="claude_authentication_error",
                message="Claude authentication failed. Check ANTHROPIC_API_KEY.",
                details=str(e),
                profile_name=profile.get("name"),
                question=question,
            )

            return "Audit error: Claude authentication failed. Please check ANTHROPIC_API_KEY."

        except anthropic.RateLimitError as e:
            self._audit_failure(
                event="claude_rate_limit_error",
                message="Claude rate limit reached.",
                details=str(e),
                profile_name=profile.get("name"),
                question=question,
            )

            return "Audit error: Claude rate limit reached. Please retry after some time."

        except anthropic.APIError as e:
            self._audit_failure(
                event="claude_api_error",
                message="Claude API failed while generating profile answer.",
                details=str(e),
                profile_name=profile.get("name"),
                question=question,
            )

            return "Audit error: Claude API failed while generating the profile answer."

        except Exception as e:
            self._audit_failure(
                event="profile_presenter_error",
                message="Unexpected error in ProfilePresenterAgent.",
                details=str(e),
                profile_name=profile.get("name"),
                question=question,
            )

            return f"Audit error: Profile presenter failed clearly instead of silently. Details: {str(e)}"