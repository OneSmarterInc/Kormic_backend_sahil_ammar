"""Savings must preserve evidence, ownership, fallback and honest usage accounting."""
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .grouped import messages_for
from .inference import Inference, InvalidResponse
from .overview import synthesize_overview
from .prompt_context import compact_sources, compact_history
from .source_rules import eligible


class PromptTests(SimpleTestCase):
    def test_duplicate_excerpts_are_lossless_and_repository_local(self):
        sources = [{'id': n, 'path': f'src/{n}.py', 'excerpt': 'identical code\n' * 200,
                    'url': f'https://github.com/owner/repo/blob/sha/src/{n}.py'} for n in (1, 2, 3)]
        sources[2]['excerpt'] += 'different ending'
        original = copy.deepcopy(sources)
        compact = compact_sources(sources)
        by_id = {source['id']: source for source in compact}
        restored = [source.get('excerpt', by_id.get(source.get('excerpt_ref'), {}).get('excerpt'))
                    for source in compact]
        self.assertEqual(restored, [source['excerpt'] for source in sources])
        self.assertEqual(sources, original)
        self.assertEqual(compact_sources([sources[1]])[0]['excerpt'], sources[1]['excerpt'])
        self.assertNotIn('excerpt_ref', compact[2])

    def test_grouped_payload_is_smaller_and_keeps_paths_ids_and_scope(self):
        sources = [{'id': n, 'path': f'src/{n}.py', 'excerpt': 'same code\n' * 300,
                    'url': 'https://github.com/owner/repo/blob/sha/source.py'} for n in (1, 2, 3)]
        payload = {'repository_id': 10, 'sources': sources, 'available_paths': ['src/more.py'],
                   'contribution': {'status': 'sampled', 'linked_commits_in_sample': 2,
                                    'commit_urls': ['https://github.com/owner/repo/commit/sha'],
                                    'note': 'Not total authorship.'}}
        original = copy.deepcopy(payload)
        message = messages_for([{'payload': payload}])[-1]['content']
        compact = json.loads(message)['repositories'][0]
        self.assertLess(len(message), len(json.dumps({'repositories': [payload]})) / 2)
        self.assertEqual([s['id'] for s in compact['sources']], [1, 2, 3])
        self.assertEqual(compact['available_paths'], ['src/more.py'])
        self.assertEqual(compact['contribution']['note'], 'Not total authorship.')
        self.assertEqual(payload, original)

    def test_history_keeps_errors_but_does_not_repeat_source_text(self):
        event = {'tool': 'read_files', 'arguments': {'paths': ['src/a.py']},
                 'result': {'sources': [{'id': 1, 'path': 'src/a.py', 'excerpt': 'CODE', 'url': 'URL'},
                                        {'path': 'src/b.py', 'error': 'Unavailable'}]}, 'provider': 'claude'}
        original = copy.deepcopy(event)
        history = compact_history([event, event])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['result']['sources'], [{'id': 1, 'path': 'src/a.py'},
                                                         {'path': 'src/b.py', 'error': 'Unavailable'}])
        self.assertEqual(event, original)

    def test_generated_assets_and_lockfiles_are_excluded_but_real_source_is_kept(self):
        for path in ('package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml',
                     'src/api_pb2.py', 'src/client.generated.ts', 'www/app.min.js',
                     '.next/server/app.js', 'coverage/report.json', 'src/model.g.dart'):
            with self.subTest(path=path):
                self.assertFalse(eligible(path))
        for path in ('package.json', 'src/generator.py', 'src/lock.ts', 'src/main.py',
                     'tests/test_app.py', 'pyproject.toml', 'requirements.txt'):
            self.assertTrue(eligible(path))


class OverviewTests(SimpleTestCase):
    def test_no_paid_summary_preserves_findings_and_shared_ownership_guidance(self):
        chat = Mock(side_effect=AssertionError('No overview inference is allowed'))
        summary = 'Detailed validated summary. ' * 40
        projects = [{'id': 1, 'full_name': 'team/project', 'owner_login': 'team', 'fork': False,
                     'analysis': {'summary': summary, 'skills': [{'name': 'Python'}], 'domains': ['Web']}}]
        text = synthesize_overview({'login': 'ada'}, {'owned': 0}, [], projects, {'note': 'Sampled.'}, chat)
        chat.assert_not_called()
        self.assertIn(summary, text)
        self.assertIn('shared repository', text)
        self.assertIn('personally implemented', text)
        self.assertIn('Sampled.', text)
        self.assertIn('did not execute tests', text)
        self.assertIn('Python', text)


class UsageTests(SimpleTestCase):
    @patch('pure_multi_agent.telemetry.emit')
    @patch('github_profiles.inference.model_slot', return_value=nullcontext())
    @patch('agents.github_agent._get_anthropic_client')
    def test_actual_usage_logged_once_even_when_paid_response_is_invalid(self, client, slot, emit):
        client.return_value.messages.create.return_value = SimpleNamespace(id='request-test',
            usage=SimpleNamespace(input_tokens=1200, output_tokens=120,
                                  cache_read_input_tokens=200, cache_creation_input_tokens=50),
            content=[SimpleNamespace(text='{"summary":42}')])
        run = SimpleNamespace(pk='run-test', stage='agent',
                              profile=SimpleNamespace(student=SimpleNamespace(uuid='student-test')))
        with self.assertRaises(InvalidResponse):
            Inference(run).chat([{'role': 'user', 'content': '{}'}],
                {'type': 'object', 'properties': {'summary': {'type': 'string'}}})
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], 'GITHUB_INFERENCE_USAGE')
        self.assertEqual(emit.call_args.kwargs['run_id'], 'run-test')
        self.assertEqual(emit.call_args.kwargs['outputs']['usage']['input_tokens'], 1200)
        self.assertEqual(emit.call_args.kwargs['outputs']['usage']['cache_creation_input_tokens'], 50)
        self.assertTrue(emit.call_args.kwargs['outputs']['usage_available'])
