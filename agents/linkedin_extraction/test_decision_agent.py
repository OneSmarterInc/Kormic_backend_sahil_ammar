import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from .decision_agent import DecisionPhotoAgent, parse_action
from .validation import validate_data
from .schemas import ImageObservation
from .image_extractor import reread_image_region


def response(payload):
    return SimpleNamespace(content=json.dumps(payload))


class DecisionLoopTests(SimpleTestCase):
    def test_text_workflow_extracts_without_paid_planning(self):
        llm = self.model([], {'education': [{'institution': 'City College', 'degree': 'Diploma',
                                           'evidence': 'City College\nDiploma'}]})
        agent = DecisionPhotoAgent(llm, validate_data)
        result, calls = agent.extract('City College\nDiploma', 'EDUCATION', plan_locally=True)
        self.assertEqual(len(result.education), 1)
        self.assertEqual(calls, 1)
        self.assertEqual([e['action'] for e in agent.trace], ['extract', 'finish'])

    def test_text_planning_covers_every_chunk_and_preserves_busy_signal(self):
        from github_profiles.scheduling import CapacityBusy
        llm = Mock(invoke=Mock(side_effect=CapacityBusy()))
        with self.assertRaises(CapacityBusy):
            DecisionPhotoAgent(llm, validate_data).extract('City College\nDiploma', 'EDUCATION', plan_locally=True)
        self.assertEqual(llm.invoke.call_count, 1)
        source = 'City College\nDiploma\n' + 'Supporting detail ' * 400
        agent = DecisionPhotoAgent(llm, validate_data, max_steps=1)
        with patch('agents.linkedin_extraction.decision_agent.AutonomousPhotoAgent') as worker:
            worker.return_value.extract.return_value = (ImageObservation(education=[
                {'institution': 'City College', 'evidence': 'City College'}]), 1)
            worker.return_value.warnings = []
            agent.extract(source, 'EDUCATION', plan_locally=True)
        from .decision_agent import chunks
        self.assertEqual(worker.return_value.extract.call_count, len(list(chunks(source))))
        self.assertEqual(agent.evidence_source, source)

    def test_busy_capacity_is_not_retried_as_bad_extraction(self):
        from github_profiles.scheduling import CapacityBusy
        model = Mock(invoke=Mock(side_effect=CapacityBusy()))
        with self.assertRaises(CapacityBusy):
            DecisionPhotoAgent(model, validate_data).extract('City College\nDiploma', 'EDUCATION')
        self.assertEqual(model.invoke.call_count, 1)

    def test_busy_extractor_propagates_through_controller(self):
        from github_profiles.scheduling import CapacityBusy
        model = Mock(invoke=Mock(side_effect=[response({'action':'extract','chunk':0}), CapacityBusy()]))
        with self.assertRaises(CapacityBusy):
            DecisionPhotoAgent(model, validate_data).extract('City College\nDiploma', 'EDUCATION')
        self.assertEqual(model.invoke.call_count, 2)

    def model(self, actions, payload):
        actions = iter(actions)
        def invoke(messages, **kwargs):
            return response(next(actions)) if messages[0][1].startswith("You control") else response(payload)
        return Mock(invoke=Mock(side_effect=invoke))

    def test_model_selects_inspect_then_extract_then_finish_with_feedback(self):
        actions = [{"action": "inspect", "chunk": 0}, {"action": "extract", "chunk": 0}, {"action": "finish"}]
        llm = self.model(actions, {"education": [{"institution": "City College", "degree": "Diploma", "evidence": "City College\nDiploma"}]})
        agent = DecisionPhotoAgent(llm, validate_data)
        result, calls = agent.extract("City College\nDiploma", "EDUCATION")
        self.assertEqual(len(result.education), 1)
        self.assertEqual([e["action"] for e in agent.trace], ["inspect", "extract", "finish"])
        self.assertEqual(calls, 4)
        last_state = json.loads(llm.invoke.call_args.args[0][1][1])
        self.assertEqual(last_state["validated_counts"]["education"], 1)
        self.assertEqual(last_state["required_chunks_remaining"], [])

    def test_model_selects_ocr_crop_to_recover_blank_image(self):
        actions = [{"action": "retry_ocr", "region": "bottom", "mode": "sparse"},
                   {"action": "extract", "chunk": 0}, {"action": "finish"}]
        source = "Cloud Certificate\nAcme"
        llm = self.model(actions, {"certifications": [{"name": "Cloud Certificate", "issuer": "Acme", "evidence": source}],
                                   "experiences": [{"title": "Cloud Certificate", "company": "Acme", "evidence": source}]})
        retry = Mock(return_value=source)
        events = []
        agent = DecisionPhotoAgent(llm, validate_data)
        result, _ = agent.extract("", "CERTIFICATES", reread=retry, on_event=lambda trace: events.append(trace))
        retry.assert_called_once_with("bottom", "sparse")
        self.assertEqual(len(result.certifications), 1)
        self.assertEqual(result.experiences, [])
        self.assertEqual(agent.evidence_source, source)
        self.assertEqual(events[-1][-1]["action"], "finish")

    def test_premature_finish_rejected_then_model_changes_action(self):
        llm = self.model([{"action": "finish"}, {"action": "extract", "chunk": 0}, {"action": "finish"}],
                         {"education": [{"institution": "City College", "degree": "Diploma", "evidence": "City College\nDiploma"}]})
        agent = DecisionPhotoAgent(llm, validate_data)
        result, _ = agent.extract("City College\nDiploma", "EDUCATION")
        self.assertEqual(agent.trace[0]["outcome"], "rejected")
        self.assertEqual(len(result.education), 1)

    def test_unsupported_action_is_rejected_and_fallback_is_explicit(self):
        llm = self.model([{"action": "run_shell", "command": "anything"}] * 2,
                         {"education": [{"institution": "City College", "degree": "Diploma", "evidence": "City College\nDiploma"}]})
        agent = DecisionPhotoAgent(llm, validate_data)
        result, _ = agent.extract("City College\nDiploma", "EDUCATION")
        self.assertEqual(len(result.education), 1)
        self.assertTrue(any(e["action"] == "fallback" for e in agent.trace))
        self.assertTrue(agent.warnings)

    def test_no_supported_results_requests_clearer_image_not_empty_success(self):
        llm = self.model([{"action": "request_clearer_image"}], {})
        agent = DecisionPhotoAgent(llm, validate_data)
        with self.assertRaisesRegex(ValueError, "clearer image"):
            agent.extract("", "PROFILE")
        self.assertEqual(agent.trace[-1]["outcome"], "needs_user_input")

    def test_repeated_actions_are_bounded_and_remaining_source_is_checked(self):
        llm = self.model([{"action": "inspect", "chunk": 0}] * 6,
                         {"education": [{"institution": "City College", "degree": "Diploma", "evidence": "City College\nDiploma"}]})
        agent = DecisionPhotoAgent(llm, validate_data, max_steps=6)
        result, calls = agent.extract("City College\nDiploma", "EDUCATION")
        self.assertEqual(len(result.education), 1)
        self.assertLessEqual(calls, 7)
        self.assertEqual(sum(e["action"] == "inspect" for e in agent.trace), 2)

    def test_budget_preserves_validated_results_and_records_limit(self):
        llm = self.model([{"action": "extract", "chunk": 0}],
                         {"education": [{"institution": "City College", "degree": "Diploma", "evidence": "City College\nDiploma"}]})
        agent = DecisionPhotoAgent(llm, validate_data, max_calls=2)
        result, calls = agent.extract("City College\nDiploma", "EDUCATION")
        self.assertEqual(calls, 2)
        self.assertEqual(len(result.education), 1)
        self.assertTrue(any(e["action"] == "limit" for e in agent.trace))

    def test_arguments_cannot_escape_tool_allowlist(self):
        for action in [{"action": "retry_ocr", "region": "../../secret", "mode": "sparse"},
                       {"action": "extract", "chunk": -1}, {"action": "extract", "chunk": True},
                       {"action": "retry_ocr", "region": "full", "mode": "sparse", "path": "/secret"}]:
            with self.assertRaises(ValueError):
                parse_action(json.dumps(action))
        with patch("agents.linkedin_extraction.image_extractor.Image.open") as opened:
            with self.assertRaises(ValueError):
                reread_image_region("/not-opened", "full", "--arbitrary-option")
            opened.assert_not_called()

    def test_empty_or_malformed_controller_never_discards_unread_chunks(self):
        source = "City College\nDiploma\n" + "Supporting text " * 250
        llm = Mock()
        llm.invoke.return_value = SimpleNamespace(content="not json")
        agent = DecisionPhotoAgent(llm, validate_data, max_calls=20)
        with patch("agents.linkedin_extraction.decision_agent.AutonomousPhotoAgent") as worker:
            worker.return_value.extract.return_value = (ImageObservation(education=[{"institution": "City College", "evidence": "City College"}]), 1)
            worker.return_value.warnings = []
            agent.extract(source, "EDUCATION")
            self.assertGreater(worker.return_value.extract.call_count, 1)
            self.assertTrue(all(e["action"] == "fallback_extract" for e in agent.trace if "chunk" in e))
