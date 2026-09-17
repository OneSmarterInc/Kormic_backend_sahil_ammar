# pure_multi_agent/tools/verification_tools.py
# Wraps agents.commons.run_verification/resolve_verification_item (->
# verification.services, unchanged) as tools the agent calls dynamically,
# replacing the old fixed "verification_check"/"verification_reply" intent
# branches in StudentAgent.chat().

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from langchain_core.tools import tool
from rich.console import Console

from agents import commons

console = Console()


def build_tools(ctx: Dict[str, Any]) -> List[Any]:
    @tool
    def check_profile_verification() -> str:
        """Run a fresh check for mismatches between the student's profile,
        resume, GitHub, and LinkedIn data, and surface the first open item
        (if any) for the student to confirm/ignore/clarify. Call this when
        the student asks you to review/check/verify their profile."""
        try:
            result = commons.run_verification(ctx["canonical_student_id"])
        except Exception as exc:
            console.print(f"[yellow]Verification check failed: {exc}[/yellow]")
            return "The verification check failed unexpectedly. Try again shortly."

        open_items = [item for item in result.get("items", []) if not item.get("is_resolved")]

        if not open_items:
            ctx["pending_verification_item_id"] = None
            ctx["pending_verification_item"] = None
            return (
                "Checked the student's profile across resume, GitHub, and LinkedIn -- "
                "everything lines up, no mismatches to review right now."
            )

        item = open_items[0]
        ctx["pending_verification_item_id"] = item["id"]
        ctx["pending_verification_item"] = item

        return (
            f"Found something worth a second look: {item.get('message')}\n"
            f"Expected: {item.get('expected_value') or 'not specified'}\n"
            f"Found: {item.get('found_value') or 'not specified'}\n\n"
            "Ask the student if this is correct, should be ignored, or if they'd "
            "like to clarify what's going on."
        )

    @tool
    def resolve_verification_item(action: Literal["confirm", "ignore", "clarify"], note: str = "") -> str:
        """Record the student's decision about the currently pending
        verification item (a flagged profile mismatch you already surfaced).
        Only call this when there is a pending verification item and the
        student's message is answering it."""
        item_id = ctx.get("pending_verification_item_id")

        if item_id is None:
            return "There is no pending verification item to resolve right now."

        try:
            result = commons.resolve_verification_item(
                student_id=ctx["canonical_student_id"],
                item_id=item_id,
                action=action,
                note=note,
            )
        except Exception as exc:
            console.print(f"[yellow]Could not resolve verification item {item_id}: {exc}[/yellow]")
            ctx["pending_verification_item_id"] = None
            ctx["pending_verification_item"] = None
            return "Could not resolve that verification item due to an unexpected error."

        ctx["pending_verification_item_id"] = None
        ctx["pending_verification_item"] = None

        open_items = [item for item in result["check"].get("items", []) if not item.get("is_resolved")]

        if open_items:
            next_item = open_items[0]
            ctx["pending_verification_item_id"] = next_item["id"]
            ctx["pending_verification_item"] = next_item
            return (
                "Recorded. One more thing worth checking: "
                f"{next_item.get('message')}\n"
                f"Expected: {next_item.get('expected_value') or 'not specified'}\n"
                f"Found: {next_item.get('found_value') or 'not specified'}\n\n"
                "Ask the student if this is correct, should be ignored, or if "
                "they'd like to clarify."
            )

        return "Recorded -- that covers everything. The profile is fully reviewed for now."

    return [check_profile_verification, resolve_verification_item]
