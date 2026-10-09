import copy
import json

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pure_multi_agent.chat_cost_controls import compact_json
from pure_multi_agent.prompt_evidence import encode_evidence, compact_chat_messages


def decode(payload):
    if payload.get('encoding') != 'shared-source-text-v1':
        return payload
    def expand(value):
        if isinstance(value, dict) and set(value) == {'$source_text'}:
            return payload['source_text'][value['$source_text']]
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value
    return expand(payload['data'])


class PromptEvidenceTests(SimpleTestCase):
    def test_direct_routes_cover_only_whole_standalone_requests(self):
        from pure_multi_agent.chat_cost_controls import simple_general_intent, standalone_profile_intent
        for text in ('What is an academic transcript?', 'Explain a fellowship in detail.', 'What is a GPA?'):
            self.assertEqual(simple_general_intent(text)['route'], 'general')
        for text in ('What is a GPA at University A?', 'Explain my scholarship eligibility',
                     'What is a fellowship and save my preferences', 'What is its application fee?'):
            self.assertIsNone(simple_general_intent(text))
        for text in ('Review my saved profile', 'How can I improve my academic profile?',
                     'Please assess my profile in detail'):
            self.assertEqual(standalone_profile_intent(text)['route'], 'profile')
        for text in ('Review my profile for University A', 'Review my profile and save changes',
                     'Review my uploaded resume', 'Review his profile', 'How can I improve it?'):
            self.assertIsNone(standalone_profile_intent(text))

    def fixture(self):
        source = 'Tuition USD 12000 per academic year; eligibility and housing charges differ. ' * 35
        return {'profile': {'description': source}, 'courses': [
            {'name': 'Course A', 'details': source, 'year': 2026, 'source_url': 'https://a.edu/course'}],
            'facts': [{'content': source, 'human_verified': True, 'year': 2027,
                       'source_url': 'https://a.edu/verified'},
                      {'content': source + ' Revised fee USD 13000.', 'human_verified': False}],
            'unknown': None, 'empty': [], 'zero': 0, 'flag': False}

    def test_roundtrip_preserves_every_field_and_conflicting_record(self):
        data = self.fixture()
        original = copy.deepcopy(data)
        packed = encode_evidence(data)
        self.assertEqual(decode(json.loads(packed)), original)
        self.assertEqual(data, original)
        self.assertLess(len(packed.encode()), len(compact_json(data).encode()) * .7)

    def test_small_unique_and_reserved_content_remain_plain_json(self):
        for value in ({'content': 'Short'}, {'source': 'Unique ' * 500},
                      {'$source_text': 'user supplied', 'a': 'Long ' * 500, 'b': 'Long ' * 500}):
            self.assertEqual(json.loads(encode_evidence(value)), value)

    def test_only_text_tool_payloads_change_and_ids_protocol_remain_intact(self):
        data = self.fixture()
        messages = [HumanMessage(content='Question'),
                    AIMessage(content='', tool_calls=[{'id': 'one', 'name': 'read', 'args': {}}]),
                    ToolMessage(content=compact_json(data), tool_call_id='one'),
                    ToolMessage(content=[{'type': 'text', 'text': 'Visual evidence'}], tool_call_id='two'),
                    ToolMessage(content='not JSON', tool_call_id='three')]
        original = copy.deepcopy(messages)
        encoded = compact_chat_messages(messages)
        self.assertEqual(messages, original)
        self.assertEqual(encoded[2].tool_call_id, 'one')
        self.assertEqual(decode(json.loads(encoded[2].content)), data)
        for index in (0, 1, 3, 4):
            self.assertEqual(encoded[index], messages[index])

    def test_references_are_local_to_each_source_and_each_request(self):
        first = self.fixture()
        second = {'a': 'Different university USD 24000. ' * 100,
                  'b': 'Different university USD 24000. ' * 100}
        messages = [ToolMessage(content=compact_json(first), tool_call_id='a'),
                    ToolMessage(content=compact_json(second), tool_call_id='b')]
        for original, encoded in zip((first, second), compact_chat_messages(messages)):
            self.assertEqual(decode(json.loads(encoded.content)), original)
        self.assertEqual(decode(json.loads(encode_evidence(second))), second)
