# agents/university_agent.py
# The university agent for the Kormic Commons.
# Each instance represents one university's graduate program.
# Handles verified knowledge, website scraping, pending queries, and fit assessment.

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import anthropic
from rich.console import Console

from knowledge.scraper import scrape_university
from knowledge.university_kb import UniversityKnowledgeBase

console = Console()

MODEL = "claude-haiku-4-5-20251001"


# UniversityAgent.answer() is called synchronously from the student's chat
# turn (via agents.commons) -- bounding this call keeps a hung upstream
# request from holding the chat worker indefinitely, same reasoning as
# pure_multi_agent.student_graph.CHAT_MODEL_TIMEOUT_SECONDS.
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120.0


def _get_anthropic_client() -> anthropic.Anthropic:
    """Create Anthropic client only when an LLM call is required."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not found. Add it to your .env file before using university agents."
        )

    return anthropic.Anthropic(timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS, max_retries=1)


class UniversityAgent:
    """
    A university agent in the Kormic Commons.

    On creation it:
    1. Loads its persona and constitution.
    2. Seeds its knowledge base with verified facts.
    3. Optionally scrapes configured university pages.

    On each question it:
    1. Checks human-verified answers first.
    2. Searches its knowledge base.
    3. Answers using only supported knowledge.
    4. Creates a pending query when confidence is low.
    5. Stores reliable conversation learning back into the KB.
    """

    MIN_CONFIDENCE = 0.6

    def __init__(self, university_id: str, auto_scrape: bool = True):
        from universities.models import University
        from universities.services import build_persona_dict

        try:
            university = University.objects.get(uuid=university_id)
        except University.DoesNotExist:
            raise ValueError(f"Unknown university: {university_id}")

        self.university_id = university_id
        self.persona = build_persona_dict(university)
        self.kb = UniversityKnowledgeBase(university_id)

        from agents.identity_registry import university_identity

        university_identity(university_id, self.persona["agent_name"])

        seed_facts = self.persona.get("key_facts_seed", [])

        for fact in seed_facts:
            self.kb.store(
                topic=fact["topic"],
                content=fact["content"],
                source_type="seed",
                confidence=1.0,
            )

        console.print(
            f"\n[bold blue]{self.persona['agent_name']}[/bold blue] "
            f"({self.persona['name']}) is initialising..."
        )
        console.print(f"  Loaded {len(seed_facts)} seed facts into knowledge base.")

        if auto_scrape:
            self._scrape_configured_urls()
        else:
            console.print("  Website scraping skipped.")

        stats = self.kb.stats()
        console.print(
            f"  [green]{self.persona['agent_name']} ready. "
            f"Knowledge base: {stats['total_entries']} entries.[/green]\n"
        )

    # --------------------------------------------------
    # Startup / scraping
    # --------------------------------------------------

    def _scrape_configured_urls(self) -> None:
        urls = self.persona.get("scrape_urls", [])

        if not urls:
            console.print("  No scrape URLs configured.")
            return

        console.print(
            f"  Scraping {len(urls)} page(s) from {self.persona['name']} website..."
        )

        try:
            scraped_count = scrape_university(
                university_id=self.university_id,
                urls=urls,
                university_name=self.persona["name"],
                kb=self.kb,
            )
            console.print(f"  [green]Scraped {scraped_count} additional facts.[/green]")
        except Exception as exc:
            console.print("[yellow]Website scraping failed. Continuing with seed facts.[/yellow]")
            console.print(f"[dim]{exc}[/dim]")

    # --------------------------------------------------
    # Model JSON helpers
    # --------------------------------------------------

    def _clean_model_json(self, raw: str) -> str:
        text = str(raw or "").strip()

        if text.startswith("```"):
            text = text.replace("```json", "")
            text = text.replace("```", "")
            text = text.strip()

        first_brace = text.find("{")
        last_brace = text.rfind("}")

        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]

        return text.strip()

    def _parse_json_response(self, raw: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return json.loads(self._clean_model_json(raw))
        except Exception:
            return fallback

    # --------------------------------------------------
    # Text matching / human verified KB search
    # --------------------------------------------------

    def _normalize_text(self, text: str) -> str:
        return " ".join(str(text or "").lower().strip().split())

    def _token_overlap_score(self, a: str, b: str) -> float:
        a_words = set(self._normalize_text(a).replace("?", "").split())
        b_words = set(self._normalize_text(b).replace("?", "").split())

        if not a_words or not b_words:
            return 0.0

        overlap = len(a_words.intersection(b_words))
        total = max(len(a_words), len(b_words))

        return overlap / total

    def _find_human_verified_answer(self, question: str) -> Optional[Dict[str, Any]]:
        """
        Search durable human-verified answers before asking Claude.

        This is what lets the agent learn from human corrections.
        """
        from django_api.models import VerifiedAnswer

        question_norm = self._normalize_text(question)
        best_record = None
        best_score = 0.0

        for record in VerifiedAnswer.objects.filter(university_id=self.university_id):
            saved_question = record.question or ""
            saved_answer = record.answer or ""

            if not saved_question or not saved_answer:
                continue

            saved_norm = self._normalize_text(saved_question)
            result = {"answer": saved_answer, "query_id": record.query_id}

            if question_norm == saved_norm:
                return result

            score = self._token_overlap_score(question, saved_question)

            if score > best_score:
                best_score = score
                best_record = result

        if best_score >= 0.75:
            return best_record

        return None

    # --------------------------------------------------
    # Prompt builders
    # --------------------------------------------------

    def _build_system_prompt(self, caller_role: str = "student") -> str:
        knowledge_context = self.kb.get_full_context()

        response_style = """

