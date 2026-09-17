from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from django_api.models import (
    ChatAttachment,
    GitHubAnalysis,
    LinkedInAnalysis,
    ResumeUpload,
    StudentProfile,
)

BASE_DIR = Path(__file__).resolve().parents[1]
UPLOADS_DIR = BASE_DIR / "uploads"

UPLOADS_DIR.mkdir(exist_ok=True)


# Columns on StudentProfile that participate in the flat profile dict.
# Anything in a saved profile dict that isn't one of these keys (plus
# student_id/created_at/updated_at, handled specially) is preserved in
# `extra_data` so no data from the agents is ever dropped.
# NOTE: the flat-dict key stays "student_id" for API stability; its value is
# now StudentProfile.uuid (a random UUID string), never an email-derived slug.
PROFILE_FIELDS: List[str] = [
    "name", "email", "country", "institution", "major", "program", "graduation_year",
    "gpa", "gpa_scale", "gpa_text",
    "gre_quant", "gre_verbal", "toefl", "ielts", "english_score_text",
    "budget", "budget_text", "work_months",
    "github", "github_assessment",
    "linkedin_url", "linkedin_profile",
    "notes", "source", "verified",
    "skills", "technical_skills", "soft_skills",
    "projects",
    "research", "research_interests", "publications", "publications_count",
    "career_goals", "conversation_insights", "assessments", "preferences", "evidence",
    "academic_intelligence", "technical_intelligence", "research_intelligence", "behaviour_intelligence",
    "overall_profile_score", "overall_profile", "profile_completeness",
    "strengths", "weaknesses", "recommendations",
    "ai_summary", "summary",
    "roadmap",
    "disciplines", "gaps", "parser_status", "parser_engine", "response_mode",
    "work_experience_summary",
]


def as_uuid(value: Any) -> Optional[str]:
    """Return `value` as a canonical UUID string, or None if it isn't a
    well-formed UUID. Lets lookups on the uuid columns treat a malformed or
    unknown identifier as "not found" instead of raising ValidationError."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _profile_to_dict(profile: StudentProfile) -> Dict[str, Any]:
    data: Dict[str, Any] = dict(profile.extra_data or {})

    for field in PROFILE_FIELDS:
        data[field] = getattr(profile, field)

    data["student_id"] = str(profile.uuid)
    data["created_at"] = profile.created_at.strftime("%Y-%m-%d %H:%M:%S")
    data["updated_at"] = profile.updated_at.strftime("%Y-%m-%d %H:%M:%S")

    return data


def _apply_dict_to_profile(profile: StudentProfile, data: Dict[str, Any]) -> None:
    extra_data = dict(profile.extra_data or {})

    for key, value in (data or {}).items():
        if key in ("student_id", "created_at", "updated_at"):
            continue
        if key in PROFILE_FIELDS:
            if value is None:
                # Columns default to "", [] or {} rather than NULL; only the
                # genuinely nullable numeric columns keep None as None.
                existing = getattr(profile, key)
                if isinstance(existing, str):
                    value = ""
                elif isinstance(existing, list):
                    value = []
                elif isinstance(existing, dict):
                    value = {}
            setattr(profile, key, value)
        else:
            extra_data[key] = value

    profile.extra_data = extra_data


def get_profile_path(student_id: str) -> str:
    """Kept for backward-compatible informational responses; profiles are DB-backed now."""
    return f"db://student_profiles/{student_id}"


def load_profile_data(student_id: str) -> Dict[str, Any]:
    profile = (
        StudentProfile.objects.filter(uuid=student_id).first()
        if as_uuid(student_id) else None
    )

    if profile is None:
        return {
            "student_id": student_id,
            "name": student_id,
            "conversation_insights": [],
            "assessments": {},
            "preferences": {},
            "evidence": {},
            "source": "api",
            "verified": False,
        }

    return _profile_to_dict(profile)


def save_profile_data(student_id: str, data: Dict[str, Any]) -> str:
    data = dict(data or {})
    data["student_id"] = student_id

    profile, _ = StudentProfile.objects.get_or_create(uuid=student_id)
    _apply_dict_to_profile(profile, data)
    profile.save()

    return get_profile_path(student_id)


def generate_summary(profile: Dict[str, Any]) -> str:
    preferences = profile.get("preferences", {}) or {}
    summary = f"""
Student Profile Summary
-----------------------
Name: {profile.get('name', 'Unknown')}
Student ID: {profile.get('student_id', 'student')}
GPA: {profile.get('gpa', 'Not Provided')}
GRE Quant: {profile.get('gre_quant', 'Not Provided')}
TOEFL/IELTS: {profile.get('toefl') or profile.get('ielts') or 'Not Provided'}
Budget: {profile.get('budget', 'Not Provided')}
Target Country: {preferences.get('target_country', 'Not Provided')}
Preferred Specialization: {preferences.get('preferred_specialization', 'Not Provided')}

Conversation Insights:
{len(profile.get('conversation_insights', []))}

