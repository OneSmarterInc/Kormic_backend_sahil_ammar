# agents/commons.py
# The Kormic Commons — where agents live and communicate.
#
# This is the single orchestration layer every backend agent (university
# agents, verification, fit assessment) is reached through. The student
# never talks to any of these agents directly -- only their personal agent
# (the LangGraph runtime in pure_multi_agent/) does, via the functions in
# this module, as tools. This module also owns the lazy-build/in-process
# -cache lifecycle for every agent instance.

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from rich.console import Console

console = Console()

# The Commons registry — all active university agents register here.
_university_agents: Dict[str, Any] = {}

# In-process cache for the other lazily-built, session-scoped agents.
# Rebuilt from persisted DB state on first use per worker process --
# same lifecycle/caveats as _university_agents (not shared across workers,
# not durable across restarts; only the underlying DB rows are durable).
_profile_presenters: Dict[str, Any] = {}


# ---------------------------------------------------------------------
# University agent registry
# ---------------------------------------------------------------------

def register(university_id: str, agent: Any) -> None:
    """Register a university agent in the Commons."""
    _university_agents[university_id] = agent

    agent_name = getattr(agent, "persona", {}).get("agent_name", university_id)

    console.print(
        f"[dim]Commons: {agent_name} registered as {university_id}.[/dim]"
    )


def unregister(university_id: str) -> bool:
    """Remove a university agent from the Commons if it exists."""
    if university_id in _university_agents:
        del _university_agents[university_id]
        console.print(f"[dim]Commons: {university_id} unregistered.[/dim]")
        return True

    return False


def get_agent(university_id: str) -> Optional[Any]:
    """
    Return a registered university agent without asking it a question, and
    without lazily building one. Use get_university_agent() instead when the
    caller wants a usable agent whether or not it's been built yet.
    """
    return _university_agents.get(university_id)


def list_agents() -> List[str]:
    """Return IDs of all registered university agents."""
    return list(_university_agents.keys())


# University directory -- single source of truth for which university ids
# exist, read from the universities app's University table (self-service,
# no hardcoded persona dict).

def list_university_ids() -> List[str]:
    """Every known university id, name-sorted."""
    from universities.models import University

    return [str(u) for u in University.objects.order_by("name").values_list("uuid", flat=True)]


def list_university_directory() -> List[Dict[str, str]]:
    """id/name/agent_name for every known university, in one query --
    backs read-only directory lookups (e.g. the student agent's
    list_universities tool) without building a full UniversityAgent."""
    from universities.models import University

    return [
        {"id": str(row.uuid), "name": row.name, "agent_name": row.agent_name or str(row.uuid)}
        for row in University.objects.order_by("name")
    ]


def get_university_agent_label(university_id: str) -> str:
    """Display label (agent name, falling back to name/id) for a university
    without constructing a full UniversityAgent."""
    from django_api.services import as_uuid
    from universities.models import University

    university = (
        University.objects.filter(uuid=university_id).first()
        if as_uuid(university_id) else None
    )
    if university is None:
        return university_id

    return university.agent_name or university.name or university_id


# ---------------------------------------------------------------------
# Lazy agent construction + caching
# ---------------------------------------------------------------------

def get_university_agent(university_id: str, auto_scrape: Optional[bool] = None) -> Any:
    """
    Return the cached university agent, building and registering it on
    first use. Raises ValueError for an unknown university_id.
    """
    if university_id in _university_agents:
        return _university_agents[university_id]

    from agents.university_agent import UniversityAgent
    from django_api.services import as_uuid
    from universities.models import University

    if not as_uuid(university_id) or not University.objects.filter(uuid=university_id).exists():
        raise ValueError(f"Unknown university_id: {university_id}")

    if auto_scrape is None:
        auto_scrape = os.getenv("KORMIC_AUTO_SCRAPE", "false").lower() == "true"

    agent = UniversityAgent(university_id, auto_scrape=auto_scrape)
    register(university_id, agent)

    return agent


def get_profile_presenter(university_id: str) -> Any:
    """Return the cached officer-facing ProfilePresenterAgent for a university."""
    if university_id in _profile_presenters:
        return _profile_presenters[university_id]

    from agents.profile_presenter import ProfilePresenterAgent

    _profile_presenters[university_id] = ProfilePresenterAgent(university_id)
    return _profile_presenters[university_id]