OUTPUT FORMAT RULES:
Use plain terminal-friendly text.
Do not use Markdown formatting.
Do not use ## headings, **bold**, markdown tables, or long divider lines.
Use short paragraphs and simple numbered points like 1), 2), 3) when useful.
Sound like a university program agent, not a generic ChatGPT report.

HONESTY RULE:
If exact data is missing from the knowledge base, do not invent it.
If a question requires exact numbers, deadlines, placement statistics, stipend amounts,
faculty names, or current official data and it is not present in the knowledge base,
return low confidence.

HUMAN VERIFIED RULE:
Human-verified knowledge has highest priority.
If a human-verified answer exists, use it directly.
"""

        officer_context = ""
        if caller_role == "officer":
            program_name = self.persona["name"]
            officer_context = f"""

CALLER CONTEXT:
You are talking to your own {program_name} admissions officer, logged into
{program_name}'s own dashboard -- there is no other way to reach this chat.
"My university", "our university", "we", and "my program" always mean
{program_name} itself. Never ask them to identify, confirm, or name their
university or program; you already know it.
"""

        return self.persona["constitution"] + response_style + officer_context + "\n\n" + knowledge_context

    def _build_student_context(self, student_context: Optional[dict] = None) -> str:
        if not student_context:
            return ""

        return (
            "\n\nSTUDENT CONTEXT:\n"
            f"Name: {student_context.get('name', 'the student')}\n"
            f"GPA: {student_context.get('gpa')} / {student_context.get('gpa_scale', '4.0')}\n"
            f"From: {student_context.get('institution', 'unknown institution')}\n"
            f"Major: {student_context.get('major', 'unknown')}\n"
            f"Program: {student_context.get('program', 'unknown')}\n"
            f"GRE Quant: {student_context.get('gre_quant', 'not taken')}\n"
            f"GRE Verbal: {student_context.get('gre_verbal', 'not taken')}\n"
            f"TOEFL: {student_context.get('toefl', 'not taken')}\n"
            f"Budget: USD {student_context.get('budget', 'unspecified')}/year\n"
            f"Work Experience: {student_context.get('work_months', 0)} months\n"
            f"Research: {student_context.get('research', 'None stated')}"
        )

    # --------------------------------------------------
    # KB search helpers
    # --------------------------------------------------

    def _entry_value(self, entry: Any, field: str, default: Any = "") -> Any:
        if isinstance(entry, dict):
            return entry.get(field, default)

        return getattr(entry, field, default)

    def _build_relevant_kb_context(self, question: str, limit: int = 5) -> tuple[str, List[Any]]:
        try:
            results = self.kb.search(question) or []
        except Exception as exc:
            console.print(f"[yellow]Knowledge base search failed: {exc}[/yellow]")
            results = []

        chunks = []

        for entry in results[:limit]:
            chunks.append(
                "Topic: {topic}\nContent: {content}\nConfidence: {confidence}\n"
                "Source Type: {source_type}\n".format(
                    topic=self._entry_value(entry, "topic", "Unknown topic"),
                    content=self._entry_value(entry, "content", ""),
                    confidence=self._entry_value(entry, "confidence", "unknown"),
                    source_type=self._entry_value(entry, "source_type", "unknown"),
                )
            )

        return "\n".join(chunks), results

    # --------------------------------------------------
    # Confidence / trust metadata
    # --------------------------------------------------

    def _confidence_level(self, score: float) -> str:
        if score >= 0.85:
            return "high"
        if score >= self.MIN_CONFIDENCE:
            return "medium"
        if score > 0:
            return "low"
        return "unknown"

    def _build_trust_context(
        self,
        confidence: float,
        reason: str = "",
        source_type: str = "conversation",
        needs_verification: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if needs_verification is None:
            needs_verification = confidence < self.MIN_CONFIDENCE

        return {
            "confidence": {
                "score": confidence,
                "level": self._confidence_level(confidence),
                "needs_verification": needs_verification,
                "reason": reason,
            },
            "source_type": source_type,
        }

    # --------------------------------------------------
    # Pending queries
    # --------------------------------------------------

    def _serialize_pending_query(self, query) -> Dict[str, Any]:
        return {
            "query_id": query.id,
            "university_id": query.university_id,
            "university": query.university_name,
            "agent_name": query.agent_name,
            "student_id": query.student_id,
            "student_name": query.student_name,
            "program": query.program,
            "question": query.question,
            "timestamp": query.created_at.isoformat(),
            "status": query.status,
            "priority": query.priority,
            "urgency_reason": query.urgency_reason,
            "display_status": query.display_status,
            "escalation_chain": query.escalation_chain,
            "group": query.group.slug if query.group_id else None,
            "routed_to_name": query.routed_to_name,
            "routed_to_email": query.routed_to_email,
            "confidence": query.confidence,
            "answer": query.answer,
            "answered_by": query.answered_by,
            "answered_at": query.answered_at.isoformat() if query.answered_at else None,
        }

    def _classify_query_urgency(
        self,
        question: str,
        failure_reason: str = "",
        student_context: Optional[dict] = None,
    ) -> Dict[str, str]:
        question_text = question or ""
        reason_text = failure_reason or ""
        student_context = student_context or {}

        fallback_keywords = [
            "urgent", "deadline", "last date", "final date", "due date",
            "today", "tomorrow", "this week", "visa", "i-20", "sevis",
            "funding deadline", "scholarship deadline", "assistantship deadline",
            "application deadline", "offer deadline", "deposit deadline",
            "fall 2027", "spring 2028", "fall 2028", "2 days", "two days",
            "few days", "as soon as possible", "asap",
        ]

        combined = f"{question_text} {reason_text}".lower()
        keyword_urgent = any(word in combined for word in fallback_keywords)

        try:
            prompt = f"""
