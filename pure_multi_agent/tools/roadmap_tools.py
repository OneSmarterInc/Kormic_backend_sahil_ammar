# pure_multi_agent/tools/roadmap_tools.py
# Roadmap progress/generation, exposed as tools instead of the old
# unconditional pre-graph shortcut (roadmap_shortcut in the previous
# preprocessing.py) that intercepted the message before the agent graph
# ever ran, based on a keyword/state check. The model now decides when a
# message is actually about roadmap progress or roadmap generation.
#
# RoadmapPlanner is an optional dependency (roadmap/roadmap_planner.py) --
# import failures are handled the same way they always were here (the
# module has never shipped in this repo), not something introduced by this
# refactor.

from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.tools import tool

try:
    from roadmap.roadmap_planner import RoadmapPlanner
except Exception:
    RoadmapPlanner = None

_planner = RoadmapPlanner() if RoadmapPlanner is not None else None


def build_tools(ctx: Dict[str, Any]) -> List[Any]:
    @tool
    def get_roadmap_progress() -> str:
        """Report the student's current month/status on their saved
        application or exam-prep roadmap. Use this when the student asks
        about their roadmap progress or where they are in their timeline.
        If no roadmap has been generated yet, says so."""
        roadmap = ctx["student_profile"].get("roadmap")
        if not roadmap:
            return (
                "No roadmap has been generated for this student yet. Offer to "
                "generate one with generate_application_roadmap if that would help."
            )

        return (
            f"Currently on Month {roadmap.get('current_month', 'N/A')} of the "
            f"{str(roadmap.get('exam', 'application')).upper()} roadmap.\n"
            f"Status: {roadmap.get('status', 'Not available')}"
        )

    @tool
    def generate_application_roadmap(request: str) -> str:
        """Generate a month-by-month application or exam-prep roadmap for the
        student, tailored to their saved profile and the given request (e.g.
        target exam, timeline, or goal they described). Use this when the
        student asks you to build/plan/generate a roadmap or study/application
        timeline."""
        if _planner is None:
            return (
                "Roadmap generation isn't available in this environment right "
                "now -- let the student know their profile and goals are still "
                "saved and this can be generated later."
            )

        roadmap = _planner.generate_application_roadmap(ctx["student_profile"], request)

        if isinstance(roadmap, dict):
            ctx["student_profile"]["roadmap"] = roadmap
            return json.dumps(roadmap, indent=2, ensure_ascii=False)

        return str(roadmap)

    return [get_roadmap_progress, generate_application_roadmap]
