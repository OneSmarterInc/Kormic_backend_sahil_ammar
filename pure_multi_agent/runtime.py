# pure_multi_agent/runtime.py
# Public entry point for the LangGraph student-agent chat flow:
# run_turn(student_id, message) -> (agent_name, reply).
# caching the context across turns let the agent answer from a
# snapshot that could be minutes or hours stale. Only the LangGraph
# `messages` state (conversation history) is intentionally kept
# out of _load_context()/_persist_context() -- it lives in the
# checkpointer below instead, since that's genuinely turn-to-turn
# conversational state with no other durable home.
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage
from rich.console import Console

from pure_multi_agent import preprocessing, prompts
from pure_multi_agent.student_graph import build_student_agent
from pure_multi_agent.tracing import VERBOSE, GraphTraceLogger

console = Console()
logger = logging.getLogger(__name__)


def _build_checkpointer():
    """
    Shared checkpointer so conversation history (the `messages` state)
    persists across per-turn graph rebuilds, keyed by thread_id=student key.

    Backed by the same Postgres database Django already uses, via a
    per-process connection pool (langgraph-checkpoint-postgres) -- not
    LangGraph's in-memory MemorySaver. MemorySaver keeps state only in the
    worker process that first handled a student's message: with more than
    one gunicorn worker (the normal deployment shape, see GUNICORN_WORKERS)
    a student's next message can land on a different worker and the agent
    silently "forgets" mid-conversation, state is lost on every
    restart/deploy, and the in-process dict never evicts so memory grows for
    the life of the process. A durable, shared backend fixes all three.
    """
    from django.conf import settings
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from langgraph.checkpoint.postgres import PostgresSaver

    db = settings.DATABASES["default"]
    conninfo = (
        f"dbname={db['NAME']} user={db['USER']} password={db['PASSWORD']} "
        f"host={db['HOST']} port={db['PORT']}"
    )
    pool = ConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=int(os.environ.get("AGENT_CHECKPOINTER_POOL_SIZE", "5")),
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    saver = PostgresSaver(pool)
    try:
        # Idempotent (CREATE TABLE IF NOT EXISTS + a migrations-version
        # table) -- safe to call from every worker process on startup. Only
        # swallow failures here rather than crashing Django's boot: a
        # transient DB hiccup at import time shouldn't take the whole
        # process down when every other Django subsystem already tolerates
        # the DB being briefly unreachable at startup.
        saver.setup()
    except Exception:
        logger.exception("Agent checkpointer setup() failed -- will retry lazily on first use.")
    return saver


_checkpointer = _build_checkpointer()


def _load_context(student_id: str) -> Dict[str, Any]:
    """Load this student's full turn context fresh from the database. Called
    at the start of every turn -- never cached across turns -- so any
    profile/resume/GitHub/LinkedIn update made through any other endpoint,
    or any agent rename, is always visible on the very next message."""
    from agents.agent_identity import ensure_agent_name
    from django_api.models import AriaMemory, StudentProfile
    from django_api.services import load_profile_data
    from verification.services import list_items

    key = student_id

    profile_row, _ = StudentProfile.objects.get_or_create(uuid=key)
    agent_name = ensure_agent_name(profile_row)

    student_profile = load_profile_data(student_id)

    memory_row, _ = AriaMemory.objects.get_or_create(student_id=key)
    memory = {
        "important_points": list(memory_row.important_points or []),
        "universities_discussed": list(memory_row.universities_discussed or []),
        "github_profiles_analyzed": list(memory_row.github_profiles_analyzed or []),
    }

    response_mode = student_profile.get("response_mode", "detailed")
    if response_mode not in prompts.VALID_RESPONSE_MODES:
        response_mode = "detailed"

    # The durable source of truth for "is there an open verification item
    # this student hasn't responded to yet" is the VerificationItem table
    # itself, not anything held in memory -- re-derive it every turn instead
    # of threading a flag through a long-lived context object.
    open_items = list_items(key, "open").get("items", [])
    pending_item = open_items[0] if open_items else None

    return {
        "canonical_student_id": key,
        "student_name": student_profile.get("name") or "there",
        "agent_name": agent_name,
        "student_profile": student_profile,
        "memory": memory,
        "response_mode": response_mode,
        "pending_verification_item_id": pending_item["id"] if pending_item else None,
        "pending_verification_item": pending_item,
    }


def _persist_context(student_id: str, ctx: Dict[str, Any]) -> None:
    from django_api.models import AriaMemory
    from django_api.services import save_profile_data

    key = student_id

    ctx["student_profile"]["response_mode"] = ctx["response_mode"]
    save_profile_data(student_id, ctx["student_profile"])

    AriaMemory.objects.update_or_create(
        student_id=key,
        defaults={
            "important_points": ctx["memory"].get("important_points", [])[-50:],
            "universities_discussed": ctx["memory"].get("universities_discussed", []),
            "github_profiles_analyzed": ctx["memory"].get("github_profiles_analyzed", []),
        },
    )