You are an admissions operations triage assistant.

Classify this escalated university query as urgent or normal.

Return ONLY valid JSON:
{{
  "priority": "urgent" or "normal",
  "urgency_reason": "short reason"
}}

QUESTION:
{question_text}

ESCALATION REASON:
{reason_text}

STUDENT CONTEXT:
{json.dumps(student_context, indent=2, ensure_ascii=False)}
"""

            client = _get_anthropic_client()

            response = client.messages.create(
                model=MODEL,
                max_tokens=200,
                system="Return only valid JSON. No markdown.",
                messages=[{"role": "user", "content": prompt}],
            )

            result = self._parse_json_response(
                response.content[0].text,
                {
                    "priority": "urgent" if keyword_urgent else "normal",
                    "urgency_reason": "Fallback urgency classification used.",
                },
            )

            priority = str(result.get("priority", "normal")).lower().strip()
            urgency_reason = str(
                result.get("urgency_reason", "AI classified this query.")
            ).strip()

            if priority not in ["urgent", "normal"]:
                priority = "urgent" if keyword_urgent else "normal"

            if keyword_urgent and priority != "urgent":
                priority = "urgent"
                urgency_reason = (
                    "Marked urgent by fallback because the question appears time-sensitive."
                )

            return {
                "priority": priority,
                "urgency_reason": urgency_reason,
            }

        except Exception:
            if keyword_urgent:
                return {
                    "priority": "urgent",
                    "urgency_reason": (
                        "Marked urgent by fallback because the question appears time-sensitive."
                    ),
                }

            return {
                "priority": "normal",
                "urgency_reason": "No clear time-sensitive risk detected.",
            }

    def _display_status_for_query(self, query: Dict[str, Any]) -> str:
        status = str(query.get("status", "")).lower()
        priority = str(query.get("priority", "normal")).lower()

        if status in ["resolved", "answered"]:
            return "answered"

        if priority == "urgent":
            return "urgent"

        return "pending"

    def _find_existing_active_query(self, question: str) -> Optional[Dict[str, Any]]:
        from django_api.models import PendingQuery

        question_norm = self._normalize_text(question)


        active_queries = PendingQuery.objects.filter(university_id=self.university_id).exclude(
            status__in=[PendingQuery.Status.RESOLVED, PendingQuery.Status.IGNORED]
        )

        for query in active_queries:
            if self._normalize_text(query.question) == question_norm:
                return self._serialize_pending_query(query)

        return None

    def create_pending_query(
        self,
        question: str,
        student_context: Optional[dict] = None,
        failure_reason: str = "Agent could not answer confidently from verified knowledge base.",
        confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        existing_query = self._find_existing_active_query(question)

        if existing_query:
            return existing_query

        from django_api.models import PendingQuery
        from universities.knowledge_groups import resolve_group_for_question

        student_context = student_context or {}

        disciplines = student_context.get("disciplines", [])
        if student_context.get("program"):
            program = student_context.get("program")
        elif isinstance(disciplines, list) and disciplines:
            program = disciplines[0]
        else:
            program = student_context.get("major", "Graduate Program")

        urgency = self._classify_query_urgency(
            question=question,
            failure_reason=failure_reason,
            student_context=student_context,
        )

        priority = urgency.get("priority", "normal")
        urgency_reason = urgency.get(
            "urgency_reason",
            "No urgency reason available.",
        )

        # A1: route to the matching knowledge group's named contact -- a
        # university with no groups configured yet still creates the
        # PendingQuery (group=None), it just isn't routed anywhere specific.
        group = resolve_group_for_question(self.university_id, question)
        routed_to_name = group.escalation_contact_name if group else ""
        routed_to_email = group.escalation_contact_email if group else ""

        escalation_chain = [
            {"step": "Student asked Aria", "resolved": True},
            {
                "step": f"Aria asked {self.persona['agent_name']}",
                "resolved": True,
            },
            {"step": failure_reason, "resolved": False},
            {
                "step": f"Urgency classified as {priority}: {urgency_reason}",
                "resolved": True,
            },
            {
                "step": (
                    f"Routed to {group.get_slug_display()} ({routed_to_name} <{routed_to_email}>)"
                    if group
                    else "No knowledge group configured for this university -- not routed to a named contact"
                ),
                "resolved": bool(group),
            },
        ]

        query = PendingQuery.objects.create(
            university_id=self.university_id,
            university_name=self.persona["name"],
            agent_name=self.persona["agent_name"],
            student_id=student_context.get("student_id", ""),
            student_name=student_context.get("name", "Unknown"),
            program=program,
            question=question,
            priority=priority,
            urgency_reason=urgency_reason,
            escalation_chain=escalation_chain,
            group=group,
            routed_to_name=routed_to_name,
            routed_to_email=routed_to_email,
            confidence=confidence,
        )

        # A1: notify the group's contact the moment the escalation lands in
        # their group. Best-effort -- send_escalation_routed_alert never
        # raises, and no-ops when the group has no contact email.
        if group is not None:
            from universities.notifications import send_escalation_routed_alert

            send_escalation_routed_alert(query=query, group=group)

        from notifications.models import NotificationLog
        from notifications.services import notify_university

        notify_university(
            self.university_id,
            event_type=NotificationLog.EventType.UNIVERSITY_QUERY,
            title="New student question",
            body=f"{query.student_name or 'A student'} asked: {query.question}",
            data={
                "type": "university_query",
                "query_id": query.id,
                "student_id": query.student_id,
                "university_id": self.university_id,
                "priority": query.priority,
                "route": f"/university/{self.university_id}/queries",
            },
        )

        return self._serialize_pending_query(query)

    def show_pending_queries(self) -> None:
        from django_api.models import PendingQuery

        active_queries = [
            self._serialize_pending_query(query)
            for query in PendingQuery.objects.filter(university_id=self.university_id).exclude(
                status__in=[PendingQuery.Status.RESOLVED, PendingQuery.Status.IGNORED]
            )
        ]

        if not active_queries:
            print("\nNo active queries.")
            return

        urgent = [
            query
            for query in active_queries
            if self._display_status_for_query(query) == "urgent"
        ]

        pending = [
            query
            for query in active_queries
            if self._display_status_for_query(query) == "pending"
        ]

        print("\n========== ACTIVE QUERIES ==========")

        if urgent:
            print("\nURGENT")
            print("----------------------------------")
            for query in urgent:
                print(
                    f"#{query.get('query_id')} | "
                    f"{query.get('student_name', 'Student')} | "
                    f"{query.get('program', 'Program')} | "
                    f"{query.get('question')} | "
                    f"Reason: {query.get('urgency_reason', 'No reason stored')}"
                )

        if pending:
            print("\nPENDING")
            print("----------------------------------")
            for query in pending:
                print(
                    f"#{query.get('query_id')} | "
                    f"{query.get('student_name', 'Student')} | "
                    f"{query.get('program', 'Program')} | "
                    f"{query.get('question')}"
                )

        print("===================================")

    def resolve_pending_query(
        self,
        query_id: int,
        answer: str,
        answered_by: str = "University contact",
    ) -> bool:
        from django.utils import timezone
        from django_api.models import PendingQuery, VerifiedAnswer

        try:
            query = PendingQuery.objects.get(id=query_id)
        except PendingQuery.DoesNotExist:
            print(f"Query ID {query_id} not found.")
            return False

        question_text = query.question or f"Pending query {query_id}"

        query.status = PendingQuery.Status.RESOLVED
        query.priority = PendingQuery.Priority.NORMAL
        query.answer = answer
        query.answered_by = answered_by
        query.answered_at = timezone.now()
        query.save()

        self.kb.store(
            topic=question_text,
            content=answer,
            source_type="human_verified",
            confidence=1.0,
        )

        VerifiedAnswer.objects.create(
            query=query,
            university_id=query.university_id or self.university_id,
            question=question_text,
            answer=answer,
            answered_by=answered_by,
            source="resolve_pending_query",
            confidence=1.0,
        )

        print(f"Pending Query #{query_id} resolved successfully and saved as human-verified knowledge.")
        return True

    def format_answer_for_student(self, question: str, answer: str) -> str:
        """
        Reformat a university officer's raw resolved-query answer into clean,
        readable text for the student-facing chat notification.

        """
        answer = (answer or "").strip()
        if not answer:
            return answer

        prompt = f"""