University Assessments:
{len(profile.get('assessments', {}))}
"""
    profile["summary"] = summary
    return summary


# Score/numeric fields that must never be silently reset to null once a real
# value is on file. 0 / 0.0 are legitimate values and always accepted; only an
# explicit attempt to null out an already-set value is rejected.
NULLABLE_SCORE_FIELDS = {
    "gpa", "gre_quant", "gre_verbal", "toefl", "ielts", "budget", "graduation_year", "work_months",
}


class ProfileValidationError(ValueError):
    """Raised when profile update data fails a business-rule validation."""

    def __init__(self, errors: Dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))


def create_or_update_profile(validated_data: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(validated_data)
    raw_student_id = data.get("student_id")
    name = data.get("name") or "Student"

    if raw_student_id:
        student_id = str(raw_student_id)
    else:
        # No caller id supplied -- mint a fresh profile and use its uuid.
        student_id = str(StudentProfile.objects.create().uuid)

    profile = load_profile_data(student_id)
    profile["student_id"] = student_id

    direct_fields = [
        "name", "email", "country", "institution", "major", "program", "graduation_year",
        "gpa", "gpa_scale", "gre_quant", "gre_verbal", "toefl", "ielts", "english_score_text",
        "budget", "github", "linkedin_url", "notes", "work_months", "work_experience_summary",
    ]

    null_field_errors: Dict[str, str] = {}

    for field in direct_fields:
        if field not in data:
            continue

        value = data[field]

        if value in [None, ""]:
            if field in NULLABLE_SCORE_FIELDS and profile.get(field) not in [None, ""]:
                null_field_errors[field] = (
                    "This field already has a value and cannot be cleared to null. "
                    "Provide a number (0 is allowed) instead."
                )
            continue

        profile[field] = value

    if null_field_errors:
        raise ProfileValidationError(null_field_errors)

    preference_fields = ["target_country", "target_degree", "preferred_specialization"]
    profile.setdefault("preferences", {})

    for field in preference_fields:
        if field in data and data[field] not in [None, ""]:
            profile["preferences"][field] = data[field]

    profile.setdefault("conversation_insights", [])
    profile.setdefault("assessments", {})
    profile.setdefault("evidence", {})
    profile["evidence"]["manual_profile_api"] = data

    generate_summary(profile)
    path = save_profile_data(student_id, profile)

    return {"student_id": student_id, "profile_file": str(path), "profile": profile}


def get_profile(student_id: str) -> Dict[str, Any]:
    if not as_uuid(student_id) or not StudentProfile.objects.filter(uuid=student_id).exists():
        raise FileNotFoundError(f"Profile not found for student_id: {student_id}")

    return load_profile_data(student_id)


def _normalize_gpa_to_4_scale(gpa: Any, gpa_scale: Any) -> Optional[float]:
    """Best-effort conversion of a stored GPA to a 4.0-scale equivalent.

    gpa_scale is free text (agents/users write "4.0", "10.0", "percentage", ...)
    and is sometimes wrong/stale relative to the actual gpa value (e.g. a 9.0/10
    GPA mislabeled as scale "4.0"), so when the raw value exceeds the stated
    scale we re-infer the scale from the value's own magnitude instead of
    silently producing a >4.0 "normalized" score.
    """
    if gpa in (None, ""):
        return None

    try:
        gpa = float(gpa)
    except (TypeError, ValueError):
        return None

    scale_match = re.search(r"[\d.]+", str(gpa_scale or ""))
    scale = float(scale_match.group()) if scale_match else 4.0
    if scale <= 0:
        scale = 4.0

    if gpa > scale:
        if gpa <= 4.0:
            scale = 4.0
        elif gpa <= 5.0:
            scale = 5.0
        elif gpa <= 10.0:
            scale = 10.0
        else:
            scale = 100.0

    return round(min(gpa / scale, 1.0) * 4.0, 2)


def _analyze_academics(profile: Dict[str, Any]) -> Dict[str, Any]:
    gpa = _normalize_gpa_to_4_scale(profile.get("gpa"), profile.get("gpa_scale"))
    gre_quant = profile.get("gre_quant")
    toefl = profile.get("toefl")
    ielts = profile.get("ielts")

    score = 0
    strengths: List[str] = []
    weaknesses: List[str] = []
    recommendations: List[str] = []
    evidence: List[str] = []

    if gpa is not None:
        if gpa >= 3.8:
            score += 40
            strengths.append("Outstanding academic performance")
            evidence.append(f"GPA (normalized to 4.0 scale: {gpa}) demonstrates excellent academic consistency.")
        elif gpa >= 3.5:
            score += 35
            strengths.append("Strong GPA")
        elif gpa >= 3.0:
            score += 30
            strengths.append("Meets typical graduate admission GPA bar")
        else:
            score += 15
            weaknesses.append("GPA is below the range preferred by competitive programs.")
            recommendations.append("Strengthen academics with certifications or additional coursework.")
    else:
        weaknesses.append("GPA is not provided.")
        recommendations.append("Add GPA to the profile.")

    if gre_quant:
        if gre_quant >= 165:
            score += 30
            strengths.append("Outstanding quantitative ability")
        elif gre_quant >= 160:
            score += 25
            strengths.append("Strong quantitative reasoning")
        elif gre_quant >= 155:
            score += 20
        else:
            weaknesses.append("GRE Quant score could be improved.")
    else:
        weaknesses.append("GRE score not provided.")
        recommendations.append("Take the GRE if the target universities recommend it.")

    if (toefl and toefl >= 105) or (ielts and ielts >= 7.5):
        score += 20
        strengths.append("Excellent English proficiency")
        evidence.append(f"TOEFL/IELTS score of {toefl or ielts} demonstrates strong communication skills.")
    elif (toefl and toefl >= 90) or (ielts and ielts >= 6.5):
        score += 12
        strengths.append("Good English proficiency")
    elif toefl or ielts:
        score += 6
    else:
        weaknesses.append("English proficiency score (TOEFL/IELTS) not provided.")
        recommendations.append("Take an English proficiency test (TOEFL/IELTS) before applying.")

    score = min(score, 100)
    readiness = "High" if score >= 80 else "Moderate" if score >= 60 else "Needs Improvement"

    return {
        "academic_score": score,
        "readiness": readiness,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "evidence": evidence,
    }


def _analyze_technical(profile: Dict[str, Any]) -> Dict[str, Any]:
    skills = profile.get("skills") or []
    projects = profile.get("projects") or []
    research = profile.get("research")
    work_months = profile.get("work_months") or 0
    github_url = profile.get("github")
    github_evidence = ((profile.get("evidence") or {}).get("github")) or {}

    skill_names = [str(skill).strip().lower() for skill in skills if str(skill).strip()]

    ai_skills = {"python", "tensorflow", "pytorch", "keras", "opencv", "scikit-learn",
                 "machine learning", "deep learning", "nlp", "agentic ai", "rag pipelines", "langchain"}
    web_skills = {"html", "css", "javascript", "react", "react.js", "angular", "node", "node.js",
                  "django", "django rest framework", "flask", "fastapi", "bootstrap", "tailwind css"}
    database_skills = {"mysql", "postgresql", "mongodb", "sql", "supabase", "redis"}

    ai_count = sum(1 for skill in skill_names if skill in ai_skills)
    web_count = sum(1 for skill in skill_names if skill in web_skills)
    db_count = sum(1 for skill in skill_names if skill in database_skills)
    project_count = len(projects)

    score = 0
    strengths: List[str] = []
    weaknesses: List[str] = []
    recommendations: List[str] = []
    evidence: List[str] = []

    if ai_count >= 3:
        score += 30
        strengths.append("Strong AI / Machine Learning skillset")
    elif ai_count > 0:
        score += 15
        strengths.append("Basic AI / Machine Learning knowledge")
    else:
        weaknesses.append("Limited AI / Machine Learning exposure.")
        recommendations.append("Learn PyTorch, TensorFlow, or Scikit-Learn.")

    if web_count >= 3:
        score += 20
        strengths.append("Strong web development skills")
    elif web_count > 0:
        score += 10
        strengths.append("Basic web development knowledge")
    else:
        weaknesses.append("Limited web development experience.")

    if db_count >= 2:
        score += 15
        strengths.append("Strong database knowledge")
    elif db_count > 0:
        score += 8

    if project_count >= 4:
        score += 25
        strengths.append("Excellent project portfolio")
        evidence.append(f"{project_count} projects completed.")
    elif project_count >= 2:
        score += 15
        strengths.append("Good practical project experience")
        evidence.append(f"{project_count} projects completed.")
    else:
        weaknesses.append("Needs more practical projects.")
        recommendations.append("Build more end-to-end software projects.")

    if work_months >= 6:
        score += 20
        strengths.append("Strong industry experience")
        evidence.append(f"{work_months} months of professional experience.")
    elif work_months > 0:
        score += 10
        strengths.append("Some industry exposure")
    else:
        weaknesses.append("No internship or industry experience.")
        recommendations.append("Complete at least one internship.")

    if research and research != "None stated":
        score += 15
        strengths.append("Research experience available")
    else:
        weaknesses.append("No research experience.")
        recommendations.append("Participate in research or publish a paper.")

    if github_url:
        score += 10
        strengths.append("GitHub profile available")
    else:
        recommendations.append("Create and maintain an active GitHub profile.")

    # Layer in the richer, already-analyzed GitHub evidence when present
    # instead of re-deriving it from a keyword match against `skills`.
    if github_evidence.get("verified"):
        evidence.append(
            f"GitHub analysis: {github_evidence.get('overall_level', 'unknown')} level across "
            f"{github_evidence.get('months_active', 0)} months of activity."
        )
        strengths.extend(github_evidence.get("strengths", [])[:2])
        weaknesses.extend(github_evidence.get("honest_gaps", [])[:2])

    score = min(score, 100)
    if score >= 90:
        level = "Excellent"
    elif score >= 75:
        level = "Very Strong"
    elif score >= 60:
        level = "Strong"
    elif score >= 40:
        level = "Intermediate"
    else:
        level = "Beginner"

    return {
        "technical_score": score,
        "technical_level": level,
        "skill_matrix": {
            "AI / Machine Learning": ai_count,
            "Web Development": web_count,
            "Databases": db_count,
            "Projects": project_count,
            "Industry Experience Months": work_months,
        },
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "evidence": evidence,
    }


def _analyze_research(profile: Dict[str, Any]) -> Dict[str, Any]:
    research = profile.get("research")
    publications = profile.get("publications") or []
    interests = profile.get("research_interests") or []

    score = 0
    strengths: List[str] = []
    weaknesses: List[str] = []
    recommendations: List[str] = []

    if research and research != "None stated":
        score += 40
        strengths.append("Research experience mentioned.")
    else:
        weaknesses.append("No direct research experience found.")
        recommendations.append("Add research projects, paper work, or academic exploration.")

    if publications:
        score += 40
        strengths.append("Publication record available.")
    else:
        recommendations.append("Try to publish or document research-based work.")

    if interests:
        score += 20
        strengths.append("Research interests are clearly listed.")
    else:
        weaknesses.append("Research interests are missing.")

    return {
        "research_score": min(score, 100),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
    }


def _analyze_behaviour(profile: Dict[str, Any]) -> Dict[str, Any]:
    insights = profile.get("conversation_insights") or []
    score = min(len(insights) * 10, 100)

    return {
        "behaviour_score": score,
        "evidence_count": len(insights),
        "summary": "Behaviour analysis is based on stored conversation insights.",
    }


def _profile_completeness(profile: Dict[str, Any]) -> Dict[str, Any]:
    evidence = profile.get("evidence") or {}
    research = profile.get("research")

    checks = [
        ("Name", bool(profile.get("name"))),
        ("Email", bool(profile.get("email"))),
        ("Institution", bool(profile.get("institution"))),
        ("Major", bool(profile.get("major"))),
        ("GPA", profile.get("gpa") is not None),
        ("Standardized test score (GRE, TOEFL, or IELTS)",
         any([profile.get("gre_quant"), profile.get("toefl"), profile.get("ielts")])),
        ("Budget", profile.get("budget") is not None),
        ("Target disciplines / programs", bool(profile.get("disciplines"))),
        ("Research experience", bool(research) and research != "None stated"),
        ("Skills", bool(profile.get("skills"))),
        ("Projects", bool(profile.get("projects"))),
        ("Work experience", bool(profile.get("work_months"))),
        ("GitHub profile (verified)", bool((evidence.get("github") or {}).get("verified"))),
        ("LinkedIn profile", bool(profile.get("linkedin_url")) or bool(evidence.get("linkedin"))),
    ]

    completed_items = [label for label, ok in checks if ok]
    missing_items = [label for label, ok in checks if not ok]
    total = len(checks)

    return {
        "percentage": round(len(completed_items) / total * 100),
        "completed_count": len(completed_items),
        "total_count": total,
        "completed_items": completed_items,
        "missing_items": missing_items,
    }


def compute_profile_intelligence(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute profile scoring/intelligence fresh from live profile data on every
    request, rather than reading the academic_intelligence/overall_profile_score/
    etc. DB columns directly -- those columns are only ever written by the
    legacy chat-driven build_ai_profile() flow, so they stay stuck at their
    defaults (0/{}/[]) for any profile built via the resume/GitHub/LinkedIn
    upload APIs instead of a chat conversation.
    """
    academic = _analyze_academics(profile)
    technical = _analyze_technical(profile)
    research = _analyze_research(profile)
    behaviour = _analyze_behaviour(profile)
    completeness = _profile_completeness(profile)

    overall_score = round(
        (academic["academic_score"] * 0.35)
        + (technical["technical_score"] * 0.45)
        + (research["research_score"] * 0.20)
    )
    overall_score = min(overall_score, 100)

    if overall_score >= 90:
        profile_level = "Excellent"
    elif overall_score >= 80:
        profile_level = "Very Strong"
    elif overall_score >= 70:
        profile_level = "Strong"
    elif overall_score >= 60:
        profile_level = "Moderate"
    else:
        profile_level = "Needs Improvement"

    if overall_score >= 80:
        recommendation = "Highly recommended for ambitious and target universities."
    elif overall_score >= 40:
        recommendation = "Suitable for target universities with a balanced application strategy."
    else:
        recommendation = "Strengthen the profile before applying to competitive universities."

    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    strengths = _dedupe(academic["strengths"] + technical["strengths"] + research["strengths"])
    weaknesses = _dedupe(academic["weaknesses"] + technical["weaknesses"] + research["weaknesses"])
    recommendations = _dedupe(academic["recommendations"] + technical["recommendations"] + research["recommendations"])

    ai_summary = (
        f"Overall Profile Score: {overall_score}/100 ({profile_level}).\n"
        f"Profile completion: {completeness['percentage']}% "
        f"({completeness['completed_count']}/{completeness['total_count']} items).\n"
        f"Academic readiness: {academic['readiness']}. Technical level: {technical['technical_level']}.\n"
        f"To reach 100% completion, add: {', '.join(completeness['missing_items']) or 'nothing -- profile is complete.'}"
    )

    return {
        "academic_intelligence": academic,
        "technical_intelligence": technical,
        "research_intelligence": research,
        "behaviour_intelligence": behaviour,
        "overall_profile_score": overall_score,
        "overall_profile": {
            "overall_score": overall_score,
            "profile_level": profile_level,
            "recommendation": recommendation,
        },
        "profile_completeness": completeness,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "ai_summary": ai_summary,
    }