# ---------------------------------------------------------------------
# Verification (used by the student's personal agent -- there is no
# direct student-facing "reanalyze"/"decision" endpoint anymore; those
# actions only happen via chat, routed through here)
# ---------------------------------------------------------------------

def run_verification(student_id: str, user: Any = None) -> Dict[str, Any]:
    from verification.services import run_verification as _run_verification

    return _run_verification(student_id, user=user)


def list_verification_items(student_id: str, filter_status: str = "open") -> Dict[str, Any]:
    from verification.services import list_items

    return list_items(student_id, filter_status)


def resolve_verification_item(*, student_id: str, item_id: int, action: str, note: str = "") -> Dict[str, Any]:
    from verification.services import resolve_item

    return resolve_item(student_id=student_id, item_id=item_id, action=action, note=note)


# ---------------------------------------------------------------------
# Fit assessment (used by the student's personal agent -- moved from
# django_api/views.generate_fit_assessment, which is no longer a direct
# student-facing POST endpoint)
# ---------------------------------------------------------------------

def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _fallback_fit_assessment(profile: Dict[str, Any], university_id: str, agent: Optional[Any] = None) -> Dict[str, Any]:
    """Deterministic fit estimate used when the AI assessment fails or is
    unusable -- keeps the student agent able to answer even if the model
    call errors out."""
    gpa = _safe_number(profile.get("gpa"), 0)
    gpa_scale = _safe_number(profile.get("gpa_scale"), 10)
    gre_quant = _safe_number(profile.get("gre_quant"), 0)
    toefl = _safe_number(profile.get("toefl"), 0)
    ielts = _safe_number(profile.get("ielts"), 0)
    budget = _safe_number(profile.get("budget"), 0)
    major = str(profile.get("major", "")).lower()
    program = str(profile.get("program", "")).lower()
    research = str(profile.get("research", "")).lower()

    score = 45
    strengths: List[str] = []
    gaps: List[str] = []
    gpa_percent = gpa / gpa_scale if gpa_scale > 0 else 0

    if gpa_percent >= 0.80:
        score += 15
        strengths.append("Strong academic profile based on GPA.")
    elif gpa_percent >= 0.70:
        score += 8
        strengths.append("Decent academic profile.")
    else:
        gaps.append("GPA may need stronger support through projects, GRE, or experience.")

    if "computer" in major or "cs" in major or "computer" in program or "science" in program or "software" in program:
        score += 12
        strengths.append("Academic background aligns with Computer Science.")
    else:
        gaps.append("Program background alignment should be explained clearly.")

    if gre_quant >= 165:
        score += 10
        strengths.append("Strong GRE Quant score.")
    elif gre_quant >= 160:
        score += 6
        strengths.append("Good GRE Quant score.")
    elif gre_quant > 0:
        gaps.append("GRE Quant score may be moderate for CS programs.")
    else:
        gaps.append("GRE score is missing or not provided.")

    if toefl >= 90:
        score += 8
        strengths.append("TOEFL score looks acceptable for many graduate programs.")
    elif ielts >= 6.5:
        score += 8
        strengths.append("IELTS score looks acceptable for many graduate programs.")
    elif toefl > 0 or ielts > 0:
        gaps.append("English proficiency score should be verified against the university minimum.")
    else:
        gaps.append("English proficiency score is missing.")

    if any(word in research for word in ["ai", "ml", "web", "project", "research", "internship"]):
        score += 8
        strengths.append("Projects/research experience supports the application.")
    else:
        gaps.append("More project or research detail would improve the profile.")

    if budget >= 40000:
        score += 7
        strengths.append("Budget appears reasonable for many US graduate options, but tuition must be verified.")
    elif budget >= 25000:
        score += 3
        gaps.append("Budget may need careful planning depending on tuition and living cost.")
    else:
        gaps.append("Budget may be tight for US graduate study.")

    from django_api.services import as_uuid
    from universities.models import University

    university = (
        University.objects.filter(uuid=university_id).first()
        if as_uuid(university_id) else None
    )
    university_name = university.name if university else university_id
    agent_name = (
        (university.agent_name if university else None)
        or (getattr(agent, "agent_name", university_id) if agent else university_id)
    )

    score = max(0, min(100, int(score)))

    if score >= 80:
        match_tier = "strong"
        recommendation = "recommend"
        realistic = True
    elif score >= 40:
        match_tier = "target"
        recommendation = "recommend"
        realistic = True
    elif score >= 50:
        match_tier = "possible"
        recommendation = "consider"
        realistic = True
    else:
        match_tier = "reach"
        recommendation = "consider"
        realistic = False

    return {
        "match_tier": match_tier,
        "match_score": score,
        "fit_summary": f"Based on the available profile, this looks like a {match_tier} fit for {university_name}.",
        "strengths_for_program": strengths or ["Basic profile information is available for assessment."],
        "gaps_for_program": gaps or ["Verify official requirements on the university website."],
        "recommendation": recommendation,
        "realistic": realistic,
        "specific_advice": "Verify tuition, deadlines, GRE/TOEFL, and funding before final decision.",
        "university": university_name,
        "agent": agent_name,
        "assessment_source": "api_fallback",
    }


