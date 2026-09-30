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
