import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage
from agents.resume_graph import extract_resume, ResumeFacts


class ResumeGraphTests(SimpleTestCase):
    def run_agent(self, facts):
        tools = ['inspect_resume', 'read_resume', 'extract_resume_facts', 'validate_resume_facts', 'finish_resume']
        calls = iter(tools)
        def model(messages, bound_tools=(), **options):
            if options.get('json_schema'):
                return AIMessage(content=json.dumps(facts), response_metadata={'routing_provider': 'qwen'})
            name = next(calls)
            return AIMessage(content='', tool_calls=[{'name': name, 'args': {}, 'id': name}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.txt'
            path.write_text('Alex Student\nalex@example.test\nExample University\nProject Atlas: Python search application.\nPython and SQL skills.', encoding='utf-8')
            with patch('agents.resume_graph.invoke', side_effect=model):
                return extract_resume(str(path))

    def test_graph_executes_tools_and_preserves_projects_and_contact(self):
        data = ResumeFacts(name='Alex Student', email='alex@example.test', skills=['Python'],
            projects=[{'title': 'Project Atlas', 'description': 'Python search application',
                       'evidence': 'Project Atlas: Python search application.'}]).model_dump()
        result = self.run_agent(data)
        self.assertEqual(result['email'], 'alex@example.test')
        self.assertEqual(result['projects'][0]['title'], 'Project Atlas')
        self.assertEqual(result['parser_engine'], 'qwen')
        self.assertEqual(len(result['agent_trace']), 5)

    def test_hallucinated_identity_and_project_are_not_applied(self):
        data = ResumeFacts(name='Someone Else', email='invented@example.test', skills=['Python'],
            projects=[{'title': 'Invented', 'evidence': 'Not in document'}]).model_dump()
        result = self.run_agent(data)
        self.assertIsNone(result['name'])
        self.assertIsNone(result['email'])
        self.assertEqual(result['projects'], [])
        self.assertTrue(result['warnings'])
