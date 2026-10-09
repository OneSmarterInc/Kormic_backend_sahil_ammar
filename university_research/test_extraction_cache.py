import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from langchain_core.messages import AIMessage

from university_research.extraction_cache import get_or_extract
from university_research.models import PublicPageExtraction


class PublicExtractionCacheTests(TestCase):
    def test_catalogue_reuses_same_document_and_keeps_current_page_provenance(self):
        from university_research.claude_fallback import extract_catalogue_locally

        text = 'Physics programmes and admission requirements are available.'
        with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(
                content=json.dumps({'description': 'Physics programmes'}))) as model:
            first = extract_catalogue_locally('https://first.edu/', 'physics',
                {'url': 'https://first.edu/physics', 'content': text})
            second = extract_catalogue_locally('https://second.edu/', 'admissions',
                {'url': 'https://second.edu/admissions', 'content': text})

        model.assert_not_called()
        self.assertEqual(first['catalogue'], second['catalogue'])
        self.assertEqual(second['url'], 'https://second.edu/admissions')
        cached = PublicPageExtraction.objects.get()
        self.assertEqual(cached.source_url, 'https://first.edu/physics')
        self.assertIsNotNone(cached.retrieved_at)
        self.assertTrue(cached.supporting_excerpts)
        self.assertTrue(all(excerpt in text for excerpt in cached.supporting_excerpts))
        self.assertNotIn('student', json.dumps(cached.result).lower())

    def test_changed_page_prompt_schema_or_model_misses_cache(self):
        calls = []

        def extract():
            calls.append(1)
            return {'fact': 'public requirement'}

        base = dict(content='Current official requirements', source_url='https://first.edu/',
                    extract=extract, validate=lambda result: None)
        versions = [
            ('schema-1', 'prompt-1', 'model-1'),
            ('schema-1', 'prompt-1', 'model-1'),
            ('schema-1', 'prompt-2', 'model-1'),
            ('schema-2', 'prompt-2', 'model-1'),
            ('schema-2', 'prompt-2', 'model-2'),
        ]
        for schema, instructions, model in versions:
            get_or_extract(**base, schema_version=schema,
                instructions_version=instructions, model_version=model)
        get_or_extract(**{**base, 'content': 'Updated official requirements'},
            schema_version='schema-2', instructions_version='prompt-2', model_version='model-2')

        self.assertEqual(len(calls), 5)
        self.assertEqual(PublicPageExtraction.objects.count(), 5)

    def test_failed_validation_is_not_cached(self):
        with self.assertRaisesRegex(ValueError, 'unsupported'):
            get_or_extract(content='Source', schema_version='schema',
                instructions_version='prompt', model_version='model',
                source_url='https://first.edu/', extract=lambda: {'fact': 'invented'},
                validate=lambda result: (_ for _ in ()).throw(ValueError('unsupported')))
        self.assertFalse(PublicPageExtraction.objects.exists())

    def test_registered_fact_extractor_reuses_verified_public_facts(self):
        from knowledge.scraper import extract_facts_from_page

        source = 'The programme requires IELTS 7.0.'
        response = SimpleNamespace(content=[SimpleNamespace(text=json.dumps([{
            'topic': 'English requirement', 'content': 'IELTS 7.0 is required.',
            'confidence': 0.9, 'source_quote': source,
        }]))])
        with patch('knowledge.scraper._get_anthropic_client') as provider:
            provider.return_value.messages.create.return_value = response
            first = extract_facts_from_page('https://first.edu/english', source, 'First University')
            second = extract_facts_from_page('https://second.edu/english', source, 'Second University')

        self.assertIn(source, first[0]['content'])
        self.assertIn(source, second[0]['content'])
        provider.assert_not_called()
        self.assertFalse(PublicPageExtraction.objects.exists())

    def test_unquoted_registered_fact_uses_source_excerpt_and_is_not_cached(self):
        from knowledge.scraper import extract_facts_from_page

        response = SimpleNamespace(content=[SimpleNamespace(text=json.dumps([{
            'topic': 'Tuition', 'content': 'Tuition is listed.', 'confidence': 0.9,
        }]))])
        with patch('knowledge.scraper._get_anthropic_client') as provider:
            provider.return_value.messages.create.return_value = response
            result = extract_facts_from_page('https://first.edu/fees', 'Tuition is listed.', 'First University')
            extract_facts_from_page('https://first.edu/fees', 'Tuition is listed.', 'First University')

        provider.assert_not_called()
        self.assertFalse(PublicPageExtraction.objects.exists())
        self.assertIn('Page excerpt: Tuition is listed.', result[0]['content'])
        self.assertLess(result[0]['confidence'], 0.9)
