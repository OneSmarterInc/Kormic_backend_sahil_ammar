from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from university_research.models import PublicUniversity
from university_research.topic_research import scholarship_gap,research_scholarships
from university_research.catalogue import save_catalogue
from university_research.test_catalogue_flow import page_fixture


class TopicTests(TestCase):
    def setUp(self):
        self.row=PublicUniversity.objects.create(identity_key='topic-test',name='Example',website='https://example.edu/')
        save_catalogue(self.row,page_fixture())
        self.row.refresh_from_db()

    def test_generic_scholarship_note_is_not_complete_coverage(self):
        self.assertTrue(scholarship_gap(self.row,'all scholarship options in that university'))
        self.assertFalse(scholarship_gap(self.row,'courses'))

    def test_targeted_attempt_fallback_merge_and_reuse(self):
        award={'name':'Merit award','amount':'USD 1000','eligibility':'First year','deadline':'N/A','application_url':'https://example.edu/awards'}
        page=page_fixture();page['catalogue']={'scholarships':[award]}
        with patch('university_research.web.search_official_site',return_value=[{'url':'https://example.edu/awards'}]), \
             patch('university_research.web.read_page_once',side_effect=ValueError('403')) as read, \
             patch('university_research.claude_fallback.search_official_evidence',return_value=page) as fallback:
            research_scholarships({},self.row)
        read.assert_called_once_with('https://example.edu/awards')
        fallback.assert_called_once()
        self.row.refresh_from_db()
        self.assertEqual(self.row.courses.count(),1)
        self.assertEqual(self.row.coverage['catalogue_profile']['scholarships'][0]['name'],'Merit award')
        self.assertFalse(scholarship_gap(self.row,'all scholarship options'))

    def test_common_agent_retrieves_without_model_selecting_tool(self):
        from pure_multi_agent.registered_adviser import _consult
        from django_api.models import StudentProfile
        from langchain_core.messages import AIMessage
        student=StudentProfile.objects.create(name='Topic student')
        ctx={'canonical_student_id':str(student.uuid),'student_profile':{},'turn_id':'topic'}
        with patch('pure_multi_agent.registered_adviser.invoke',return_value=AIMessage(content='Saved scholarship details')) as model, \
             patch('agents.student_context.university_context',return_value={}):
            result=_consult(ctx,self.row,'scholarships',public_row=self.row)
        self.assertEqual(result['answer'],'Saved scholarship details')
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1],[])
        self.assertTrue(any(m.type=='tool' for m in model.call_args.args[0]))
