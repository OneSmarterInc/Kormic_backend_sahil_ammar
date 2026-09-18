import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from django_api.models import KnowledgeSource, KnowledgeChunk, UniversityKnowledgeEntry
from universities.models import University
from knowledge.university_kb import UniversityKnowledgeBase
from knowledge.retrieval import sync_chunks, available, citations, MODEL
from knowledge.freshness import recrawl, fetch_public

class RetrievalTests(TestCase):
    def setUp(self):
        self.u = University.objects.create(name="Evidence University")
        self.uid = str(self.u.uuid)
        self.kb = UniversityKnowledgeBase(self.uid)
    def fact(self, topic="Tuition fees", content="Tuition costs 1000.", source_type="manual", **extra):
        row = UniversityKnowledgeEntry.objects.create(university_id=self.uid, topic=topic, content=content, source_type=source_type, confidence=1, **extra)
        sync_chunks(row); self.kb.reload(); return row
    def test_authority_priority_and_no_popularity_only_match(self):
        human = self.fact(source_type="human_verified", last_verified_at=timezone.now())
        self.fact(source_type="conversation", times_used=999999)
        self.assertEqual(self.kb.search("tuition")[0].db_id, human.pk)
        self.assertEqual(self.kb.search("galacticwalrus"), [])
    def test_stale_disabled_deleted_and_changed_sources_excluded(self):
        row = self.fact(source_type="scraped", source_url="https://example.edu/fees")
        self.assertEqual(self.kb.search("tuition"), [])
        source = KnowledgeSource.objects.create(university=self.u, url=row.source_url, health="healthy", last_success_at=timezone.now())
        self.assertEqual(len(self.kb.search("tuition")), 1)
        for health in ["deleted", "robots_blocked", "changed_pending"]:
            source.health = health; source.save(); self.assertEqual(self.kb.search("tuition"), [])
        source.health="healthy"; source.last_success_at=timezone.now()-timedelta(days=31); source.save()
        self.assertEqual(self.kb.search("tuition"), [])
    def test_tenant_isolation_and_citations(self):
        row = self.fact(source_type="human_verified", last_verified_at=timezone.now(), source_url="https://example.edu/fees")
        other = University.objects.create(name="Other")
        self.assertEqual(UniversityKnowledgeBase(str(other.uuid)).search("tuition"), [])
        source = citations(self.kb.search("tuition"), self.uid)[0]
        self.assertEqual(source['id'], f'knowledge:{row.pk}'); self.assertTrue(source['human_verified']); self.assertIsNotNone(source['last_verified_at'])
    def test_content_edit_invalidates_vectors_even_with_stale_instance(self):
        row = self.fact()
        row.chunks.update(embedding=[1.0]+[0.0]*1023, embedding_model=MODEL)
        UniversityKnowledgeEntry.objects.filter(pk=row.pk).update(content="Tuition costs 2000.")
        sync_chunks(row)
        chunk = row.chunks.get(); self.assertIsNone(chunk.embedding); self.assertIn("2000", chunk.text)
    def test_semantic_synonym_retrieval_on_postgres(self):
        if connection.vendor != "postgresql": self.skipTest("pgvector requires PostgreSQL; covered by CI")
        row = self.fact(topic="Accommodation", content="Residence halls offer private rooms.")
        row.chunks.update(embedding=[1.0]+[0.0]*1023, embedding_model=MODEL)
        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test"}), patch("knowledge.retrieval.embed", return_value=[[1.0]+[0.0]*1023]):
            self.assertEqual(self.kb.search("housing")[0].db_id, row.pk)

class FreshnessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = University.objects.create(name="Fresh University")
        self.s = KnowledgeSource.objects.create(university=self.u, url="https://example.edu/admissions")
        self.robots = patch("knowledge.freshness.robots_policy", return_value=(True, 2, "https://example.edu")).start()
        self.addCleanup(patch.stopall)
    def crawl(self, status=200, html="<p>New deadline January 1</p>", headers=None):
        cache.clear()
        with patch("knowledge.freshness.fetch_public", return_value=(status, headers or {"content-type":"text/html"}, html)):
            result=recrawl(self.s.pk)
        self.s.refresh_from_db(); return result
    @patch("knowledge.scraper.extract_facts_from_page", return_value=[{"topic":"Deadline", "content":"January 1"}])
    def test_hash_change_detection_preserves_human_facts(self, extract):
        human=UniversityKnowledgeEntry.objects.create(university_id=str(self.u.uuid), topic="Reviewed", content="Human", source_type="human_verified", source_url=self.s.url)
        self.assertEqual(self.crawl(), 1); digest=self.s.content_hash
        self.assertEqual(self.crawl(), 0); self.assertEqual(extract.call_count, 1)
        self.assertEqual(self.crawl(html="<p>Changed deadline February 1</p>"), 1)
        self.assertNotEqual(digest, self.s.content_hash); self.assertTrue(UniversityKnowledgeEntry.objects.filter(pk=human.pk).exists())
        self.assertEqual(UniversityKnowledgeEntry.objects.filter(source_type="scraped").count(), 1)
    def test_deleted_source_deactivates_scraped_evidence(self):
        row=UniversityKnowledgeEntry.objects.create(university_id=str(self.u.uuid), topic="Deadline", content="Old", source_type="scraped", source_url=self.s.url)
        self.crawl(410); row.refresh_from_db(); self.assertFalse(row.active); self.assertEqual(self.s.health,"deleted")
    def test_rate_limit_and_robots(self):
        self.crawl(429, headers={"retry-after":"600"}); self.assertEqual(self.s.health,"rate_limited")
        self.assertGreater(self.s.next_fetch_at, timezone.now()+timedelta(seconds=590))
        self.robots.return_value=(False,2,"https://example.edu")
        with patch("knowledge.freshness.fetch_public") as fetch: recrawl(self.s.pk); fetch.assert_not_called()
        self.s.refresh_from_db(); self.assertEqual(self.s.health,"robots_blocked")
    @patch("knowledge.scraper.extract_facts_from_page", side_effect=RuntimeError("provider"))
    def test_changed_page_extraction_failure_withholds_old_facts(self, extract):
        self.s.content_hash="old"; self.s.last_success_at=timezone.now(); self.s.save()
        self.crawl(); self.assertEqual(self.s.health,"changed_pending")
    def test_private_network_blocked_before_connection(self):
        with patch("socket.getaddrinfo", return_value=[(2,1,6,"",("127.0.0.1",443))]), patch("socket.create_connection") as connect:
            with self.assertRaises(ValueError): fetch_public("https://example.edu/")
            connect.assert_not_called()

class CitationTests(TestCase):
    @patch('agents.university_agent._get_anthropic_client')
    def test_model_cannot_invent_citation_ids(self, client):
        import json
        from agents.university_agent import UniversityAgent
        university=University.objects.create(name='Cited',agent_name='Cited Agent')
        agent=UniversityAgent(str(university.uuid),auto_scrape=False)
        agent.kb.store(topic='Tuition',content='Tuition is 1000.',source_type='manual')
        row=UniversityKnowledgeEntry.objects.get(university_id=str(university.uuid),topic='Tuition')
        client.return_value.messages.create.return_value=SimpleNamespace(content=[SimpleNamespace(text=json.dumps({'answer':'1000','confidence':.9,'source_ids':[f'knowledge:{row.pk}','knowledge:999999','https://invented.invalid']}))],usage={'input_tokens':10,'output_tokens':5})
        answer=agent.answer('Tuition?')
        self.assertEqual([s['id'] for s in answer['sources']],[f'knowledge:{row.pk}'])
        self.assertFalse(answer['human_verified'])
    @patch('agents.university_agent._get_anthropic_client')
    def test_no_evidence_cannot_be_high_confidence(self, client):
        from agents.university_agent import UniversityAgent
        university=University.objects.create(name='Empty',agent_name='Empty Agent')
        agent=UniversityAgent(str(university.uuid),auto_scrape=False)
        client.return_value.messages.create.return_value=SimpleNamespace(content=[SimpleNamespace(text='{"answer":"Invented","confidence":1,"source_ids":["knowledge:999"]}')],usage=None)
        result=agent.answer('Scholarships?',caller_role='officer')
        self.assertEqual(result['confidence'],0); self.assertEqual(result['sources'],[])
