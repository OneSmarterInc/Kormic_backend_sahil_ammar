"""Source agent adapted to Kormic's shared routing and persistence boundary."""
from pathlib import Path
from langchain_core.messages import convert_to_messages
from langgraph.graph import StateGraph, START, END
from .decision_agent import DecisionPhotoAgent
from .validation import clean_text, validate_data, validate_node, merge_node
from .image_extractor import extract_text_from_image, reread_image_region
from github_profiles.scheduling import CapacityBusy
from pure_multi_agent.capacity import AgentBusy


class RoutedModel:
    def invoke(self, messages, **kwargs):
        from pure_multi_agent.model_router import invoke
        from .agent import _json_from_response
        prepared = convert_to_messages(messages)
        # Preserve the controller's JSON contract instead of triggering a second
        # provider call merely because the first returned prose or code fences.
        routing = {'json_schema': {'type': 'object'}} if kwargs.get('format') == 'json' else {}
        reply = invoke(prepared, **routing)
        try:
            _json_from_response(reply.content)
        except (ValueError, TypeError):
            reply = invoke(prepared, force_claude=True, **routing)
            _json_from_response(reply.content)
        return reply


def extract(paths):
    from pure_multi_agent.telemetry import emit
    emit('AGENT_PROGRESS', 'LinkedIn upload', outputs={'summary': f'Received {len(paths)} LinkedIn files. Preparing text extraction.'})
    def read(state):
        documents = []
        for index, raw in enumerate(paths):
            path = Path(raw)
            emit('AGENT_PROGRESS', 'LinkedIn text extraction', outputs={'summary': f'Extracting LinkedIn file {index+1} of {len(paths)}: {path.name}'})
            try:
                text = path.read_text(encoding='utf-8') if path.suffix.lower() in ('.txt', '.md') else extract_text_from_image(str(path))
            except Exception:
                text = ''  # The agent can retry OCR on this specific image.
            documents.append({'image_id': index, 'path': str(path), 'text': clean_text(text), 'section': 'AUTO'})
        return {'documents': documents}

    def analyze(state):
        observations, traces, warnings = [], [], []
        for doc in state['documents']:
            emit('AGENT_PROGRESS', 'LinkedIn analysis', outputs={'summary': f'Analyzing extracted LinkedIn content from {Path(doc["path"]).name}.'})
            agent = DecisionPhotoAgent(llm=RoutedModel(), validator=validate_data)
            reread = None if Path(doc['path']).suffix.lower() in ('.txt', '.md') else lambda region, mode: reread_image_region(doc['path'], region, mode)
            try:
                observation, _ = agent.extract(doc['text'], section=doc['section'], reread=reread)
            except (CapacityBusy, AgentBusy):
                raise
            except Exception:
                traces.append({'file':Path(doc['path']).name, 'actions':agent.trace, 'status':'failed'})
                warnings.append(f"Could not extract {Path(doc['path']).name}. Please upload a clearer image.")
                continue
            observations.append({'image_id':doc['image_id'], 'source_text':agent.evidence_source,
                                 'section':doc['section'], 'data':observation.model_dump()})
            traces.append({'file':Path(doc['path']).name, 'actions':agent.trace})
            warnings.extend(agent.warnings)
        if not observations:
            raise ValueError('No supported LinkedIn information could be extracted. Upload clearer screenshots or a text export.')
        return {'observations': observations, 'agent_trace':traces, 'warnings':warnings}

    from typing import TypedDict
    class State(TypedDict, total=False):
        documents: list
        observations: list
        validated_observations: list
        merged_profile: dict
        agent_trace: list
        warnings: list
    graph = StateGraph(State)
    for name, fn in [('ocr',read), ('agent',analyze), ('validate',validate_node), ('merge',merge_node)]:
        from pure_multi_agent.telemetry import traced_step
        graph.add_node(name, traced_step(name)(fn))
    for left,right in [(START,'ocr'),('ocr','agent'),('agent','validate'),('validate','merge'),('merge',END)]:
        graph.add_edge(left,right)
    from pure_multi_agent.telemetry import trace_config
    state = graph.compile().invoke({}, trace_config())
    result = state['merged_profile']
    result.update(experience=result.get('experiences',[]), source='linkedin_screenshot_or_text', verified=False,
                  input_files=[Path(p).name for p in paths], agent_trace=state['agent_trace'], warnings=state['warnings'])
    return result
