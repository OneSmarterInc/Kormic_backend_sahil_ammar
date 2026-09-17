# agents/identity_registry.py
# A3: birth records + agent-to-agent conversation logging (django_api.
# models.AgentIdentity / AgentConversationLog). Identities are created
# lazily, the first time an agent is actually used -- see call sites in
# agents.agent_identity.ensure_agent_name (student) and
# agents.university_agent.UniversityAgent.__init__ (university) -- rather
# than at profile/university creation time, so an identity row means "this
# agent has actually been used" the same way the agent_name it's built
# around does.

from __future__ import annotations

from typing import Any, Dict, Optional


def get_or_create_identity(owner_type: str, owner_id: str, agent_name: str = ""):
    from django_api.models import AgentIdentity

    owner_id = str(owner_id or "").strip()
    if not owner_id:
        raise ValueError("owner_id is required to create an agent identity.")

    identity, created = AgentIdentity.objects.get_or_create(
        owner_type=owner_type,
        owner_id=owner_id,
        defaults={"agent_name": agent_name or ""},
    )

    if not created and agent_name and identity.agent_name != agent_name:
        identity.agent_name = agent_name
        identity.save(update_fields=["agent_name"])

    return identity


def student_identity(student_id: str, agent_name: str = ""):
    from django_api.models import AgentIdentity

    return get_or_create_identity(AgentIdentity.OwnerType.STUDENT, student_id, agent_name)


def university_identity(university_id: str, agent_name: str = ""):
    from django_api.models import AgentIdentity

    return get_or_create_identity(AgentIdentity.OwnerType.UNIVERSITY, university_id, agent_name)


def log_conversation(
    *,
    student_id: str,
    university_id: str,
    question: str,
    answer: str,
    knowledge_source: str = "",
    confidence: Optional[float] = None,
) -> Optional[Any]:
    """Log one student-agent -> university-agent exchange. Best-effort: a
    missing student/university id (e.g. an unresolved university_id in a
    test or a not-yet-persisted student) skips logging rather than breaking
    the chat turn, since the exchange itself already happened either way."""
    from django_api.models import AgentConversationLog

    student_id = str(student_id or "").strip()
    university_id = str(university_id or "").strip()

    if not student_id or not university_id:
        return None

    asker = student_identity(student_id)
    responder = university_identity(university_id)

    return AgentConversationLog.objects.create(
        asker=asker,
        responder=responder,
        question=question or "",
        answer=answer or "",
        knowledge_source=knowledge_source or "",
        confidence=confidence,
    )


def log_exchange_from_result(*, student_id: str, university_id: str, question: str, result: Dict[str, Any]) -> Optional[Any]:
    """Convenience wrapper around log_conversation() for the dict shape
    agents.university_agent.UniversityAgent.answer() returns."""
    trust = result.get("trust") or {}
    knowledge_source = result.get("source") or trust.get("source_type") or ""

    return log_conversation(
        student_id=student_id,
        university_id=university_id,
        question=question,
        answer=result.get("answer", ""),
        knowledge_source=knowledge_source,
        confidence=result.get("confidence"),
    )