def _assessment_failed(assessment: Any) -> bool:
    if not isinstance(assessment, dict):
        return True
    if assessment.get("match_tier") == "unknown":
        return True
    # match_score may arrive as a non-integer string ("75", "75.5") from the
    # model -- coerce defensively rather than letting int() raise.
    if _safe_number(assessment.get("match_score"), 0) <= 0:
        return True
    return False


def generate_fit_assessment(student_id: str, university_id: str, force: bool = False) -> Dict[str, Any]:
    """
    Generate (or return the cached) fit assessment for one student/university
    pair. This is the only way a fit assessment gets produced -- there is no
    direct student-facing endpoint; the student's personal agent calls this
    when a fit/match question comes up in chat.
    """
    from django_api.models import FitAssessment, StudentProfile
    from django_api.services import as_uuid, load_profile_data, save_profile_data

    student_key = as_uuid(student_id)
    if student_key is None:
        raise ValueError(f"Invalid student_id: {student_id!r}")

    if not force:
        cached = (
            FitAssessment.objects.filter(student__uuid=student_key, university_id=university_id)
            .order_by("-created_at")
            .first()
        )
        if cached:
            response_data = dict(cached.assessment)
            response_data["cached"] = True
            response_data["generated_at"] = cached.created_at.isoformat()
            return response_data

    profile = load_profile_data(student_id)
    agent = get_university_agent(university_id)

    try:
        assessment = agent.assess_fit(profile)
    except Exception:
        assessment = None

    if _assessment_failed(assessment):
        assessment = _fallback_fit_assessment(profile, university_id, agent)

    profile.setdefault("assessments", {})
    profile["assessments"][university_id] = assessment
    save_profile_data(student_id, profile)

    # A fit assessment can be requested before the student has a saved
    # StudentProfile row -- create one rather than 500 on .get().
    student_row, _ = StudentProfile.objects.get_or_create(uuid=student_key)
    row = FitAssessment.objects.create(
        student=student_row,
        university_id=university_id,
        assessment=assessment,
    )

    response_data = dict(assessment)
    response_data["cached"] = False
    response_data["generated_at"] = row.created_at.isoformat()

    return response_data


def record_university_interest(student_id: str, university_id: str, source: str) -> None:
    from django_api.services import record_university_interest as _record_university_interest

    _record_university_interest(student_id, university_id, source)


# ---------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------

def status() -> str:
    """Show the current state of the Kormic Commons."""
    if not _university_agents:
        return "The Kormic Commons is empty — no agents registered yet."

    lines = [
        f"\n{'=' * 60}",
        "  THE KORMIC COMMONS",
        f"  {len(_university_agents)} university agent(s) active",
        f"{'=' * 60}",
    ]

    for university_id, agent in _university_agents.items():
        try:
            lines.append(f"  {agent.status()}")
        except Exception:
            agent_name = getattr(agent, "persona", {}).get("agent_name", university_id)
            lines.append(f"  {agent_name} ({university_id}) — status unavailable")

    lines.append(f"{'=' * 60}\n")

    return "\n".join(lines)
