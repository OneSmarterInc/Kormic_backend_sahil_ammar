# pure_multi_agent/tracing.py
# Terminal tracing for the LangGraph student agent -- prints every model call
# and every tool call/result as they happen, so you can see the agent's
# actual tool/agent-selection decisions live instead of guessing. Purely for
# understanding/debugging; toggle off with PURE_MULTI_AGENT_VERBOSE=false.

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from rich.console import Console

from django_api.tasks import save_audit_log_task
import logging

console = Console()
logger = logging.getLogger(__name__)

VERBOSE = os.getenv("PURE_MULTI_AGENT_VERBOSE", "true").strip().lower() not in {"0", "false", "no"}


def _truncate(text: Any, limit: int = 400) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 15] + "... [truncated]"

def _safe_dict(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {"payload": str(data)}
    try:
        # Try converting to dict using a stringifier for non-serializable objects
        return json.loads(json.dumps(data, default=str))
    except Exception:
        return {"error": "Could not serialize payload"}

def _message_preview(message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return _truncate(content, 200)


class GraphTraceLogger(BaseCallbackHandler):
    """Logs the student agent's reasoning loop to the terminal and database:
    - every time the model is invoked (and with how much context)
    - every tool the model decides to call, with its arguments
    - every tool's result
    - the model's final natural-language decision (tool call vs direct reply)
    """

    def __init__(self, label: str = ""):
        self.label = label
        self._step = 0

    def _tag(self) -> str:
        return f"[bold blue]\\[{self.label}][/bold blue]" if self.label else ""

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        *,
        run_id,
        **kwargs: Any,
    ) -> None:
        self._step += 1
        history = messages[0] if messages else []
        if VERBOSE:
            console.print(
                f"{self._tag()} [dim]step {self._step}: asking the model "
                f"({len(history)} messages of context so far)[/dim]"
            )
        try:
            from django_api.models import AgentAuditLog
            AgentAuditLog.objects.create(
                run_id=str(run_id),
                student_id=self.label,
                actor_agent="Aria (Student Agent)",
                action_type="REASONING_START",
                target="Model Invocation",
                inputs=_safe_dict({"context_messages": len(history)}),
                outputs=_safe_dict({})
            )
        except Exception as e:
            logger.error(f"Failed to log reasoning start: {e}")

    def on_llm_end(self, response, *, run_id, **kwargs: Any) -> None:
        try:
            message = response.generations[0][0].message
        except Exception:
            return

        tool_calls = getattr(message, "tool_calls", None) or []
        
        try:
            from django_api.models import AgentAuditLog
            if tool_calls:
                for call in tool_calls:
                    args = call.get("args", {})
                    args_str = json.dumps(args, ensure_ascii=False)
                    if VERBOSE:
                        console.print(
                            f"{self._tag()} [bold cyan]model decided to call tool[/bold cyan] "
                            f"{call.get('name')}({args_str})"
                        )
                    
                    # Check if it's agent communication
                    is_agent_comm = call.get('name') in ['ask_university', 'compare_all_universities']
                    
                    AgentAuditLog.objects.create(
                        run_id=str(run_id),
                        student_id=self.label,
                        actor_agent="Aria (Student Agent)",
                        action_type="AGENT_COMMUNICATION_INTENT" if is_agent_comm else "TOOL_CALL_INTENT",
                        target=call.get('name', 'unknown_tool'),
                        inputs=_safe_dict(args),
                        outputs=_safe_dict({})
                    )
            else:
                final_reply = _message_preview(message)
                if VERBOSE:
                    console.print(
                        f"{self._tag()} [bold green]model produced a final reply:[/bold green] "
                        f"{final_reply}"
                    )
                AgentAuditLog.objects.create(
                    run_id=str(run_id),
                    student_id=self.label,
                    actor_agent="Aria (Student Agent)",
                    action_type="FINAL_REPLY",
                    target="Student",
                    inputs=_safe_dict({}),
                    outputs=_safe_dict({"reply": final_reply})
                )
        except Exception as e:
            logger.error(f"Failed to log llm end: {e}")

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        name = serialized.get("name", "tool")
        payload = inputs if inputs is not None else input_str
        if VERBOSE:
            console.print(
                f"{self._tag()} [yellow]-> running tool[/yellow] {name}"
                f"({_truncate(payload, 300)})"
            )
        
        is_agent_comm = name in ['ask_university', 'compare_all_universities']
        
        try:
            from django_api.models import AgentAuditLog
            AgentAuditLog.objects.create(
                run_id=str(run_id),
                student_id=self.label,
                actor_agent="Aria (Student Agent)",
                action_type="AGENT_COMMUNICATION_START" if is_agent_comm else "TOOL_CALL_START",
                target=name,
                inputs=_safe_dict({"payload": payload}),
                outputs=_safe_dict({})
            )
        except Exception as e:
            logger.error(f"Failed to log tool start: {e}")

    def on_tool_end(self, output: Any, *, run_id, **kwargs: Any) -> None:
        text = output if isinstance(output, str) else getattr(output, "content", str(output))
        if VERBOSE:
            console.print(f"{self._tag()} [green]<- tool result:[/green] {_truncate(text, 400)}")
        
        try:
            from django_api.models import AgentAuditLog
            AgentAuditLog.objects.create(
                run_id=str(run_id),
                student_id=self.label,
                actor_agent="Aria (Student Agent)",
                action_type="TOOL_RESULT",
                target="unknown",
                inputs=_safe_dict({}),
                outputs=_safe_dict({"result": text})
            )
        except Exception as e:
            logger.error(f"Failed to log tool end: {e}")

    def on_tool_error(self, error: BaseException, *, run_id, **kwargs: Any) -> None:
        if VERBOSE:
            console.print(f"{self._tag()} [red]tool error: {error}[/red]")
        try:
            from django_api.models import AgentAuditLog
            AgentAuditLog.objects.create(
                run_id=str(run_id),
                student_id=self.label,
                actor_agent="Aria (Student Agent)",
                action_type="TOOL_ERROR",
                target="unknown",
                inputs=_safe_dict({}),
                outputs=_safe_dict({"error": str(error)})
            )
        except Exception as e:
            logger.error(f"Failed to log tool error: {e}")