A university just answered a student's question. Reformat the answer so it
reads cleanly in a chat message: short paragraphs, and simple numbered
points like 1), 2), 3) when it lists multiple items, options, or steps.

Do not add, remove, or change any fact, number, date, or amount. Do not add
commentary, disclaimers, or a greeting. Only improve structure, clarity, and
grammar.

Do not use Markdown formatting (no **bold**, no ## headings, no markdown
tables, no bullet dashes) -- this is displayed as plain text, so use
numbered points like 1), 2), 3) instead.

QUESTION:
{question}

RAW ANSWER FROM THE UNIVERSITY:
{answer}

Return ONLY the reformatted answer text. No JSON, no preamble.
"""
        try:
            client = _get_anthropic_client()
            response = client.messages.create(
                model=MODEL,
                max_tokens=600,
                system="Return only the reformatted plain-text answer. No markdown, no preamble, no JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            formatted = response.content[0].text.strip()
            return formatted or answer
        except Exception as exc:
            console.print(f"[yellow]Answer formatting for student failed: {exc}[/yellow]")
            return answer

    # --------------------------------------------------
    # Answering
    # --------------------------------------------------

    def answer(
        self,
        question: str,
        student_context: Optional[dict] = None,
        caller_role: str = "student",
    ) -> Dict[str, Any]:
        """
        Answer a question from Aria or direct mode.

        caller_role distinguishes a student's agent asking on the student's
        behalf ("student", the default) from the university's own officer
        previewing/testing the agent directly ("officer") -- the latter adds
        system-prompt context so the agent knows "my university"/"we" refers
        to itself instead of asking the officer to identify their school.

        If the answer cannot be supported with enough confidence, a student
        question creates a PendingQuery so a human contact can follow up.
        An officer talking to their own agent is not a student waiting on
        an answer -- escalation queries exist for the student -> Aria -> Sol
        chain, so officer calls never create one. Instead they get a
        guardrail response pointing at the knowledge base gap directly.
        """
    
        self.kb.reload()

        self.kb.total_questions_answered += 1

        # 1. Human-verified durable knowledge always wins.
        verified = self._find_human_verified_answer(question)

        if verified:
            return {
                "university": self.persona["name"],
                "agent_name": self.persona["agent_name"],
                "answer": verified.get("answer"),
                "pending": False,
                "source": "human_verified",
                "query_id": verified.get("query_id"),
                "confidence": 1.0,
                "trust": self._build_trust_context(
                    confidence=1.0,
                    reason="Answered from human-verified knowledge.",
                    source_type="human_verified",
                    needs_verification=False,
                ),
                "kb_size": self.kb.stats()["total_entries"],
            }

        # 2. Search regular knowledge base.
        kb_context, results = self._build_relevant_kb_context(question)

        if not results and caller_role == "officer" and self.kb.entries:
            kb_context = self.kb.get_full_context()
            results = self.kb.entries

        student_ctx = self._build_student_context(student_context)

        if caller_role == "officer":
            questioner_instruction = (
                "Answer your own university's admissions officer, who is asking "
                "about their own university (not a student's application), "
                "using ONLY the available knowledge base context."
            )
            caller_context_block = ""
        else:
            questioner_instruction = (
                "Answer the student's question using ONLY the available "
                "knowledge base context."
            )
            caller_context_block = f"\n\nSTUDENT:\n{student_ctx}"

        prompt = f"""