def _extract_reply_text(result: Dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if not messages:
        return "I hit an error while generating a response. Please try again."

    content = messages[-1].content
    if isinstance(content, str):
        return content

    # Anthropic content blocks can come back as a list of dicts/blocks.
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "".join(parts)

    return str(content)


def reset_conversation(student_id: str) -> None:
    """
    Wipe this student's LangGraph conversational state (the `messages`
    checkpoint), so the next run_turn starts with no prior turns
    in context -- i.e. a genuine "new chat", not just a cleared-looking
    transcript that still secretly informs the next reply. Callers also need
    to delete the student's persisted ChatMessage rows (the visible
    history) separately; this only clears the short-term turn-to-turn state
    kept here in _checkpointer.
    """
    key = student_id
    _checkpointer.delete_thread(key)


def seed_conversation(student_id: str, turns: List[Tuple[str, str]]) -> None:
    """
    Reset this student's LangGraph thread and, if `turns` is
    non-empty, pre-load it with a known-good prefix of prior turns
    (oldest-first (sender, content) pairs, sender being "user"/"assistant")
    with no model call involved. Used by the chat "edit message" flow: after
    a message is edited, everything after it in the transcript is discarded
    and the LangGraph thread must be rebuilt to match, so the regenerated
    reply is grounded in the same context the model had right before the
    edit -- not the stale, now-invalid conversation state left over from
    before the edit.
    """
    reset_conversation(student_id)
    if not turns:
        return

    ctx = _load_context(student_id)
    system_prompt = prompts.build_runtime_system_prompt(
        agent_name=ctx["agent_name"],
        student_profile=ctx["student_profile"],
        memory=ctx["memory"],
        response_mode=ctx["response_mode"],
        pending_item=ctx.get("pending_verification_item"),
    )
    agent = build_student_agent(ctx, system_prompt, _checkpointer)

    messages = [
        HumanMessage(content=content) if sender == "user" else AIMessage(content=content)
        for sender, content in turns
    ]
    agent.update_state(
        {"configurable": {"thread_id": ctx["canonical_student_id"]}},
        {"messages": messages},
    )


def run_turn(
    student_id: str, message: str, image_blocks: Optional[List[Dict[str, Any]]] = None
) -> tuple[str, str]:
    ctx = _load_context(student_id)

    if VERBOSE:
        console.print(
            f"\n[bold magenta]=== pure_multi_agent turn: student={ctx['canonical_student_id']} "
            f"agent={ctx['agent_name']} ===[/bold magenta]"
        )
        console.print(f"[dim]student says:[/dim] {message}")

    system_prompt = prompts.build_runtime_system_prompt(
        agent_name=ctx["agent_name"],
        student_profile=ctx["student_profile"],
        memory=ctx["memory"],
        response_mode=ctx["response_mode"],
        pending_item=ctx.get("pending_verification_item"),
    )

    agent = build_student_agent(ctx, system_prompt, _checkpointer)
    tracer = GraphTraceLogger(label=ctx["canonical_student_id"])

   
    human_content: Any = message
    if image_blocks:
        human_content = [{"type": "text", "text": message}, *image_blocks]

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=human_content)]},
            config={
                "configurable": {"thread_id": ctx["canonical_student_id"]},
                "recursion_limit": 25,
                "callbacks": [tracer],
            },
        )
        reply = _extract_reply_text(result)
    except Exception as exc:
        logger.exception("Agent turn failed for student %s", ctx["canonical_student_id"])
        console.print(f"[yellow]Agent turn failed: {exc}[/yellow]")
        # Ops-facing detail (which env var, which upstream, etc.) is for the
        # alert email only -- a student seeing "check your ANTHROPIC_API_KEY"
        # would be confusing at best and a config-detail leak at worst.
        try:
            from pure_multi_agent.tasks import send_agent_error_alert_task

            send_agent_error_alert_task.delay(str(exc), ctx["canonical_student_id"])
        except Exception:
            logger.exception("Failed to queue agent-error alert task")
        reply = (
            "I hit an error while generating the response, likely a token limit "
            "issue on our side. Please try again in a little while."
        )

    preprocessing.update_memory(ctx, message, reply)
    _persist_context(student_id, ctx)

    if VERBOSE:
        console.print(f"[bold magenta]=== turn complete ({tracer._step} model call(s)) ===[/bold magenta]\n")

    return ctx["agent_name"], reply
