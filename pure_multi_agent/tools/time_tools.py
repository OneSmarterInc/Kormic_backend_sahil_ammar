from __future__ import annotations

import json
from typing import Any, Dict, List

from langchain_core.tools import tool

from pure_multi_agent.time_context import current_time_payload


def build_tools(ctx: Dict[str, Any]) -> List[Any]:
    @tool
    def get_current_datetime(timezone_name: str = "") -> str:
        """Return the authoritative current date and time.

        Use this for questions about the current date/time, relative dates,
        deadline timing, or when the student asks about a timezone different
        from their saved/default timezone. timezone_name may be an IANA name
        such as 'Asia/Kolkata' or 'America/New_York'; leave it blank to use the
        student's saved timezone or Kormic's configured default.
        """
        payload = current_time_payload(
            ctx.get("student_profile", {}),
            timezone_name=timezone_name or None,
        )
        return json.dumps(payload, ensure_ascii=False)

    return [get_current_datetime]
