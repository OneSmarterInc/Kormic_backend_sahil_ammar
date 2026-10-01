from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from accounts.models import Account, TOTPDevice
from django_api.models import AgentAuditLog
from pure_multi_agent.telemetry import scope, operation, emit, trace_config, current
from pure_multi_agent.tracing import GraphTraceLogger

class TelemetryTests(TestCase):
    def test_model_start_records_provider_without_prompt_contents(self):
        tracer=GraphTraceLogger(label='s',root_run_id='run')
        for model,kind in [('qwen3:1.7b','ChatOllama'),('claude-haiku-4-5-20251001','ChatAnthropic')]:
            tracer.on_chat_model_start({'id':['langchain',kind]},[[]],run_id=model,
                invocation_params={'model':model})
        rows=list(AgentAuditLog.objects.order_by('id'))
        self.assertEqual([r.inputs['provider'] for r in rows],['qwen','claude'])
        self.assertEqual(rows[0].inputs['model'],'qwen3:1.7b')

    def test_nested_agent_tools_keep_message_correlation(self):
        with scope('Student Agent', 's', 'run'):
            current()['message_id'] = 123
            with operation('GitHub Agent', 'read_repositories'):
                emit('TOOL_CALL_START', 'list_repos')
        self.assertEqual(set(AgentAuditLog.objects.values_list('inputs__message_id', flat=True)), {123})

    def test_passive_status_poll_does_not_create_agent_handoffs(self):
        from pure_multi_agent.telemetry import traced_operation
        @traced_operation('Verification Agent')
        def check(student_id, allow_analysis=True):
            return {'status':'needs_review'}
        check('student', allow_analysis=False)
        self.assertFalse(AgentAuditLog.objects.exists())
        with scope('Student Agent', 'student', 'turn'):
            check('student', allow_analysis=False)
        self.assertEqual(AgentAuditLog.objects.count(), 2)

    def test_model_and_node_progress_preserve_public_summary_only(self):
        from langchain_core.messages import AIMessage
        tracer = GraphTraceLogger(label='s', root_run_id='workflow')
        with scope('CV Agent', 's', 'workflow'):
            tracer.on_chain_start({}, {}, run_id='node', name='extract', metadata={'langgraph_node':'extract'})
            tracer.on_chat_model_start({}, [[]], run_id='model')
            message = AIMessage(content=[{'type':'reasoning','summary':[{'type':'summary_text','text':'Checked document evidence.'}], 'text':'private internal text'}, {'type':'text','text':'Done'}])
            tracer.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=message)]]), run_id='model')
            tracer.on_chain_end({'facts':{}}, run_id='node')
        rows=list(AgentAuditLog.objects.filter(run_id='workflow').order_by('id'))
        self.assertEqual([r.action_type for r in rows], ['AGENT_STEP_START','MODEL_START','MODEL_END','REASONING_SUMMARY','MODEL_OUTPUT','AGENT_STEP_RESULT'])
        self.assertNotIn('private internal text', str([r.outputs for r in rows]))
        self.assertEqual(rows[-1].outputs['updated_state_fields'], ['facts'])

    def test_real_graph_github_request_reply(self):
        from pure_multi_agent.tools.github_tools import build_tools
        class State(TypedDict):
            result: dict
        graph = StateGraph(State)
        github = build_tools({'canonical_student_id': 'student-1'})[0]
        graph.add_node('tools', lambda state: {'result': github.invoke({})})
        graph.add_edge(START, 'tools'); graph.add_edge('tools', END)
        with patch('accounts.github_oauth.get_connection_for_student_id', return_value=None):
            with scope('Cove (Student Agent)', 'student-1', 'turn-1'):
                result = graph.compile().invoke({}, trace_config())
        self.assertEqual(result['result']['status'], 'not_connected')
        rows = list(AgentAuditLog.objects.order_by('id'))
        request = next(r for r in rows if r.action_type == 'AGENT_COMMUNICATION_START')
        reply = next(r for r in rows if r.action_type == 'AGENT_COMMUNICATION_REPLY')
        self.assertEqual((request.actor_agent, request.target), ('Cove (Student Agent)', 'GitHub Agent'))
        self.assertEqual((reply.actor_agent, reply.target), ('GitHub Agent', 'Cove (Student Agent)'))
        self.assertEqual(request.inputs['exchange_id'], reply.outputs['exchange_id'])
        self.assertEqual(reply.outputs['result']['status'], 'not_connected')
        self.assertEqual({r.run_id for r in rows}, {'turn-1'})
        tool_result = next(r for r in rows if r.action_type == 'TOOL_RESULT')
        self.assertEqual(tool_result.target, 'get_github_processing_status')
        self.assertEqual(tool_result.actor_agent, 'Cove (Student Agent)')

    def test_errors_restore_context(self):
        with scope('Student Agent', 's', 'turn'):
            with self.assertRaises(ValueError):
                with operation('LinkedIn Agent', 'extract'):
                    raise ValueError('Unreadable document')
            self.assertEqual(current()['actor'], 'Student Agent')
        error = AgentAuditLog.objects.get(action_type='AGENT_COMMUNICATION_ERROR')
        self.assertEqual(error.target, 'Student Agent')
        self.assertEqual(error.outputs['error'], 'Unreadable document')
        self.assertEqual(current(), {})

    def test_full_outputs_and_sensitive_media(self):
        from langchain_core.messages import AIMessage
        tracer = GraphTraceLogger(label='s', root_run_id='turn')
        message = AIMessage(content='Long reply ' * 100)
        tracer.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=message)]]), run_id=uuid4())
        self.assertEqual(AgentAuditLog.objects.get(action_type='MODEL_OUTPUT').outputs['reply'], message.content)
        with scope('Document Agent', 's'):
            emit('TOOL_RESULT', 'read', outputs={'access_token':'private', '_media_blocks':['binary'], 'result': 'visible'})
        row = AgentAuditLog.objects.order_by('id').last()
        self.assertEqual(row.outputs['access_token'], '[redacted]')
        self.assertEqual(row.outputs['_media_blocks'], '[redacted]')

    def test_telemetry_failure_does_not_break_operation(self):
        with patch('django_api.models.AgentAuditLog.objects.create', side_effect=RuntimeError('storage unavailable')):
            with scope('Student Agent', 's'):
                with operation('GitHub Agent', 'read') as result:
                    result['result'] = 'ok'
        self.assertEqual(current(), {})

