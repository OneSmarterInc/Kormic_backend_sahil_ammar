# pure_multi_agent/tools
# Thin LangChain @tool wrappers around the existing, unmodified analysis
# services. Each build_tools(ctx) factory closes over one turn's mutable
# context dict (student_profile/memory/pending_verification_item_id) so
# tools can read/update it directly and the runtime can persist it to
# Django afterward -- no LangGraph Command/InjectedState machinery needed.

from __future__ import annotations

from typing import Any, Dict, List

from . import (
    github_tools,
    profile_tools,
    roadmap_tools,
    time_tools,
    university_tools,
    verification_tools,
)


def build_all_tools(ctx: Dict[str, Any]) -> List[Any]:
    return [
        *time_tools.build_tools(ctx),
        *profile_tools.build_tools(ctx),
        *github_tools.build_tools(ctx),
        *verification_tools.build_tools(ctx),
        *university_tools.build_tools(ctx),
        *roadmap_tools.build_tools(ctx),
    ]
