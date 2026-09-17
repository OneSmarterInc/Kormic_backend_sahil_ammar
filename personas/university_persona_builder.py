# personas/university_persona_builder.py
# Assembles a university agent's system-prompt "constitution" dynamically
# from a University row's structured fields, mirroring how
# personas/aria_constitution.py's AGENT_CONSTITUTION_TEMPLATE.format(...)
# builds the student agent's persona from a name + profile instead of a
# hand-written file per student.
#
# Admin input only ever fills named template slots below -- it never
# authors or overrides the fixed safety/behavioral rules (output format,
# the default answer shape, and the "never invent numbers" guardrails).
# Replaces the two hand-written per-university constitutions that used to
# live in personas/university_personas.py.

from __future__ import annotations

from typing import Iterable, List, Optional


FIXED_COMMUNICATION_RULES = """- Direct and factual.
- Use plain terminal-friendly text.
- Do not use Markdown headings like ## or ###.
- Do not use bold markers like **text**.
- Do not use long divider lines, tables, or copy-pasted report formatting.
- Prefer short paragraphs and simple numbered points like 1), 2), 3).
- When you do not know something, say so clearly."""

DEFAULT_ANSWER_SHAPE = """Open with one natural sentence.
Then give a compact answer in this format when useful:

Quick picture:
1) Practical point
2) Practical point
3) Practical point

Fit for the student:
Write 2-4 honest sentences tied to the student's profile.

Bottom line:
Give a clear recommendation in one or two sentences."""

WHAT_YOU_KNOW_TEMPLATE = """You will be given a knowledge base of facts about {program_name} -- seed
facts, scraped official pages, conversation facts, and human-verified or
admin-entered answers. Always use the knowledge base first. If the answer
is not there, say you are not certain and recommend checking the official
{program_name} page or admissions contact."""

UNIVERSAL_NEVER_DO_RULES = """- Overstate {program_name}'s ranking or prestige.
- Invent acceptance rates, salary outcomes, exact deadlines, tuition, or research statistics.
- Promise admission, funding, internships, visas, CPT/OPT, or jobs.
- Treat scraped or inferred information as official if it needs verification.
- Pretend {program_name} is the right fit for every student."""


def _identity_paragraph(*, program_name: str, location: str, tagline: str, description: str) -> str:
    lines: List[str] = [f"You represent {program_name}."]

    if location:
        lines.append(f"You are based in {location}.")

    if tagline:
        lines.append(tagline.strip())

    if description:
        lines.append(description.strip())

    lines.append(
        "You know your program through seed facts, official scraped pages, "
        "admin-entered facts, and human-verified answers stored in the "
        "Kormic Commons. You are honest about what you know and clear about "
        "what you are uncertain about."
    )

    return " ".join(lines)


def _personality_block(
    *,
    tone_descriptors: Optional[Iterable[str]],
    best_fit_notes: str,
    not_best_fit_notes: str,
) -> str:
    paragraphs: List[str] = []

    descriptors = [str(item).strip() for item in (tone_descriptors or []) if str(item).strip()]
    if descriptors:
        traits = ", ".join(descriptors)
        paragraphs.append(
            f"Your tone is {traits}. You do not oversell your program, and you "
            "are honest about tradeoffs a student should weigh."
        )
    else:
        paragraphs.append(
            "You are honest and grounded. You do not oversell your program, "
            "and you are clear about tradeoffs a student should weigh."
        )

    if best_fit_notes:
        paragraphs.append(f"Best-fit profile: {best_fit_notes.strip()}")

    if not_best_fit_notes:
        paragraphs.append(f"Not the best fit: {not_best_fit_notes.strip()}")

    return "\n\n".join(paragraphs)


def _communication_block(communication_style_notes: str) -> str:
    if not communication_style_notes:
        return FIXED_COMMUNICATION_RULES

    return FIXED_COMMUNICATION_RULES + "\n\nAdditional notes for this program:\n" + communication_style_notes.strip()


def _never_do_block(*, program_name: str, never_do_notes: str) -> str:
    block = UNIVERSAL_NEVER_DO_RULES.format(program_name=program_name)

    if never_do_notes:
        block += "\n\nAdditional notes for this program:\n" + never_do_notes.strip()

    return block


def build_constitution(
    *,
    agent_name: str,
    program_name: str,
    location: str = "",
    tagline: str = "",
    description: str = "",
    tone_descriptors: Optional[Iterable[str]] = None,
    best_fit_notes: str = "",
    not_best_fit_notes: str = "",
    communication_style_notes: str = "",
    never_do_notes: str = "",
) -> str:
    """Build a full system-prompt constitution for a university agent from
    structured fields. Mirrors the shape/behavioral bar of the old
    hand-written WRIGHT_STATE_CONSTITUTION/FRANKLIN_CS_CONSTITUTION, but
    every school-specific string is interpolated into fixed scaffolding --
    the safety rules (never invent tuition/deadlines/visa outcomes, honesty
    rule, output format, default answer shape) are never admin-editable."""
    identity = _identity_paragraph(
        program_name=program_name, location=location, tagline=tagline, description=description
    )
    personality = _personality_block(
        tone_descriptors=tone_descriptors,
        best_fit_notes=best_fit_notes,
        not_best_fit_notes=not_best_fit_notes,
    )
    communication = _communication_block(communication_style_notes)
    what_you_know = WHAT_YOU_KNOW_TEMPLATE.format(program_name=program_name)
    never_do = _never_do_block(program_name=program_name, never_do_notes=never_do_notes)

    return f"""
You are {agent_name}, the {program_name} agent living in the Kormic Commons.

YOUR IDENTITY:
{identity}

YOUR PERSONALITY:
{personality}

YOUR COMMUNICATION STYLE:
{communication}

DEFAULT ANSWER SHAPE:
{DEFAULT_ANSWER_SHAPE}

WHAT YOU KNOW ABOUT YOUR PROGRAM:
{what_you_know}

WHAT YOU WILL NEVER DO:
{never_do}
"""
