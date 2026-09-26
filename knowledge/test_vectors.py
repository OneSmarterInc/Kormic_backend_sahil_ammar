from unittest import mock, skipUnless

from django.db import connection
from django.test import TestCase, override_settings

from django_api.models import UniversityKnowledgeEntry
from knowledge.university_kb import UniversityKnowledgeBase
from knowledge import vectors


class UniversityRetrievalTests(TestCase):
    def setUp(self):
        self.own = UniversityKnowledgeEntry.objects.create(
            university_id="one", topic="Tuition", content="Annual fee is $12000.", source_type="manual",
        )
        self.other = UniversityKnowledgeEntry.objects.create(
            university_id="two", topic="Tuition", content="Annual fee is $99000.", source_type="manual",
        )

    @mock.patch("knowledge.vectors.search_ids")
    def test_semantic_results_are_merged_without_other_university_data(self, search):
        search.return_value = [self.other.pk, self.own.pk]
        kb = UniversityKnowledgeBase("one")
        self.assertEqual([row.db_id for row in kb.search("cost to attend")], [self.own.pk])
        search.assert_called_once_with("one", "cost to attend", limit=8)

    @override_settings(UNIVERSITY_VECTOR_SEARCH=False)
    def test_usage_alone_does_not_make_unrelated_facts_relevant(self):
        self.own.times_used = 99
        self.own.save()
        self.assertEqual(UniversityKnowledgeBase("one").search("housing"), [])

    @mock.patch("knowledge.vectors.enabled", return_value=True)
    @mock.patch("knowledge.vectors.embed_texts", return_value=[[1.0] + [0.0] * 383])
    def test_index_refreshes_edits_but_not_unchanged_facts_or_other_universities(self, embed, _enabled):
        self.assertEqual(vectors.sync_embeddings("one"), 1)
        self.assertEqual(vectors.sync_embeddings("one"), 0)
        self.other.refresh_from_db()
        self.assertIsNone(self.other.embedding)
        UniversityKnowledgeEntry.objects.filter(pk=self.own.pk).update(content="Annual fee is $14000.")
        self.assertEqual(vectors.sync_embeddings("one"), 1)
        self.assertEqual(embed.call_count, 2)
        self.own.refresh_from_db()
        self.assertEqual(len(self.own.embedding), 384)
        self.assertEqual(self.own.embedding_model, vectors.MODEL)

    @mock.patch("knowledge.vectors.enabled", return_value=True)
    @mock.patch("knowledge.vectors.embed_texts", side_effect=RuntimeError("Encoder offline"))
    def test_embedding_failure_preserves_keyword_answers(self, _embed, _enabled):
        with self.assertLogs("knowledge.vectors", level="ERROR"):
            results = UniversityKnowledgeBase("one").search("tuition")
        self.assertEqual([entry.db_id for entry in results], [self.own.pk])

    @skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL with pgvector")
    @override_settings(UNIVERSITY_VECTOR_SEARCH=True)
    @mock.patch("knowledge.vectors.embed_texts", return_value=[[1.0] + [0.0] * 383])
    def test_postgres_cosine_query_is_university_scoped(self, _embed):
        vectors.sync_embeddings("one")
        vectors.sync_embeddings("two")
        self.assertEqual(vectors.search_ids("one", "cost to attend"), [self.own.pk])
