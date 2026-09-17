# pure_multi_agent/tools/profile_tools.py

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from langchain_core.tools import tool


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in [None, ""]:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in [None, ""]:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _profile_overview(student_profile: dict) -> str:
    academic = student_profile.get("academic_intelligence", {}) or {}
    technical = student_profile.get("technical_intelligence", {}) or {}
    overall = student_profile.get("overall_profile", {}) or {}

    return f"""
### Student Profile

| Field | Value |
|---|---|
| Name | {student_profile.get("name", "Not provided")} |
| Program | {student_profile.get("program", "Not provided")} |
| Institution | {student_profile.get("institution", "Not provided")} |
| Major | {student_profile.get("major", "Not provided")} |
| GPA | {student_profile.get("gpa", "Not provided")} |
| GRE Quant | {student_profile.get("gre_quant", "Not provided")} |
| TOEFL | {student_profile.get("toefl", "Not provided")} |
| Budget | ${student_profile.get("budget", "Not provided")} |

**Research / Notes**

{student_profile.get("research", "Not provided")}

**Academic Score:** {academic.get("academic_score", "Not available")}
**Readiness:** {academic.get("readiness", "Not available")}
**Technical Score:** {technical.get("technical_score", "Not available")}
**Technical Level:** {technical.get("technical_level", "Not available")}
**Overall Score:** {overall.get("overall_score", student_profile.get("overall_profile_score", "Not available"))}
"""


def _profile_academic(student_profile: dict) -> str:
    academic = student_profile.get("academic_intelligence", {}) or {}
    strengths = "\n".join(f"• {item}" for item in academic.get("strengths", []) or [])
    weaknesses = "\n".join(f"• {item}" for item in academic.get("weaknesses", []) or [])

    return f"""
### Academic Profile

**Academic Score:** {academic.get("academic_score", "Not available")}/100
**Readiness:** {academic.get("readiness", "Not available")}

**Strengths**
{strengths or "• No academic strengths stored yet."}

**Weaknesses / Gaps**
{weaknesses or "• No academic weaknesses stored yet."}
"""


def _profile_technical(student_profile: dict) -> str:
    technical = student_profile.get("technical_intelligence", {}) or {}
    strengths = "\n".join(f"• {item}" for item in technical.get("strengths", []) or [])
    weaknesses = "\n".join(f"• {item}" for item in technical.get("weaknesses", []) or [])

    return f"""
### Technical Profile

**Technical Score:** {technical.get("technical_score", "Not available")}/100
**Technical Level:** {technical.get("technical_level", "Not available")}

**Strengths**
{strengths or "• No technical strengths stored yet."}

**Weaknesses / Gaps**
{weaknesses or "• No technical weaknesses stored yet."}
"""


def _profile_overall(student_profile: dict) -> str:
    overall = student_profile.get("overall_profile", {}) or {}
    return f"""
### Overall Profile

**Overall Score:** {overall.get("overall_score", student_profile.get("overall_profile_score", "Not available"))}/100
"""


def admission_probability(student_profile: dict) -> str:
    gpa = _safe_float(student_profile.get("gpa"), 0.0)
    gre = _safe_int(student_profile.get("gre_quant"), 0)
    budget = _safe_int(student_profile.get("budget"), 0)

    if gpa >= 3.7 and gre >= 167:
        cmu = "Reach"
    elif gpa >= 3.3 and gre >= 160:
        cmu = "High Reach"
    else:
        cmu = "Very High Reach"

    if gpa >= 3.2 and gre >= 158:
        wright = "Realistic"
    elif gpa >= 3.0:
        wright = "Moderate"
    else:
        wright = "Reach"

    if gpa >= 3.35 and gre >= 160:
        msu = "Moderate"
    else:
        msu = "Reach"

    if gpa >= 3.7 and gre >= 165:
        uw = "Reach"
    else:
        uw = "High Reach"

    # Franklin is evaluated differently because it is a flexible, career-focused MSCS pathway,
    # not a prestige/research-driven target in the same category as CMU/UW.
    if gpa >= 3.0:
        franklin = "Realistic / Practical Fit"
    elif gpa >= 2.7:
        franklin = "Possible, verify requirements"
    else:
        franklin = "Needs review"

    cmu_note = (
        "Cost is a major issue for CMU based on the current budget."
        if budget and budget < 40000
        else "Budget should still be reviewed carefully for CMU."
    )

    github_info = student_profile.get("github_profile_intelligence", {})
    github_note = ""
    if github_info:
        github_note = (
            f"\n**Profile signal:** GitHub analysis currently points toward "
            f"**{github_info.get('primary_direction', 'Unknown')}**, which should be used "
            f"to choose better-fit tracks and electives."
        )

    return f"""
### Admission Fit Estimate

| University | Fit / Chance Category |
|---|---|
| CMU | {cmu} |
| Wright State | {wright} |
| Franklin University | {franklin} |
| Michigan State | {msu} |
| University of Washington | {uw} |

**Note:** {cmu_note}
{github_note}

This is a rough advising estimate, not an official admission prediction. Exact admission chances require verified historical admission data and current program-specific criteria.
"""


