import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage
from agents.resume_graph import extract_resume, ResumeFacts


class ResumeGraphTests(SimpleTestCase):
    def run_agent(self, facts):
        def model(messages, bound_tools=(), **options):
            self.assertFalse(bound_tools)
            self.assertEqual(options['json_schema'], ResumeFacts.model_json_schema())
            self.assertNotIn('"properties"', messages[0].content)
            return AIMessage(content=json.dumps(facts), response_metadata={'routing_provider': 'claude'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.txt'
            path.write_text('Alex Student\nalex@example.test\nExample University\nProject Atlas: Python search application.\nPython and SQL skills.', encoding='utf-8')
            with patch('agents.resume_graph.invoke', side_effect=model) as paid:
                result = extract_resume(str(path))
            paid.assert_called_once()
            self.assertEqual([step['tool'] for step in result['agent_trace']],
                ['inspect_resume', 'read_resume', 'extract_resume_facts', 'validate_resume_facts', 'finish_resume'])
            return result

    def test_graph_executes_tools_and_preserves_projects_and_contact(self):
        data = ResumeFacts(name='Alex Student', email='alex@example.test', skills=['Python'],
            projects=[{'title': 'Project Atlas', 'description': 'Python search application',
                       'evidence': 'Project Atlas: Python search application.'}]).model_dump()
        result = self.run_agent(data)
        self.assertEqual(result['email'], 'alex@example.test')
        self.assertEqual(result['projects'][0]['title'], 'Project Atlas')
        self.assertEqual(result['parser_engine'], 'claude')
        self.assertEqual(len(result['agent_trace']), 5)

    def test_hallucinated_identity_and_project_are_not_applied(self):
        data = ResumeFacts(name='Someone Else', email='invented@example.test', skills=['Python'],
            projects=[{'title': 'Invented', 'evidence': 'Not in document'}]).model_dump()
        result = self.run_agent(data)
        self.assertIsNone(result['name'])
        self.assertIsNone(result['email'])
        self.assertEqual(result['projects'], [])
        self.assertTrue(result['warnings'])

    def test_unreadable_text_stops_before_paid_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.txt'
            path.write_text(' ', encoding='utf-8')
            with patch('agents.resume_graph.invoke') as model:
                with self.assertRaisesRegex(ValueError, 'No readable resume'):
                    extract_resume(str(path))
            model.assert_not_called()

    def test_scanned_pdf_keeps_visual_extraction(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            writer.write(str(path))
            facts = ResumeFacts(name='Alex Student', skills=['Python']).model_dump()
            with patch('agents.resume_parser.read_pdf', return_value={'type': 'document', 'source': {}}) as visual, \
                 patch('agents.resume_graph.invoke', return_value=AIMessage(content=json.dumps(facts))) as model:
                result = extract_resume(str(path))
            visual.assert_called_once_with(str(path))
            model.assert_called_once()
            self.assertIsInstance(model.call_args.args[0][1].content, list)
            self.assertEqual(result['name'], 'Alex Student')