def format_profile_response(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Group the flat profile dict returned by get_profile() into labelled
    sections for the GET /api/profile/<student_id>/ response.

    github_assessment/linkedin_profile are intentionally dropped here: they
    duplicate the richer, same-shaped data already under evidence.github /
    evidence.linkedin, which is the actual "duplicates" complaint this fixes.
    Anything not explicitly placed in a section is preserved under
    "additional" so no profile data is ever silently dropped.
    """
    consumed: set = {"github_assessment", "linkedin_profile"}

    def take(*keys: str) -> Dict[str, Any]:
        consumed.update(keys)
        return {key: profile.get(key) for key in keys}

    response: Dict[str, Any] = take("student_id", "created_at", "updated_at")

    profile_section = take(
        "name", "email", "country", "institution", "major", "program",
        "graduation_year", "gpa", "gpa_scale", "gpa_text",
    )
    profile_section.update(take("linkedin_url", "profile_image_url", "source", "verified"))
    profile_section["github_url"] = profile.get("github")
    consumed.add("github")
    response["profile"] = profile_section

    response["test_scores"] = take("gre_quant", "gre_verbal", "toefl", "ielts", "english_score_text")
    response["financials"] = take("budget", "budget_text")

    work_experience = take("work_months")
    work_experience["summary"] = profile.get("work_experience_summary")
    consumed.add("work_experience_summary")
    response["work_experience"] = work_experience

    response["skills"] = {
        "all_skills": profile.get("skills") or [],
        "technical_skills": profile.get("technical_skills") or [],
        "soft_skills": profile.get("soft_skills") or [],
    }
    consumed.update({"skills", "technical_skills", "soft_skills"})

    response["projects"] = profile.get("projects") or []
    consumed.add("projects")

    response["research"] = take("research", "research_interests", "publications", "publications_count")

    response["career"] = {
        "career_goals": profile.get("career_goals") or [],
        "target_disciplines": profile.get("disciplines") or [],
        "preferences": profile.get("preferences") or {},
    }
    consumed.update({"career_goals", "disciplines", "preferences"})

    response["resume_notes"] = profile.get("notes") or ""
    consumed.add("notes")

    response["gaps"] = profile.get("gaps") or []
    consumed.add("gaps")

    response["conversation_insights"] = profile.get("conversation_insights") or []
    consumed.add("conversation_insights")

    response["evidence"] = profile.get("evidence") or {}
    consumed.add("evidence")

    response["assessments"] = profile.get("assessments") or {}
    consumed.add("assessments")

    # academic/technical/research/behaviour_intelligence, overall_profile_score,
    # overall_profile, profile_completeness, strengths, weaknesses,
    # recommendations, and ai_summary are computed fresh below rather than
    # read from their (permanently-zero/empty) DB columns -- see
    # compute_profile_intelligence() for why.
    consumed.update({
        "academic_intelligence", "technical_intelligence", "research_intelligence", "behaviour_intelligence",
        "overall_profile_score", "overall_profile", "profile_completeness",
        "strengths", "weaknesses", "recommendations", "ai_summary",
    })

    intelligence = compute_profile_intelligence(profile)

    response["intelligence"] = {
        "academic_intelligence": intelligence["academic_intelligence"],
        "technical_intelligence": intelligence["technical_intelligence"],
        "research_intelligence": intelligence["research_intelligence"],
        "behaviour_intelligence": intelligence["behaviour_intelligence"],
    }

    response["profile_scoring"] = {
        "profile_completion_score": intelligence["profile_completeness"]["percentage"],
        "overall_profile_score": intelligence["overall_profile_score"],
        "overall_profile": intelligence["overall_profile"],
        "profile_completeness": intelligence["profile_completeness"],
        "strengths": intelligence["strengths"],
        "weaknesses": intelligence["weaknesses"],
        "recommendations": intelligence["recommendations"],
        "ai_summary": intelligence["ai_summary"],
        "summary": profile.get("summary"),
    }
    consumed.add("summary")

    response["roadmap"] = profile.get("roadmap") or {}
    consumed.add("roadmap")

    response["meta"] = take("response_mode", "parser_status", "parser_engine")

    leftover = {key: value for key, value in profile.items() if key not in consumed}
    if leftover:
        response["additional"] = leftover

    return response


def get_profile_image_path(student_id: str) -> Optional[str]:
    if not as_uuid(student_id):
        return None
    profile = StudentProfile.objects.filter(uuid=student_id).first()

    if profile is None or not profile.profile_image_path:
        return None

    return profile.profile_image_path


def upload_profile_image(student_id: str, uploaded_file) -> Dict[str, Any]:
    """
    Save/replace the student's single current profile picture.

    Unlike resumes or LinkedIn screenshots, a profile picture isn't a
    history -- each upload replaces the previous one, and the old file is
    removed from disk.
    """
    profile, _ = StudentProfile.objects.get_or_create(uuid=student_id)

    old_path = profile.profile_image_path
    if old_path and Path(old_path).exists():
        Path(old_path).unlink(missing_ok=True)

    file_path = save_uploaded_file(student_id, uploaded_file, "profile_images")
    profile.profile_image_path = str(file_path)
    profile.save()

    return {"student_id": student_id}


def delete_profile_image(student_id: str) -> bool:
    if not as_uuid(student_id):
        return False
    profile = StudentProfile.objects.filter(uuid=student_id).first()

    if profile is None or not profile.profile_image_path:
        return False

    old_path = Path(profile.profile_image_path)
    if old_path.exists():
        old_path.unlink(missing_ok=True)

    profile.profile_image_path = ""
    profile.save()

    return True


CHAT_ATTACHMENT_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
CHAT_ATTACHMENT_DOCUMENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
CHAT_ATTACHMENT_ALLOWED_TYPES = CHAT_ATTACHMENT_IMAGE_TYPES | CHAT_ATTACHMENT_DOCUMENT_TYPES
CHAT_ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024  # 15 MB per file
CHAT_ATTACHMENT_MAX_PER_MESSAGE = 5


def save_uploaded_file(student_id: str, uploaded_file, folder_name: str) -> Path:
    target_dir = UPLOADS_DIR / folder_name / str(student_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(uploaded_file.name).name
    # Suffix with a short uuid so repeated uploads of the same filename don't
    # clobber each other on disk -- needed so upload-history rows keep
    # pointing at distinct files.
    unique_name = f"{Path(safe_name).stem}__{uuid.uuid4().hex[:8]}{Path(safe_name).suffix}"
    file_path = target_dir / unique_name

    with open(file_path, "wb") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)

    return file_path


def save_chat_attachment(student_id: str, message, uploaded_file) -> ChatAttachment:
    """
    Save one chat-uploaded screenshot/document to disk and record it against
    `message` (a ChatMessage row already saved by the caller). Raises
    ValueError on an unsupported type or an over-size file -- callers are
    expected to turn that into a 400 response, same as other upload endpoints.
    """
    content_type = getattr(uploaded_file, "content_type", "") or ""
    if content_type not in CHAT_ATTACHMENT_ALLOWED_TYPES:
        raise ValueError(f"Unsupported attachment type: {content_type or 'unknown'}.")
    if uploaded_file.size > CHAT_ATTACHMENT_MAX_BYTES:
        raise ValueError(
            f"'{uploaded_file.name}' is too large. Max size is "
            f"{CHAT_ATTACHMENT_MAX_BYTES // (1024 * 1024)}MB."
        )

    file_path = save_uploaded_file(student_id, uploaded_file, "chat_attachments")
    return ChatAttachment.objects.create(
        message=message,
        file_path=str(file_path),
        original_filename=Path(uploaded_file.name).name,
        content_type=content_type,
        size_bytes=uploaded_file.size,
    )


def build_image_content_blocks(attachments) -> List[Dict[str, Any]]:
    """
    Turn image ChatAttachment rows into Anthropic-style vision content
    blocks (same block shape agents.linkedin_agent.LinkedInAgent uses), so
    the student's agent can actually see screenshots shared in chat, not
    just store them inertly. Non-image attachments (PDFs, docs) are skipped
    here -- they're stored and downloadable, but not sent to the model.
    """
    blocks: List[Dict[str, Any]] = []
    for attachment in attachments:
        if attachment.content_type not in CHAT_ATTACHMENT_IMAGE_TYPES:
            continue
        path = Path(attachment.file_path)
        if not path.exists():
            continue
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": attachment.content_type,
                "data": data,
            },
        })
    return blocks


def merge_resume_data_into_profile(student_id: str, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    profile = load_profile_data(student_id)

    field_map = {
        "undergraduate_institution": "institution",
        "undergraduate_major": "major",
        "work_experience_months": "work_months",
        "research_experience": "research",
        "technical_skills": "skills",
        "inferred_disciplines": "disciplines",
    }

    for key, value in (extracted_data or {}).items():
        if value in [None, "", [], {}]:
            continue
        mapped_key = field_map.get(key, key)
        profile[mapped_key] = value

    profile.setdefault("evidence", {})
    profile["evidence"]["resume"] = extracted_data
    generate_summary(profile)
    save_profile_data(student_id, profile)
    return profile


def parse_resume(student_id: str, uploaded_file) -> Dict[str, Any]:
    file_path = save_uploaded_file(student_id, uploaded_file, "resumes")

    from agents.resume_parser import ResumeParserAgent

    parser = ResumeParserAgent()
    extracted_data = parser.parse(str(file_path))
    updated_profile = merge_resume_data_into_profile(student_id, extracted_data)

    resume_row = ResumeUpload.objects.create(
        student=StudentProfile.objects.get(uuid=student_id),
        file_path=str(file_path),
        original_filename=uploaded_file.name,
        extracted_data=extracted_data,
    )

    return {
        "student_id": student_id,
        "resume_id": resume_row.id,
        "extracted_data": extracted_data,
        "profile": updated_profile,
    }


def analyze_github(student_id: str) -> Dict[str, Any]:
    """
    Analyzes the student's own OAuth-connected GitHub account only -- there
    is no github_url parameter anymore.
    """
    profile = load_profile_data(student_id)

    from accounts.github_oauth import (
        GitHubNotConnectedError,
        GitHubOAuthError,
        get_connection_for_student_id,
        get_valid_access_token,
    )
    from agents.github_agent import GitHubSkillsAgent

    connection = get_connection_for_student_id(student_id)
    if connection is None:
        raise GitHubNotConnectedError("Connect your GitHub account before running analysis.")

    github_url = f"https://github.com/{connection.github_username}"

    try:
        student_token = get_valid_access_token(connection)
    except GitHubOAuthError:
        # Their verified identity is still known even if the stored token
        # needs reconnecting -- fall back to the shared server token for
        # the actual API calls rather than blocking analysis entirely.
        student_token = None

    github_agent = GitHubSkillsAgent(token=student_token)

    if hasattr(github_agent, "analyse"):
        github_result = github_agent.analyse(github_url)
    else:
        github_result = github_agent.analyze(github_url)

    if isinstance(github_result, dict) and github_result.get("error"):
        raise ValueError(github_result.get("error"))

    profile["github"] = github_url
    profile["github_assessment"] = github_result

    existing_skills = list(profile.get("skills", []) or [])
    skills_added = []

    for item in github_result.get("languages", []) or []:
        skill = item.get("name") if isinstance(item, dict) else str(item)
        if skill and skill not in existing_skills:
            existing_skills.append(skill)
            skills_added.append(skill)

    for tool in github_result.get("frameworks_and_tools", []) or []:
        if tool and tool not in existing_skills:
            existing_skills.append(tool)
            skills_added.append(tool)

    profile["skills"] = existing_skills[:80]
    profile.setdefault("evidence", {})
    profile["evidence"]["github"] = {"github_url": github_url, "result": github_result}
    generate_summary(profile)
    save_profile_data(student_id, profile)

    GitHubAnalysis.objects.create(
        student=StudentProfile.objects.get(uuid=student_id),
        github_url=github_url,
        result=github_result,
    )

    return {
        "student_id": student_id,
        "github_username": connection.github_username,
        "github_result": github_result,
        "skills_added": skills_added,
        "profile": profile,
    }


def record_university_interest(student_id: str, university_id: str, source: str) -> None:

    from django_api.models import UniversityInterestEvent

    if not as_uuid(student_id):
        return
    profile = StudentProfile.objects.filter(uuid=student_id).first()
    if profile is None:
        return

    UniversityInterestEvent.objects.get_or_create(student=profile, university_id=university_id, source=source)


def student_has_university_interest(student_id: str, university_id: str) -> bool:
    """
    True if this student has an on-record interest signal for this university
    (an UniversityInterestEvent -- searched it, ran a fit check, etc.).

    This is the access boundary for the officer dashboard's per-student views
    (profile detail, profile-presenter chat): an officer may only inspect a
    student who has actually engaged with their university. 
    """
    from django_api.models import UniversityInterestEvent

    if not as_uuid(student_id) or not university_id:
        return False

    return UniversityInterestEvent.objects.filter(
        university_id=university_id, student__uuid=student_id
    ).exists()


DEFAULT_PRIORITY_TIER_BOUNDS = {"high": 80, "medium": 60, "low": 40}


def get_priority_tier_bounds(university_id: str) -> Dict[str, int]:
    """University's configured priority-tier bounds, per-key-merged over the
    defaults so a partially-set bounds dict (e.g. only {"high": 85}) still
    yields usable values for the other bands."""
    from universities.models import University

    university = University.objects.filter(uuid=university_id).first()
    configured = (university.priority_tier_bounds if university else None) or {}
    return {**DEFAULT_PRIORITY_TIER_BOUNDS, **configured}


def compute_priority_tier(match_score: int, bounds: Dict[str, int]) -> str:
    """Map a match_score to a priority band using the given bounds (see
    get_priority_tier_bounds). "unranked" covers interested students who
    don't clear even the "low" bound."""
    if match_score >= bounds["high"]:
        return "high"
    if match_score >= bounds["medium"]:
        return "medium"
    if match_score >= bounds["low"]:
        return "low"
    return "unranked"


def get_shortlisted_profiles(
    university_id: str,
    min_score: Optional[int] = None,
    priority_tiers: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Students who (a) have shown interest in this university (an
    UniversityInterestEvent row -- searched it or ran a fit check) and
    (b) whose latest FitAssessment.match_score meets min_score. min_score
    defaults to the university's configured min_fit_score_threshold, unless
    priority_tiers is given without an explicit min_score -- then it defaults
    to the lowest bound among the requested tiers, so e.g. filtering to
    priority_tiers=["low"] isn't silently blocked by a stricter default gate.
    Each result carries a "priority_tier" (high/medium/low/unranked, see
    compute_priority_tier) so callers can group/filter without recomputing
    bounds themselves. Sorted by match_score descending. Backs
    UniversityProfilesListView -- the officer-facing dashboard only ever sees
    this shortlist, never every StudentProfile row.
    """
    from django_api.models import FitAssessment, UniversityInterestEvent
    from universities.models import University

    bounds = get_priority_tier_bounds(university_id)

    if min_score is None:
        if priority_tiers:
            tier_floor = {**bounds, "unranked": 0}
            min_score = min(tier_floor.get(tier, 0) for tier in priority_tiers)
        else:
            university = University.objects.filter(uuid=university_id).first()
            min_score = university.min_fit_score_threshold if university else 40

    interested_student_pks = (
        UniversityInterestEvent.objects.filter(university_id=university_id)

        .order_by()
        .values_list("student_id", flat=True)
        .distinct()
    )

    shortlisted: List[Dict[str, Any]] = []

    for student_pk in interested_student_pks:
        assessment_row = (
            FitAssessment.objects.filter(student_id=student_pk, university_id=university_id)
            .order_by("-created_at")
            .first()
        )
        if assessment_row is None:
            continue

        try:
            match_score = int(assessment_row.assessment.get("match_score"))
        except (TypeError, ValueError):
            continue

        if match_score < min_score:
            continue

        tier = compute_priority_tier(match_score, bounds)
        if priority_tiers and tier not in priority_tiers:
            continue

        shortlisted.append({
            "student_id": str(assessment_row.student.uuid),
            "assessment": assessment_row.assessment,
            "match_score": match_score,
            "priority_tier": tier,
        })

    shortlisted.sort(key=lambda item: item["match_score"], reverse=True)
    return shortlisted


def get_priority_tier_counts(university_id: str) -> Dict[str, int]:
    """Tally of every interested-and-scored student for this university by
    priority tier, ignoring any score gate (min_score=0) -- lets the officer
    dashboard show tier filter buttons with counts regardless of which
    tier(s) are currently selected."""
    counts = {"high": 0, "medium": 0, "low": 0, "unranked": 0}
    for entry in get_shortlisted_profiles(university_id, min_score=0):
        counts[entry["priority_tier"]] += 1
    return counts


def analyze_linkedin(student_id: str, uploaded_images: List[Any]) -> Dict[str, Any]:
    image_paths = []
    for image in uploaded_images:
        saved_path = save_uploaded_file(student_id, image, "linkedin")
        image_paths.append(str(saved_path))

    from agents.linkedin_agent import LinkedInAgent

    linkedin_agent = LinkedInAgent()
    extracted = linkedin_agent.extract(image_paths)

    profile = load_profile_data(student_id)
    profile["linkedin_profile"] = extracted

    if not profile.get("name") and isinstance(extracted, dict) and extracted.get("name"):
        profile["name"] = extracted["name"]

    if not profile.get("country") and isinstance(extracted, dict) and extracted.get("location"):
        profile["country"] = extracted["location"]

    existing_skills = list(profile.get("skills", []) or [])
    skills_added = []

    if isinstance(extracted, dict):
        for skill in extracted.get("skills", []) or []:
            if skill and skill not in existing_skills:
                existing_skills.append(skill)
                skills_added.append(skill)

    profile["skills"] = existing_skills[:80]
    profile.setdefault("evidence", {})
    profile["evidence"]["linkedin"] = {"image_paths": image_paths, "result": extracted}
    generate_summary(profile)
    save_profile_data(student_id, profile)

    analysis = LinkedInAnalysis.objects.create(
        student=StudentProfile.objects.get(uuid=student_id),
        image_paths=image_paths,
        extracted=extracted,
    )

    return {
        "student_id": student_id,
        "analysis_id": analysis.id,
        "image_paths": image_paths,
        "extracted": extracted,
        "skills_added": skills_added,
        "profile": profile,
    }
