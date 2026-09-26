from types import SimpleNamespace
from unittest import mock

from anthropic.types import ToolUseBlock
from django.test import TestCase, override_settings

from agents.tests import _fake_response
from agents.university_agent import UniversityAgent
from agents.university_officer_tools import OfficerData
from django_api.models import StudentProfile, UniversityInterestEvent, FitAssessment, UniversityKnowledgeEntry
from universities.models import University


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class OfficerToolsTests(TestCase):
    def setUp(self):
        self.uni = University.objects.create(name="Test University", eligibility_criteria=[{"criterion": "GPA", "detail": "Minimum 3.0"}])
        self.other = University.objects.create(name="Other University")
        self.good = StudentProfile.objects.create(name="Eligible Student", gpa=3.6)
        self.low = StudentProfile.objects.create(name="Low GPA Student", gpa=2.4)
        self.outside = StudentProfile.objects.create(name="Outside Student", gpa=4.0)
        for student, university in [(self.good, self.uni), (self.low, self.uni), (self.outside, self.other)]:
            UniversityInterestEvent.objects.create(student=student, university_id=str(university.uuid), source="searched")
        self.agent = UniversityAgent(str(self.uni.uuid), auto_scrape=False)
        self.data = OfficerData(self.uni, self.agent.kb)

    def test_qualified_filter_uses_rules_and_interest_scope(self):
        FitAssessment.objects.create(student=self.good, university_id=str(self.uni.uuid), assessment={"match_score": "unknown"})
        result = self.data.execute("interested_students", {"qualification": "qualified"})
        self.assertEqual([r["name"] for r in result["records"]], [self.good.name])
        self.assertEqual(result["qualification_counts"], {"qualified": 1, "not_qualified": 1})
        self.assertEqual(result["records"][0]["eligibility"]["details"][0]["actual"], 3.6)

    def test_detail_and_scope_cannot_be_changed(self):
        self.assertIn("error", self.data.execute("interested_student_detail", {"student_id": str(self.outside.uuid)}))
        self.assertIn("error", self.data.execute("interested_students", {"university_id": str(self.other.uuid)}))
        self.assertIn("error", self.data.execute("interested_students", {"page": 0}))
        detail = self.data.execute("interested_student_detail", {"student_id": str(self.good.uuid)})
        self.assertEqual(detail["profile"]["name"], self.good.name)

    def test_dashboard_and_empty_tools(self):
        self.assertEqual(self.data.execute("university_dashboard", {})["interested_students"], 2)
        self.assertEqual(self.data.execute("university_queries", {})["total"], 0)
        self.assertEqual(self.data.execute("university_exchanges", {})["total"], 0)
        self.assertEqual(self.data.execute("interested_students", {"name": "No match"})["total"], 0)

    def test_missing_academic_data_requires_review(self):
        self.good.gpa = None
        self.good.save()
        result = self.data.execute("interested_students", {"qualification": "unassessed"})
        self.assertEqual([r["name"] for r in result["records"]], [self.good.name])
        self.assertEqual(result["records"][0]["eligibility"]["matched"], 0)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_claude_receives_live_tool_results(self, client):
        client.return_value.messages.create.side_effect = [
            SimpleNamespace(content=[ToolUseBlock(type="tool_use", id="tool_1", name="interested_students", input={"qualification": "qualified"})]),
            _fake_response({"answer": "Eligible Student meets the recorded GPA requirement.", "confidence": 0.95}),
        ]
        before = UniversityKnowledgeEntry.objects.count()
        answer = self.agent.answer("Name interested students qualified for admission", caller_role="officer")
        request = client.return_value.messages.create.call_args.kwargs
        evidence = request["messages"][-1]["content"][0]["content"]
        self.assertIn("Eligible Student", evidence)
        self.assertNotIn("Outside Student", evidence)
        self.assertNotIn("Low GPA Student", evidence)
        self.assertIn("Eligible Student", answer["answer"])
        self.assertEqual(UniversityKnowledgeEntry.objects.count(), before)

    @mock.patch("agents.university_agent._get_anthropic_client")
    def test_student_call_has_no_private_tools(self, client):
        client.return_value.messages.create.return_value = _fake_response({"answer": "Public university information", "confidence": 0.9})
        self.agent.answer("Who are your interested students?", caller_role="student")
        request = client.return_value.messages.create.call_args.kwargs
        self.assertNotIn("tools", request)
        self.assertNotIn("Eligible Student", str(request))
