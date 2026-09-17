# pure_multi_agent/university_graph.py
# Each university is modeled as a real LangGraph node with its own typed
# state, reusing agents.university_agent.UniversityAgent (via
# agents.commons.get_university_agent, which owns construction/caching,
# seed facts, scraping, and KB) completely unchanged. Used both for a single
# university (.invoke()) and for the parallel "ask every university" fan-out
# (.batch()), which lets the graph runtime manage the concurrency instead of
# a hand-rolled for-loop with try/except per iteration.

from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict

from langgraph.graph import END, StateGraph

from agents import commons


class UniversityState(TypedDict, total=False):
    university_id: str
    question: str
    student_context: Optional[dict]
    result: Dict[str, Any]


def _answer_node(state: UniversityState) -> Dict[str, Any]:
    university_id = state["university_id"]

    try:
        agent = commons.get_university_agent(university_id)
    except ValueError:
        return {
            "result": {
                "university": university_id,
                "agent_name": university_id,
                "answer": f"Unknown university_id: {university_id}",
                "error": "unknown_university_id",
            }
        }

    try:
        result = agent.answer(state["question"], state.get("student_context"))
    except Exception as exc:
        result = {
            "university": getattr(agent, "persona", {}).get("university", university_id),
            "agent_name": getattr(agent, "persona", {}).get("agent_name", university_id),
            "answer": "I could not answer this because the university agent hit an error.",
            "error": str(exc),
        }

    return {"result": result}


def build_university_graph():
    graph = StateGraph(UniversityState)
    graph.add_node("answer", _answer_node)
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph.compile()


_university_graph = None


def get_university_graph():
    """Process-wide singleton, built once and reused (mirrors the lazy
    module-level caches already used throughout agents/commons.py)."""
    global _university_graph
    if _university_graph is None:
        _university_graph = build_university_graph()
    return _university_graph


def ask_one(university_id: str, question: str, student_context: Optional[dict] = None) -> Dict[str, Any]:
    result = get_university_graph().invoke(
        {"university_id": university_id, "question": question, "student_context": student_context}
    )
    return result["result"]


def ask_all(
    question: str,
    student_context: Optional[dict] = None,
    university_ids: Optional[list] = None,
    max_concurrency: int = 5,
) -> list:
    """Parallel fan-out across every known university, handled by the graph
    runtime's .batch() -- the old hand-rolled for-loop this replaced
    (agents.commons.query_all) has since been deleted as dead code."""
    target_ids = university_ids if university_ids is not None else commons.list_university_ids()

    inputs = [
        {"university_id": university_id, "question": question, "student_context": student_context}
        for university_id in target_ids
    ]

    if not inputs:
        return []

    outputs = get_university_graph().batch(inputs, config={"max_concurrency": max_concurrency})
    return [output["result"] for output in outputs]