def university_comparison(student_profile: dict) -> str:
    github_info = student_profile.get("github_profile_intelligence", {})
    github_direction = github_info.get("primary_direction")

    github_row = ""
    if github_direction:
        github_row = (
            f"| Student GitHub Fit | Good if applied {github_direction} work connects to AFRL/research areas | "
            f"Good if {github_direction} connects to software systems, data analytics, or cybersecurity |\n"
        )

    return f"""
### Wright State vs Franklin University

| Factor | Wright State | Franklin University |
|---|---|---|
| Core identity | Traditional public university CS/CSE option | Flexible, online, career-focused MSCS pathway |
| Best fit | Students wanting campus-based university experience, applied research, AFRL/Dayton connection | Students wanting practical software skills, online flexibility, working-adult format, or non-CS pathway |
| Research orientation | Stronger fit for applied research and regional lab/defense ecosystem | Better framed as professional/career advancement rather than research-heavy CS |
| Flexibility | Traditional university structure | 100% online program family with focus areas |
| Focus areas | CS/CSE, AI, cybersecurity, systems, HCI, applied research | Cybersecurity, Data Analytics, Software Systems, general MSCS |
| Budget lens | Public university value and possible assistantships, verify details | Per-credit tuition model; verify latest cost and partner discounts |
| Prestige lens | Not top-10 but more traditional university signal | Not a prestige/research play; practical career pathway |
{github_row}

**Simple advice:** Wright State is better if the student wants a traditional university environment, applied research, and regional opportunities around Dayton/AFRL. Franklin is better if the student values flexibility, online study, practical software/system skills, or a bridge into CS from a non-traditional background.
"""


def build_tools(ctx: Dict[str, Any]) -> List[Any]:
    @tool
    def show_student_profile(section: Literal["overview", "academic", "technical", "overall"]) -> str:
        """Show the student's own saved profile. Use section="overview" for
        the general profile table (program, GPA, GRE, budget, etc), "academic"
        for academic strengths/weaknesses, "technical" for technical
        strengths/weaknesses, or "overall" for the overall profile score."""
        student_profile = ctx["student_profile"]
        if section == "academic":
            return _profile_academic(student_profile)
        if section == "technical":
            return _profile_technical(student_profile)
        if section == "overall":
            return _profile_overall(student_profile)
        return _profile_overview(student_profile)

    @tool
    def admission_probability_estimate() -> str:
        """Get a rough, qualitative admission-fit estimate (reach/target/
        realistic) across CMU, Wright State, Franklin, Michigan State, and
        University of Washington, based on the student's stored GPA/GRE/budget.
        Not tied to a saved fit assessment for a specific university."""
        return admission_probability(ctx["student_profile"])

    @tool
    def university_comparison_table() -> str:
        """Get a generic Wright State vs Franklin University comparison table
        (identity, research orientation, flexibility, budget, prestige)."""
        return university_comparison(ctx["student_profile"])

    @tool
    def update_student_profile(
        name: Optional[str] = None,
        institution: Optional[str] = None,
        major: Optional[str] = None,
        program: Optional[str] = None,
        gpa: Optional[float] = None,
        gpa_scale: Optional[float] = None,
        gre_quant: Optional[int] = None,
        gre_verbal: Optional[int] = None,
        toefl: Optional[float] = None,
        ielts: Optional[float] = None,
        budget: Optional[float] = None,
        graduation_year: Optional[int] = None,
        work_months: Optional[int] = None,
        research: Optional[str] = None,
        skills: Optional[List[str]] = None,
        projects: Optional[List[str]] = None,
    ) -> str:
        """Save profile facts the student just stated or corrected in chat --
        GPA, test scores (GRE/TOEFL/IELTS), budget, institution, major,
        program, graduation year, work experience, skills, projects, or
        research interests. Call this immediately whenever such a fact
        appears in the student's message, even if they were really asking
        about something else -- this is the only way that fact gets saved;
        if you don't call it, it's lost after this turn. Only pass the
        fields actually stated; leave everything else unset."""
        updates = {
            "name": name,
            "institution": institution,
            "major": major,
            "program": program,
            "gpa": gpa,
            "gpa_scale": gpa_scale,
            "gre_quant": gre_quant,
            "gre_verbal": gre_verbal,
            "toefl": toefl,
            "ielts": ielts,
            "budget": budget,
            "graduation_year": graduation_year,
            "work_months": work_months,
            "research": research,
            "skills": skills,
            "projects": projects,
        }
        cleaned = {key: value for key, value in updates.items() if value is not None}

        if not cleaned:
            return "No profile fields were provided to update."

        ctx["student_profile"].update(cleaned)
        return f"Profile updated: {', '.join(cleaned.keys())}."

    return [
        show_student_profile,
        admission_probability_estimate,
        university_comparison_table,
        update_student_profile,
    ]
