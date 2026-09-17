# pure_multi_agent/tools/github_tools.py
# Wraps profile_intelligence.profile_intelligence.ProfileIntelligenceService
# unchanged -- the same GitHub course-recommendation analyzer used by the old
# StudentAgent._handle_github_profile_analysis. The old code made a second,
# dedicated Claude call here just to phrase the result in Aria's voice; that
# phrasing now happens in the same agent turn that called this tool (rules
# folded into pure_multi_agent.prompts.TOOL_USE_RULES), so this tool just
# returns the structured findings.

from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.tools import tool
from rich.console import Console

try:
    from profile_intelligence.profile_intelligence import ProfileIntelligenceService
except Exception:
    ProfileIntelligenceService = None

console = Console()

_service = ProfileIntelligenceService() if ProfileIntelligenceService is not None else None


def build_tools(ctx: Dict[str, Any]) -> List[Any]:
    @tool
    def analyze_github_profile(github_input: str) -> str:
        """Analyze a student's public GitHub profile (URL or username) to infer
        their technical interests and recommend course directions. Call this
        whenever the student shares a GitHub link/username or asks you to look
        at their GitHub."""
        if _service is None:
            return (
                "The profile_intelligence module is not available in this "
                "environment (missing github_analyzer.py/course_mapper.py/"
                "profile_intelligence.py)."
            )

        student_profile = ctx["student_profile"]
        memory = ctx["memory"]

        try:
            analysis = _service.analyze_github(
                github_input,
                student_name=ctx.get("student_name", "student"),
            )
        except Exception as exc:
            console.print(f"[yellow]GitHub analysis failed: {exc}[/yellow]")
            return (
                "I tried checking that GitHub profile, but couldn't analyze it "
                "properly. Confirm the username/link is correct, public, and "
                "reachable, or ask the student to paste a short project summary "
                "instead."
            )

        course_recommendation = analysis.get("course_recommendation", {})
        github_analysis = analysis.get("github_analysis", {})

        student_profile["github_profile"] = github_input
        student_profile["github_profile_intelligence"] = {
            "generated_at": analysis.get("generated_at"),
            "human_summary": analysis.get("human_summary"),
            "primary_direction": course_recommendation.get("primary_direction"),
            "recommendations": course_recommendation.get("recommendations", []),
        }

        if github_input not in memory["github_profiles_analyzed"]:
            memory["github_profiles_analyzed"].append(github_input)

        top_languages = github_analysis.get("top_languages", [])[:5]
        top_keywords = github_analysis.get("top_keywords", [])[:15]
        inferred_interests = (
            github_analysis.get("inferred_interests", {}).get("ranked_interests", [])[:5]
        )

        return (
            f"GITHUB HUMAN SUMMARY:\n{analysis.get('human_summary')}\n\n"
            f"TOP VISIBLE LANGUAGES:\n{top_languages}\n\n"
            f"TOP TECHNICAL KEYWORDS:\n{top_keywords}\n\n"
            f"INFERRED INTEREST AREAS:\n{inferred_interests}\n\n"
            f"COURSE RECOMMENDATION:\n"
            f"Primary direction: {course_recommendation.get('primary_direction')}\n"
            f"Recommended course areas: {course_recommendation.get('recommendations')}"
        )

    return [analyze_github_profile]