class TelemetryApiTests(TestCase):
    def setUp(self):
        user = User.objects.create_user('telemetry-admin')
        Account.objects.create(user=user, role='superuser')
        TOTPDevice.objects.create(user=user, secret_encrypted='fixture', confirmed_at=timezone.now())
        self.client = APIClient(); self.client.force_authenticate(user)
        self.url = '/api/superuser/agent-audit-logs/'
        self.rows = [AgentAuditLog.objects.create(run_id='turn', student_id='s', actor_agent='GitHub Agent', action_type='TOOL_RESULT') for _ in range(5)]
        AgentAuditLog.objects.filter(pk=self.rows[0].pk).update(timestamp=timezone.now())

    def test_paged_child_events_keep_exact_message_and_student_identity(self):
        from django_api.models import StudentProfile
        student=StudentProfile.objects.create(name='Task owner')
        sid=str(student.uuid)
        AgentAuditLog.objects.create(run_id='task-a',student_id=sid,actor_agent='Student Agent',action_type='RUN_START',inputs={'message_id':101,'message':'Research IIT Bombay'})
        AgentAuditLog.objects.create(run_id='task-a',student_id=sid,actor_agent='Student Agent',action_type='BACKGROUND_JOB_LINKED',outputs={'job_id':'child-a'})
        AgentAuditLog.objects.create(run_id='task-b',student_id=sid,actor_agent='Student Agent',action_type='RUN_START',inputs={'message_id':102,'message':'Review GitHub'})
        event=AgentAuditLog.objects.create(run_id='child-a',actor_agent='Research Agent',action_type='TOOL_RESULT')
        result=self.client.get(self.url,{'limit':1}).data['logs'][0]
        self.assertEqual(result['id'],event.id)
        self.assertEqual(result['message_id'],101)
        self.assertEqual(result['student_name'],'Task owner')
        self.assertEqual(result['user_message'],'Research IIT Bombay')

    def test_cursor_pages_have_no_gaps(self):
        first = self.client.get(self.url, {'limit':2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual([r['id'] for r in first.data['logs']], [self.rows[4].pk, self.rows[3].pk])
        older = self.client.get(self.url, {'before_id':first.data['next_before_id'], 'limit':2})
        self.assertEqual([r['id'] for r in older.data['logs']], [self.rows[2].pk, self.rows[1].pk])
        live = self.client.get(self.url, {'since_id':self.rows[0].pk, 'limit':2})
        self.assertEqual([r['id'] for r in live.data['logs']], [self.rows[1].pk, self.rows[2].pk])
        self.assertTrue(live.data['has_more'])
        last = self.client.get(self.url, {'since_id':live.data['next_since_id'], 'limit':2})
        self.assertEqual([r['id'] for r in last.data['logs']], [self.rows[3].pk, self.rows[4].pk])
        self.assertFalse(last.data['has_more'])

    def test_filters_and_permissions(self):
        self.assertEqual(self.client.get(self.url, {'student_id':'other'}).data['logs'], [])
        self.assertEqual(self.client.get(self.url, {'run_id':'other'}).data['logs'], [])
        self.assertEqual(self.client.get(self.url, {'since_id':'bad'}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'limit':-5}).status_code, 200)
        user = User.objects.create_user('ordinary')
        Account.objects.create(user=user, role='student')
        self.client.force_authenticate(user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_cv_upload_extraction_records_each_tool_and_full_result(self):
        import json
        import tempfile
        from pathlib import Path
        from langchain_core.messages import AIMessage
        from agents.resume_graph import extract_resume, ResumeFacts
        tools = ['inspect_resume', 'read_resume', 'extract_resume_facts', 'validate_resume_facts', 'finish_resume']
        calls = iter(tools)
        facts = ResumeFacts(name='Alex Student', skills=['Python']).model_dump()
        def model(messages, bound_tools=(), **options):
            if options.get('json_schema'):
                return AIMessage(content=json.dumps(facts), response_metadata={'routing_provider':'fixture'})
            name = next(calls)
            return AIMessage(content='', tool_calls=[{'name':name,'args':{},'id':name}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.txt'
            path.write_text('Alex Student\nExample University. A computer science student with Python programming and software project experience.', encoding='utf-8')
            with scope('Student', 'cv-student', 'cv-upload'):
                with operation('CV Agent', 'parse_resume') as event:
                    with patch('agents.resume_graph.invoke', side_effect=model):
                        event['result'] = extract_resume(str(path))
        rows = AgentAuditLog.objects.filter(run_id='cv-upload', action_type='TOOL_CALL_START').order_by('id')
        self.assertEqual(list(rows.values_list('target', flat=True)), tools)
        self.assertEqual(set(rows.values_list('actor_agent', flat=True)), {'CV Agent'})
        self.assertEqual(AgentAuditLog.objects.filter(run_id='cv-upload', action_type='TOOL_RESULT').count(), 5)
        reply = AgentAuditLog.objects.get(run_id='cv-upload', action_type='AGENT_COMMUNICATION_REPLY')
        self.assertEqual(reply.outputs['result']['name'], 'Alex Student')
