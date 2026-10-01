from types import SimpleNamespace as Obj
from django.test import SimpleTestCase
from unittest.mock import patch
from contextlib import nullcontext
from university_research.claude_fallback import citation_pages

class ClaudeEvidenceTests(SimpleTestCase):
    def test_full_provider_answer_is_retained_separately_from_citation_snippets(self):
        from university_research.claude_fallback import search_official_evidence
        text='Fees: $12000. Seats: 100. Scholarships: need-based grants. Housing and amenities: campus facilities.'
        import json
        response=Obj(content=[Obj(type='text',text=json.dumps({'name':'Example University','official_website':'https://example.edu/','answer':text}),citations=[])])
        with patch('university_research.claude_fallback.Anthropic') as client, patch('university_research.claude_fallback.model_slot',return_value=nullcontext()), patch('university_research.claude_fallback.shared_slot',return_value=nullcontext()), patch('university_research.claude_fallback.operation',return_value=nullcontext({})), patch('university_research.claude_fallback.emit'):
            client.return_value.messages.create.return_value=response
            result=search_official_evidence('https://example.edu/','fees seats scholarships housing amenities')
        self.assertEqual(result['provider_answer']['text'],text)
        self.assertEqual(result['evidence_provider'],'claude_direct')
        self.assertEqual(result['content'],text)
        client.return_value.messages.create.assert_called_once()
        self.assertNotIn('tools',client.return_value.messages.create.call_args.kwargs)

    def test_only_provider_citations_on_the_official_domain_are_accepted(self):
        def citation(url,quote): return Obj(type='web_search_result_location',url=url,cited_text=quote,title='Official source')
        response=Obj(content=[Obj(type='text',text='Uncited fee 99999',citations=[]), Obj(type='text',citations=[citation('https://www.ox.ac.uk/fees','Official fees excerpt'),citation('https://fake-ox.ac.uk/fees','Fake fee'),citation('https://www.ox.ac.uk.evil.test/fees','Fake fee')])])
        pages=citation_pages(response,'ox.ac.uk')
        self.assertEqual(len(pages),1)
        self.assertEqual(pages[0]['content'],'Official fees excerpt')
        self.assertEqual(pages[0]['evidence_provider'],'claude_web_search')


class SingleResearchCallTests(SimpleTestCase):
    def test_same_task_reuses_result_and_blocks_other_domain(self):
        import json
        from university_research.claude_fallback import search_official_evidence
        ctx = {}
        response = Obj(content=[Obj(type='text', text=json.dumps({'name':'Example University',
            'official_website':'https://example.edu/', 'answer':'Programmes and fees.', 'catalogue':{'description':'Example'}}))])
        with patch('university_research.claude_fallback.Anthropic') as client, \
             patch('university_research.claude_fallback.model_slot', return_value=nullcontext()), \
             patch('university_research.claude_fallback.shared_slot', return_value=nullcontext()), \
             patch('university_research.claude_fallback.operation', return_value=nullcontext({})), \
             patch('university_research.claude_fallback.emit'):
            client.return_value.messages.create.return_value = response
            first = search_official_evidence('https://example.edu/', ctx=ctx)
            self.assertEqual(search_official_evidence('https://example.edu/fees', ctx=ctx), first)
            with self.assertRaisesRegex(ValueError, 'single Claude'):
                search_official_evidence('https://another.edu/', ctx=ctx)
        client.return_value.messages.create.assert_called_once()

    def test_readable_page_is_extracted_locally(self):
        from langchain_core.messages import AIMessage
        from university_research.claude_fallback import search_official_evidence
        with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='{"description":"Physics programmes"}')) as model, \
             patch('university_research.claude_fallback.Anthropic') as client:
            page = search_official_evidence('https://example.edu/', source_page={'url':'https://example.edu/', 'content':'Physics programmes'})
        self.assertTrue(model.call_args.kwargs['local_only'])
        self.assertEqual(page['catalogue']['description'], 'Physics programmes')
        client.assert_not_called()
