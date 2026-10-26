import hashlib
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase
from django_api.models import UniversityKnowledgeEntry
from universities.models import University
from url_discovery.models import DiscoveryJob, DiscoveryClusterApproval


class EvidenceReplayTests(TestCase):
    def setUp(self):
        self.university = University.objects.create(
            name='Franklin University', uuid='12ea2efd-73d0-4982-b022-9e76b25060f7')
        self.rows = []
        for i in range(6):
            text = f'Official housing information for source number {i}.'
            self.rows.append({'url': f'https://www.franklin.edu/source-{i}',
                              'text': text, 'characters': len(text),
                              'text_sha256': hashlib.sha256(text.encode()).hexdigest()})
        job = DiscoveryJob.objects.create(university=self.university,
            base_url='https://www.franklin.edu/', status='completed', settings={
                'gate2_import': 'franklin-gate2-20261026T114702Z',
                'source_hashes': {r['url']: r['text_sha256'] for r in self.rows}})
        DiscoveryClusterApproval.objects.create(job=job, category='campus_life',
            urls=[r['url'] for r in self.rows], url_count=6)

    def replay(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            folder = home / 'kormic-gate2'
            folder.mkdir()
            (folder / 'franklin-source-review-20261026T114702Z.json').write_text(
                json.dumps(self.rows), encoding='utf-8')
            with patch('pathlib.Path.home', return_value=home), patch('builtins.print'):
                runpy.run_path(str(settings.BASE_DIR / 'deploy/refresh_franklin_gate2.py'))

    def test_replay_is_idempotent(self):
        self.replay()
        count = UniversityKnowledgeEntry.objects.count()
        self.assertEqual(count, 6)
        self.replay()
        self.assertEqual(UniversityKnowledgeEntry.objects.count(), count)

    def test_modified_evidence_is_rejected_without_writes(self):
        self.rows[-1]['text'] += ' altered'
        with self.assertRaisesMessage(ValueError, 'verification failed'):
            self.replay()
        self.assertFalse(UniversityKnowledgeEntry.objects.exists())
