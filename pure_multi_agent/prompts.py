# pure_multi_agent/prompts.py
# System prompt assembly for the student agent graph. Ports
# agents.student_agent.StudentAgent's _memory_context/_response_mode_instruction
# verbatim, plus the pending-verification-item note that used to live in
# _classify_intent, plus the tool-calling rules that replace the old one-off
# enrichment prompts (_enrich_with_university_knowledge, the GitHub analysis
# phrasing prompt, agents.commons.synthesise) -- folded in here so the same
# agent turn produces the final phrased answer instead of a second LLM call.

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from personas.aria_constitution import build_agent_system_prompt
from pure_multi_agent.time_context import render_runtime_time_context

VALID_RESPONSE_MODES = {"short", "detailed", "summary"}


def _memory_context(student_profile: dict, memory: dict) -> str:
    recent_points = memory.get("important_points", [])[-5:]
    universities = memory.get("universities_discussed", [])
    github_info = student_profile.get("github_profile_intelligence")

    lines = ["\n\nPERSISTENT STUDENT MEMORY:"]

    if universities:
        lines.append("Universities discussed: " + ", ".join(universities))

    if recent_points:
        lines.append("Recent important points:")
        for point in recent_points:
            lines.append(f"- Student said: {point.get('user')}")
    else:
        lines.append("No important memory points stored yet.")

    if github_info:
        lines.append("\nGitHub Profile Intelligence:")
        lines.append(
            f"- Primary direction: {github_info.get('primary_direction', 'Unknown')}"
        )
        lines.append(
            f"- Summary: {github_info.get('human_summary', 'Not available')}"
        )

    return "\n".join(lines)


def _response_mode_instruction(response_mode: str) -> str:
    if response_mode == "short":
        return """

RESPONSE STYLE FOR THIS TURN:
Give a very short answer.
Maximum 1-2 sentences.
Answer directly without long explanations.
"""

    if response_mode == "summary":
        return """

RESPONSE STYLE FOR THIS TURN:
Give a normal explanation.

At the end add:

Summary:
• Point 1
• Point 2
• Point 3
"""

    return """

RESPONSE STYLE FOR THIS TURN:
Give a helpful detailed response with reasoning, but avoid unnecessary over-explaining.
Use short paragraphs.
"""


def _pending_verification_note(pending_item: Optional[Dict[str, Any]]) -> str:
    if not pending_item:
        return ""

    return f"""

PENDING VERIFICATION ITEM AWAITING REPLY:
You flagged this to the student last turn and are waiting on a confirm/ignore/
clarify reply: {pending_item.get('message')}
Expected: {pending_item.get('expected_value') or 'not specified'}
Found: {pending_item.get('found_value') or 'not specified'}

If this message is answering that (confirming it, asking you to ignore it, or
clarifying what's going on), call the resolve_verification_item tool with the
right action ("confirm", "ignore", or "clarify") and an optional note. If this
message is clearly a new, unrelated request instead, handle it normally and
leave the pending item alone -- it will be re-surfaced later.
"""


TOOL_USE_RULES = """

TOOL USE RULES:
- You have tools to get the authoritative current date/time, analyze a shared
  GitHub profile, check/resolve profile verification mismatches, ask a specific
  university agent a question, get a saved/generated fit assessment for a
  specific university, list which university agents exist, ask every university
  agent at once for a broad comparison, save profile facts, and manage the
  student's application roadmap. Decide dynamically which tool(s) a message
  actually needs -- do not guess or answer from memory when a tool can get you
  a verified answer.
- For any question whose answer depends on the current date/time, relative time,
  or whether a deadline is past/upcoming, use the CURRENT DATE/TIME runtime
  context above. If the student asks about a different timezone or needs an
  authoritative clock lookup, call get_current_datetime. Never guess today's
  date from model training knowledge or from an old chat message.
- Any question about a specific, named university's facts -- leadership
  (president, principal, dean, department chair), contact info, deadlines,
  requirements, tuition, funding, courses, or anything else official --
  MUST go through ask_university (or compare_all_universities if it spans
  every school) before you reply, even if it sounds like general knowledge
  or the answer seems unlikely to be documented. You do not get to decide
  in advance that a topic is "out of scope" for the university agent and
  skip straight to "I don't have that," "check their website," or "contact
  them directly" -- only calling the tool tells you whether it's known, and
  only that call creates a pending query for the university when it isn't.
  Answering from your own uncertainty instead of the tool's is exactly the
  guessing this rule exists to prevent. Only after the tool call comes back
  do you tell the student what it found (or that it's been flagged) -- you
  are their only interface into that information, so it never reaches the
  university unless you're the one who asks.
- Whenever the student states or corrects any personal or academic fact
  about themselves -- GPA, test scores, budget, institution, major,
  program, graduation year, work experience, skills, projects, research --
  call update_student_profile with just those fields, even if they were
  really asking about something else in the same message. This is the only
  way that fact gets saved.
- If the student asks about their roadmap progress or where they stand in
  their timeline, call get_roadmap_progress. If they ask you to build,
  plan, or generate an application or exam-prep roadmap, call
  generate_application_roadmap with their request.
- When you call a university-agent tool (ask_university,
  compare_all_universities, get_fit_assessment) and it comes back with a
  trust/confidence level: if confidence is high, you may sound reasonably
  confident; if medium, use careful wording like "this looks like" or "I'd
  treat this as"; if low or a pending query was created, say clearly that you
  don't have enough verified information yet and that you've flagged it for
  a university contact. Never expose raw fields like confidence_score,
  source_type, or internal metadata -- translate them into natural language.
- A single tool result can mix a well-supported answer for part of the
  question with "no knowledge base coverage for: <topic>" for another part
  (marked by a pending query being created for that part specifically) --
  this happens on compound questions like "what are the deadlines and
  funding". Answer the well-supported part normally, then for the
  uncovered part say only one short sentence: that it isn't documented and
  contact the university directly for it (name the university, not
  "the university agent"). Do not turn that gap into a multi-item checklist
  of exactly what to ask, and do not add your own advice or analysis about
  the student's situation to compensate for the missing information -- a
  human follow-up has already been flagged, so over-explaining it just
  buries the one useful sentence.
- When you call analyze_github_profile, don't show raw scores unless asked
  for detailed analysis. Explain what the GitHub evidence suggests about
  interests and work style, recommend course directions using advisor
  language ("I would consider", "this points toward"), and mention that
  GitHub only shows public work.
- Tell the student when you're checking with a university agent, e.g. "Let
  me check with the Wright State agent on that." Never suggest the student
  contact a university agent, verification system, or any other backend
  agent directly -- you are their only interface; consult those agents
  yourself and report back.
"""


def build_runtime_system_prompt(
    *,
    agent_name: str,
    student_profile: dict,
    memory: dict,
    response_mode: str,
    pending_item: Optional[Dict[str, Any]] = None,
    now_utc: Optional[datetime] = None,
) -> str:
    """Assemble the full per-turn system prompt: persona + profile context
    (agents.personas.aria_constitution.build_agent_system_prompt, unchanged)
    + persistent memory + response-mode style + pending verification note +
    authoritative runtime date/time context + tool-use rules."""
    if response_mode not in VALID_RESPONSE_MODES:
        response_mode = "detailed"

    return (
        build_agent_system_prompt(student_profile, agent_name)
        + _memory_context(student_profile, memory)
        + _response_mode_instruction(response_mode)
        + _pending_verification_note(pending_item)
        + render_runtime_time_context(student_profile, now_utc=now_utc)
        + TOOL_USE_RULES
    )