You are a university graduate admissions program agent.

{questioner_instruction}
If the answer is not clearly supported, say so and return low confidence.

The question may bundle several distinct topics together (e.g. "what are the
deadlines and funding" asks about two separate things). Judge each topic on
its own: it is common for one topic to be well documented while another has
no supporting knowledge base entries at all. List any such topics in
"unsupported_topics" -- short labels like "funding" or "GRE waiver policy" --
even if your overall confidence is high because the *other* topics are
well supported. Do not let a strong answer on one topic hide silence on
another.

If there are unsupported_topics, answer the parts you can from the
knowledge base, then add ONE short closing line naming that university and
inviting the student to contact it directly for the missing topic(s) --
e.g. "For funding details, contact {self.persona['name']} directly." Do not
write a multi-step checklist of exactly what to ask, do not add your own
advice about the student's finances or plans, and do not restate the
university's contact process at length -- a human follow-up is being
flagged separately, so one short sentence is enough.

Return ONLY valid JSON. No markdown.

Format:
{{
  "answer": "your answer here",
  "confidence": 0.0,
  "unsupported_topics": []
}}

Confidence Guide (for the topics you DID answer -- an unsupported topic
listed separately doesn't need to drag this down):
1.0 = Answer explicitly exists in the knowledge base.
0.8 = Strongly supported by the knowledge base.
0.6 = Reasonable inference.
0.4 = Weak inference.
0.2 = Mostly guessing.
0.0 = No reliable information.

RELEVANT KNOWLEDGE BASE CONTEXT:
{kb_context if kb_context else "No matching knowledge found."}

QUESTION:
{question}
{caller_context_block}
"""

        try:
            client = _get_anthropic_client()

            response = client.messages.create(
                model=MODEL,
                max_tokens=1000,
                system=self._build_system_prompt(caller_role=caller_role),
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()

            parsed = self._parse_json_response(
                raw,
                {
                    "answer": raw,
                    "confidence": 0.0,
                    "unsupported_topics": [],
                },
            )

            answer_text = str(parsed.get("answer", "")).strip()
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
            unsupported_topics = [
                str(topic).strip()
                for topic in (parsed.get("unsupported_topics") or [])
                if str(topic or "").strip()
            ]

        except Exception as exc:
            console.print(f"[yellow]University answer generation failed: {exc}[/yellow]")
            answer_text = "I could not generate an answer from the available university knowledge."
            confidence = 0.0
            unsupported_topics = []


        if not results:
            failure_reason = "No matching knowledge found in the knowledge base."
        elif confidence < self.MIN_CONFIDENCE:
            failure_reason = "Confidence below acceptable threshold."
        else:
            failure_reason = "Answer supported by available knowledge."

        trust = self._build_trust_context(
            confidence=confidence,
            reason=failure_reason,
            source_type="conversation",
            needs_verification=confidence < self.MIN_CONFIDENCE,
        )

        if confidence < self.MIN_CONFIDENCE:
            if caller_role == "officer":
                return {
                    "university": self.persona["name"],
                    "agent_name": self.persona["agent_name"],
                    "answer": (
                        f"I can only answer questions about {self.persona['name']} using "
                        "our verified knowledge base, and I don't have enough verified "
                        "information on this yet. If it should be part of our knowledge "
                        "base, add it there first and I'll be able to answer it directly."
                    ),
                    "pending": False,
                    "knowledge_gap": True,
                    "confidence": confidence,
                    "trust": trust,
                    "kb_size": self.kb.stats()["total_entries"],
                }

            pending_query = self.create_pending_query(
                question=question,
                student_context=student_context,
                failure_reason=failure_reason,
                confidence=confidence,
            )

            return {
                "university": self.persona["name"],
                "agent_name": self.persona["agent_name"],
                "answer": (
                    "I do not have enough verified information to answer this confidently. "
                    f"Pending Query #{pending_query['query_id']} has been created for a university contact."
                ),
                "pending": True,
                "pending_query": pending_query,
                "confidence": confidence,
                "trust": trust,
                "kb_size": self.kb.stats()["total_entries"],
            }

        # Deliberately NOT writing this Q&A back into self.kb
        result = {
            "university": self.persona["name"],
            "agent_name": self.persona["agent_name"],
            "answer": answer_text,
            "pending": False,
            "confidence": confidence,
            "trust": trust,
            "kb_size": self.kb.stats()["total_entries"],
        }

        # The overall confidence above only reflects the topics that WERE
        # supported -- a compound question can clear MIN_CONFIDENCE on
        # deadlines alone while funding has zero knowledge base coverage.
        # For a student's question that gap must still reach the university
        # as a real follow-up instead of being silently absorbed into
        # "confidence: 0.8". An officer previewing their own agent isn't a
        # student waiting on an answer, so it's surfaced as a knowledge-base
        # gap to fill instead of a pending query.
        if unsupported_topics:
            if caller_role == "officer":
                result["knowledge_gap"] = True
                result["unsupported_topics"] = unsupported_topics
                result["answer"] = (
                    answer_text
                    + "\n\nNote: your knowledge base does not yet cover: "
                    + ", ".join(unsupported_topics)
                    + ". Add this information so I can answer it directly next time."
                )
            else:
                pending_query = self.create_pending_query(
                    question=f"{question} (specifically: {', '.join(unsupported_topics)})",
                    student_context=student_context,
                    failure_reason=(
                        "No knowledge base coverage for: " + ", ".join(unsupported_topics)
                    ),
                    confidence=confidence,
                )
                result["partial_pending"] = True
                result["pending_query"] = pending_query
                result["unsupported_topics"] = unsupported_topics

        return result

    # --------------------------------------------------
    # Status / assessment
    # --------------------------------------------------

    def status(self) -> str:
        """Quick status summary for the Commons dashboard."""
        stats = self.kb.stats()

        return (
            f"{self.persona['agent_name']} | {self.persona['name']} | "
            f"{stats['total_entries']} facts | "
            f"{stats['questions_answered']} questions answered"
        )

    def assess_fit(self, student_package: dict) -> Dict[str, Any]:
        """
        Assess a student's fit for this program based on their complete profile.

        Returns a structured dict that can be stored in StudentProfile.
        """
        # Cached agent instance -- pull in any knowledge added since it was
        # built before assessing fit against the knowledge base.
        self.kb.reload()

        self.kb.total_questions_answered += 1

        prompt = f"""
You are {self.persona['agent_name']}, the {self.persona['name']} agent.

Assess this student's fit for your program honestly.

Return ONLY valid JSON. No markdown.

{{
  "match_tier": "strong|target|reach|unlikely",
  "match_score": 0,
  "fit_summary": "",
  "strengths_for_program": [],
  "gaps_for_program": [],
  "recommendation": "strong_apply|apply|consider|unlikely_but_try|do_not_apply",
  "realistic": true,
  "specific_advice": ""
}}

PROGRAM KNOWLEDGE BASE:
{self.kb.get_full_context()}

STUDENT PROFILE:
{json.dumps(student_package, indent=2, ensure_ascii=False)}
"""

        try:
            client = _get_anthropic_client()

            response = client.messages.create(
                model=MODEL,
                max_tokens=1000,
                system=self._build_system_prompt(),
                messages=[{"role": "user", "content": prompt}],
            )

            raw = response.content[0].text.strip()

            assessment = self._parse_json_response(
                raw,
                {
                    "match_tier": "unknown",
                    "match_score": 0,
                    "fit_summary": raw[:500],
                    "strengths_for_program": [],
                    "gaps_for_program": ["Could not parse structured JSON response."],
                    "recommendation": "consider",
                    "realistic": False,
                    "specific_advice": "Review manually because the model response was not valid JSON.",
                },
            )

        except Exception as exc:
            console.print(f"[yellow]Fit assessment generation failed: {exc}[/yellow]")
            assessment = {
                "match_tier": "unknown",
                "match_score": 0,
                "fit_summary": "Fit assessment could not be generated from the available knowledge.",
                "strengths_for_program": [],
                "gaps_for_program": ["Assessment generation failed."],
                "recommendation": "consider",
                "realistic": False,
                "specific_advice": "Try again after checking the API key and university knowledge base.",
            }

        assessment["university"] = self.persona["name"]
        assessment["agent"] = self.persona["agent_name"]

        # Deliberately NOT written to self.kb -- to prevent cross-student leak 
        return assessment
