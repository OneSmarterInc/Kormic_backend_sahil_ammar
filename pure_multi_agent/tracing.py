"""LangChain callbacks preserving actor, tool identity, and turn correlation."""
from uuid import uuid4
import json
import os
from langchain_core.callbacks import BaseCallbackHandler
from pure_multi_agent.telemetry import current, emit, safe_data

VERBOSE = os.getenv('PURE_MULTI_AGENT_VERBOSE', 'true').lower() not in {'0', 'false', 'no'}


def _content(value):
    content = getattr(value, 'content', value)
    if isinstance(content, list):
        return ''.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return content


class GraphTraceLogger(BaseCallbackHandler):
    run_inline = True

    def __init__(self, label='', actor='Aria (Student Agent)', root_run_id=None):
        self.label, self.actor = label, actor
        self.root_run_id = str(root_run_id or current().get('run_id') or uuid4())
        self._step = 0
        self._calls = {}
        self._nodes = {}

    def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
        node = (kwargs.get('metadata') or {}).get('langgraph_node')
        if node and kwargs.get('name') == node:
            self._nodes[str(run_id)] = (self._identity(), node)
            self._log('AGENT_STEP_START', node, run_id, inputs={'method': node},
                parent_run_id=kwargs.get('parent_run_id'))

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        entry = self._nodes.pop(str(run_id), None)
        if entry:
            identity, node = entry
            self._log('AGENT_STEP_RESULT', node, run_id, identity=identity,
                outputs={'updated_state_fields': list(outputs) if isinstance(outputs, dict) else []})

    def on_chain_error(self, error, *, run_id, **kwargs):
        entry = self._nodes.pop(str(run_id), None)
        from github_profiles.scheduling import CapacityBusy
        from pure_multi_agent.capacity import AgentBusy
        if isinstance(error, (CapacityBusy, AgentBusy)):
            return  # The enclosing run records this single resumable pause.
        if entry:
            identity, node = entry
            self._log('AGENT_STEP_ERROR', node, run_id, identity=identity, outputs={'error': str(error)})

    def _identity(self):
        ctx = current()
        return {'actor': ctx.get('actor', self.actor), 'student_id': ctx.get('student_id', self.label),
                'run_id': ctx.get('run_id', self.root_run_id)}

    def _log(self, action, target, run_id, *, inputs=None, outputs=None, identity=None, parent_run_id=None):
        emit(action, target, inputs={'call_id': str(run_id), 'parent_call_id': str(parent_run_id or ''), **(inputs or {})},
            outputs=outputs, **(identity or self._identity()))

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._step += 1
        self._calls[str(run_id)] = (self._identity(), current().get('recipient', 'Student'))
        params = kwargs.get('invocation_params') or {}
        model = params.get('model') or params.get('model_name') or (serialized or {}).get('kwargs', {}).get('model') or ''
        kind = str((serialized or {}).get('id', '')) + ' ' + str(params.get('_type', '')) + ' ' + str(model)
        provider = 'claude' if 'anthropic' in kind.lower() or 'claude' in kind.lower() else 'qwen' if 'qwen' in kind.lower() else 'unknown'
        self._log('MODEL_START', 'Model invocation', run_id,
            inputs={'context_messages': len(messages[0]) if messages else 0, 'provider': provider, 'model': str(model)}, parent_run_id=kwargs.get('parent_run_id'))

    def on_llm_end(self, response, *, run_id, **kwargs):
        identity, recipient = self._calls.pop(str(run_id), (self._identity(), 'Student'))
        try:
            message = response.generations[0][0].message
        except (AttributeError, IndexError):
            return
        calls = getattr(message, 'tool_calls', None) or []
        self._log('MODEL_END', 'Model invocation', run_id, identity=identity,
            outputs={'selected_tools': [call.get('name') for call in calls]})
        # Only provider-designated public summaries; never persist raw thinking blocks.
        for block in message.content if isinstance(message.content, list) else []:
            if isinstance(block, dict) and block.get('type') == 'reasoning' and block.get('summary'):
                self._log('REASONING_SUMMARY', 'Model summary', run_id, identity=identity,
                    outputs={'summary': block['summary']})
        if calls:
            for call in calls:
                self._log('TOOL_CALL_INTENT', call.get('name', 'tool'), run_id, identity=identity,
                    inputs={'arguments': call.get('args', {}), 'tool_call_id': call.get('id')})
        else:
            self._log('MODEL_OUTPUT', recipient, run_id, identity=identity, outputs={'reply': _content(message)})

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
        name = (serialized or {}).get('name', kwargs.get('name', 'tool'))
        self._calls[str(run_id)] = (self._identity(), name)
        self._log('TOOL_CALL_START', name, run_id, inputs={'arguments': inputs if inputs is not None else input_str},
            parent_run_id=kwargs.get('parent_run_id'))

    def on_tool_end(self, output, *, run_id, **kwargs):
        identity, name = self._calls.pop(str(run_id), (self._identity(), getattr(output, 'name', 'tool')))
        value = _content(output)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass
        failed = isinstance(value, dict) and (bool(value.get('error')) or value.get('status') == 'failed')
        self._log('TOOL_ERROR' if failed else 'TOOL_RESULT', name, run_id, identity=identity, outputs={'result': safe_data(value)})

    def on_tool_error(self, error, *, run_id, **kwargs):
        identity, name = self._calls.pop(str(run_id), (self._identity(), 'tool'))
        self._log('TOOL_ERROR', name, run_id, identity=identity, outputs={'error': str(error)})

    def on_llm_error(self, error, *, run_id, **kwargs):
        identity, _ = self._calls.pop(str(run_id), (self._identity(), 'Model invocation'))
        self._log('MODEL_ERROR', 'Model invocation', run_id, identity=identity, outputs={'error': str(error)})
