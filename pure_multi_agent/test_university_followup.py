from django.test import TestCase
from langchain_core.messages import AIMessage,HumanMessage
from django_api.models import StudentProfile
from university_research.models import UniversitySearch
from pure_multi_agent.university_followup import restore_selection,selection_number
from pure_multi_agent.tools.university_tools import numbered_search_results


class FollowupTests(TestCase):
    def test_pronoun_followup_reuses_owned_previous_selection_without_model(self):
        from django_api.models import AgentJob
        from university_research.models import PublicUniversity
        from pure_multi_agent.university_followup import restore_university_followup
        row=PublicUniversity.objects.create(identity_key='ucf-followup',name='University of Central Florida',website='https://ucf.edu/')
        AgentJob.objects.create(owner_key='followup',idempotency_key='previous',kind='student',student_id=str(self.student.uuid),status='completed',
            payload={'message_id':10,'resume_state':{'university_candidates':['public:'+str(row.pk)]}})
        ctx={**self.ctx,'current_message_id':11,'current_message':'all the scholarship options in that university'}
        self.assertTrue(restore_university_followup(ctx))
        self.assertEqual(ctx['university_candidates'],['public:'+str(row.pk)])
        ctx['current_message']='their scholarships at Yale University'
        self.assertFalse(restore_university_followup(ctx))

    def setUp(self):
        self.student=StudentProfile.objects.create(name='Selection student')
        self.ctx={'canonical_student_id':str(self.student.uuid),'current_message':'the first one','turn_id':'new'}
        self.search=UniversitySearch.objects.create(student=self.student,query='University of Cincinnati',candidates={'universities':[
            {'name':'University of Cincinnati','website':'https://www.uc.edu/'},
            {'name':'Athletics','website':'https://gobearcats.com/'}]})
        self.messages=[HumanMessage(content='Courses and fees at University of Cincinnati'),
            AIMessage(content='Which university do you mean?\n1. https://www.uc.edu/\n2. https://gobearcats.com/'),HumanMessage(content='the first one')]

    def test_selection_restores_exact_search_and_original_question(self):
        self.assertEqual(restore_selection(self.ctx,self.messages),1)
        self.assertEqual(self.ctx['university_search_id'],str(self.search.pk))
        self.assertEqual(self.ctx['university_question'],self.messages[0].content)

    def test_not_reused_after_unrelated_reply_or_for_another_student(self):
        self.assertIsNone(restore_selection(self.ctx,[self.messages[0],AIMessage(content='Hello'),self.messages[-1]]))
        other=StudentProfile.objects.create(name='Other')
        self.ctx['canonical_student_id']=str(other.uuid)
        self.assertIsNone(restore_selection(self.ctx,self.messages))

    def test_repeated_choice_keeps_original_question(self):
        messages=[*self.messages,self.messages[1],HumanMessage(content='the first one')]
        self.assertEqual(restore_selection(self.ctx,messages),1)
        self.assertEqual(self.ctx['university_question'],self.messages[0].content)

    def test_ordinal_choices_and_athletics_filter(self):
        self.assertEqual(selection_number('the first one'),1)
        self.assertEqual(selection_number('option 2'),2)
        self.assertEqual(selection_number('second'),2)
        self.assertIsNone(selection_number('the first one is wrong'))
        rows=numbered_search_results([{'url':'https://www.uc.edu/','title':'University of Cincinnati'},
            {'url':'https://gobearcats.com/','title':'University of Cincinnati Athletics - Official Website'}])
        self.assertEqual([r['result_number'] for r in rows],[1])
