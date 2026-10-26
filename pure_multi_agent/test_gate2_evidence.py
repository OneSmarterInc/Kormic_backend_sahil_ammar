import json

from django.test import SimpleTestCase
from langchain_core.messages import ToolMessage

from knowledge.university_kb import UniversityKnowledgeBase
from pure_multi_agent.officer_context import prepare, PAGE_CHARS
from pure_multi_agent.evidence_preview import focused_preview


class GateTwoEvidenceTests(SimpleTestCase):
    def test_tuition_preview_preserves_level_and_year_with_amount(self):
        text = ('Doctorate Costs 2026-2027 Standard tuition per credit $828. ' +
                'Other doctoral information. ' * 200 +
                "Master's Costs 2026-2027 Standard tuition per credit hour $690. " +
                'Other fees and charges. ' * 80)
        result = focused_preview(json.dumps({'records': [{
            'id': 1, 'topic': 'Tuition', 'content': text,
            'source_url': 'https://example.edu/tuition',
        }]}), 'What is standard master’s tuition per credit for 2026–2027?', PAGE_CHARS)
        excerpt = json.loads(result)['records'][0]['content']
        self.assertIn("Master's Costs 2026-2027", excerpt)
        self.assertIn('$690', excerpt)
        self.assertNotIn('$828', excerpt)

    def test_changed_rate_after_shared_intro_is_not_a_duplicate(self):
        kb = UniversityKnowledgeBase.__new__(UniversityKnowledgeBase)
        prefix = 'Official tuition introduction. ' * 30
        self.assertNotEqual(kb._normalize_key('Fees', prefix + 'Masters $670'),
                            kb._normalize_key('Fees', prefix + 'Masters $690'))

    def test_relevant_source_beyond_prefix_is_visible_with_citation(self):
        source = 'https://example.edu/orientation'
        records = [{'id': 1, 'topic': 'General', 'content': 'Unrelated degree listing. ' * 500}]
        text = ('Navigation links. ' * 400 +
                'International Student Orientation is delivered online in one day. ' * 10)
        records.append({'id': 2, 'topic': 'Orientation', 'content': text,
                        'source_url': source, 'source_type': 'scraped'})
        original = json.dumps({'records': records})
        message = ToolMessage(content=original, tool_call_id='read', name='search_university_knowledge')
        projected, tools = prepare({'current_message': 'How is International Student Orientation delivered?'}, [message], [])
        wrapper = json.loads(projected[0].content)
        preview = json.loads(wrapper['content'])
        self.assertTrue(wrapper['question_focused'])
        self.assertEqual(wrapper['next_offset'], 0)
        self.assertLessEqual(len(wrapper['content']), PAGE_CHARS)
        selected = preview['records'][0]
        self.assertEqual(selected['source_url'], source)
        self.assertIn('delivered online', selected['content'])
        start = selected['excerpt_offset']
        self.assertEqual(selected['content'], text[start:start + len(selected['content'])])
        reader = next(tool for tool in tools if tool.name == 'read_officer_evidence')
        self.assertEqual(reader.invoke({'evidence_id': wrapper['evidence_id']})['content'], original[:PAGE_CHARS])
        self.assertEqual(message.content, original)
