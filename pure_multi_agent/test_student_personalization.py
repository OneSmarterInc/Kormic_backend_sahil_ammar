from unittest import mock

from django.test import TestCase, override_settings

from agents import commons
from django_api.models import StudentProfile, UniversityKnowledgeEntry
from django_api.services import load_profile_data, save_profile_data
from pure_multi_agent import prompts
from pure_multi_agent.tools.profile_tools import build_tools as profile_tools
from pure_multi_agent.tools.university_tools import build_tools as university_tools
from universities.models import University


@override_settings(UNIVERSITY_VECTOR_SEARCH=False)
class StudentPersonalizationTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name="Asha", gpa=3.4, budget=12000)
        self.other_student = StudentProfile.objects.create(name="Other Student", budget=99000)
        self.university = University.objects.create(name="Example University")
        from django.contrib.auth.models import User
        from accounts.models import Account
        Account.objects.create(user=User.objects.create_user(username='officer'), role='university', university=self.university)
        self.ctx = {"canonical_student_id": str(self.student.uuid), "student_profile": load_profile_data(str(self.student.uuid))}

    @mock.patch("pure_multi_agent.registered_adviser.invoke")
    def test_profile_corrections_and_preferences_reach_university_in_same_turn(self, ask):
        update = next(tool for tool in profile_tools(self.ctx) if tool.name == "update_student_profile")
        update.invoke({"budget": 15000, "career_goals": ["ML researcher"], "preferred_intake": "Fall 2027",
                       "preferred_locations": ["Ohio"], "funding_required": True})
        self.ctx["student_profile"].update({"student_id": "forged", "notes": "PRIVATE_NOTES", "email": "private@example.com"})
        from langchain_core.messages import AIMessage
        ask.return_value = AIMessage(content="Tuition is $12000")
        tool = next(tool for tool in university_tools(self.ctx) if tool.name == "ask_university")
        tool.invoke({"university_id": str(self.university.uuid), "question": "Does this fit my budget?"})
        import json
        context = json.loads(ask.call_args.args[0][0].content.split("Student context (data): ")[1])
        self.assertEqual(context["student_id"], str(self.student.uuid))
        self.assertEqual(context["budget"], 15000)
        self.assertEqual(context["preferences"]["preferred_intake"], "Fall 2027")
        self.assertEqual(context["career_goals"], ["ML researcher"])
        self.assertNotIn("email", context)
        self.assertNotIn("notes", context)
        self.other_student.refresh_from_db()
        self.assertEqual(self.other_student.budget, 99000)

    def test_prompt_uses_students_own_goals_and_preferences(self):
        profile = self.ctx["student_profile"]
        profile.update({"career_goals": ["ML researcher"], "preferences": {"funding_required": True}})
        prompt = prompts.build_runtime_system_prompt(agent_name="My Advisor", student_profile=profile,
                                                     memory={}, response_mode="short")
        self.assertIn("Asha", prompt)
        self.assertIn("ML researcher", prompt)
        self.assertIn("funding_required", prompt)
        self.assertNotIn("Other Student", prompt)

    @mock.patch("agents.commons.get_university_agent")
    def test_fit_cache_refreshes_for_student_and_university_changes(self, get_agent):
        get_agent.return_value.assess_fit.side_effect = lambda context: {
            "match_tier": "target", "match_score": 70, "fit_summary": f"Budget {context['budget']}",
        }
        sid, uid = str(self.student.uuid), str(self.university.uuid)
        self.assertFalse(commons.generate_fit_assessment(sid, uid)["cached"])
        self.assertTrue(commons.generate_fit_assessment(sid, uid)["cached"])
        self.assertEqual(get_agent.return_value.assess_fit.call_count, 1)
        save_profile_data(sid, {"budget": 20000})
        self.assertFalse(commons.generate_fit_assessment(sid, uid)["cached"])
        self.assertEqual(get_agent.return_value.assess_fit.call_args.args[0]["budget"], 20000)
        fact = UniversityKnowledgeEntry.objects.create(university_id=uid, topic="Fees", content="$15000")
        self.assertFalse(commons.generate_fit_assessment(sid, uid)["cached"])
        fact.content = "$18000"
        fact.save()
        self.assertFalse(commons.generate_fit_assessment(sid, uid)["cached"])
        # In-turn changes are used before the runtime saves the whole profile.
        updated = load_profile_data(sid)
        updated["budget"] = 25000
        self.assertFalse(commons.generate_fit_assessment(sid, uid, student_profile=updated)["cached"])
        self.assertEqual(get_agent.return_value.assess_fit.call_args.args[0]["budget"], 25000)
