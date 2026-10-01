from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from accounts.models import Account, TOTPDevice
from universities.models import University, KnowledgeGroup
from django_api.models import PendingQuery
from cryptography.fernet import Fernet

@override_settings(TOTP_SECRET_KEYS=(Fernet.generate_key().decode(),), PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DepartmentAccessTests(TestCase):
    def setUp(self):
        self.uni = University.objects.create(name='Department test university')
        self.other = University.objects.create(name='Other university')
        self.money = KnowledgeGroup.objects.create(university=self.uni, slug='money')
        self.admissions = KnowledgeGroup.objects.create(university=self.uni, slug='admissions')
        self.foreign = KnowledgeGroup.objects.create(university=self.other, slug='money')
        self.admin = self.make_user('admin', 'university')
        self.staff = self.make_user('staff', 'department')
        self.staff.account.departments.add(self.money)
        self.allowed = PendingQuery.objects.create(university_id=str(self.uni.uuid), question='Fees?', group=self.money)
        self.denied = PendingQuery.objects.create(university_id=str(self.uni.uuid), question='Admissions?', group=self.admissions)
        self.client = APIClient()
        self.client.force_authenticate(self.staff)

    def make_user(self, name, role):
        user=User.objects.create_user(name, email=name+'@example.com', password='Test-password!234', first_name=name.title())
        Account.objects.create(user=user, role=role, university=self.uni)
        TOTPDevice.objects.create(user=user, secret='JBSWY3DPEHPK3PXP', confirmed_at=timezone.now())
        return user

    def test_staff_only_lists_assigned_department(self):
        for suffix in ['', 'active/']:
            response=self.client.get(f'/api/university/{self.uni.uuid}/queries/{suffix}')
            self.assertEqual(response.status_code,200)
            self.assertEqual([q['query_id'] for q in response.data['queries']], [self.allowed.pk])
        self.assertEqual(self.client.get(f'/api/university/{self.other.uuid}/queries/').status_code,403)

    def test_staff_cannot_change_other_department_or_admin_data(self):
        self.assertEqual(self.client.post(f'/api/queries/{self.denied.pk}/ignore/', {}, format='json').status_code,404)
        self.assertEqual(self.client.get('/api/university-admin/profile/').status_code,403)
        self.assertEqual(self.client.post('/api/university-admin/department-users/', {}, format='json').status_code,403)
        self.assertEqual(self.client.delete(f'/api/queries/{self.allowed.pk}/').status_code,403)

    def test_author_is_taken_from_authenticated_user(self):
        response=self.client.post(f'/api/queries/{self.allowed.pk}/ignore/', {'ignored_by':'Impersonated person'}, format='json')
        self.assertEqual(response.status_code,200)
        self.allowed.refresh_from_db()
        self.assertEqual(self.allowed.answered_by,'Staff')

    def test_create_user_hashes_password_and_scopes_departments(self):
        self.client.force_authenticate(self.admin)
        response=self.client.post('/api/university-admin/department-users/', {'name':'Financial officer','email':'officer@example.com','password':'Complicated-pass!314159','departments':['money']},format='json')
        self.assertEqual(response.status_code,201,response.data)
        user=User.objects.get(email='officer@example.com')
        self.assertTrue(user.check_password('Complicated-pass!314159'))
        self.assertEqual(user.account.role,'department')
        self.assertEqual(list(user.account.departments.all()),[self.money])
        self.assertNotIn('password',response.data)

    def test_unsupported_department_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response=self.client.post('/api/university-admin/department-users/',{'name':'New','email':'new@example.com','password':'Complicated-pass!314159','departments':['campus_life']},format='json')
        self.assertEqual(response.status_code,400)
        self.assertFalse(User.objects.filter(email='new@example.com').exists())

    def test_agent_queries_are_scoped_and_answers_cannot_cross_department(self):
        from agent_queries.models import AgentConversation, AgentQuery
        from django_api.models import StudentProfile
        conv=AgentConversation.objects.create(student=StudentProfile.objects.create(name='Applicant'), university=self.uni)
        allowed=AgentQuery.objects.create(conversation=conv, group=self.money, direction='student_to_university', question='Fees?', question_hash='fees',raised_by_agent='Student',recipient_agent='University')
        denied=AgentQuery.objects.create(conversation=conv, group=self.admissions, direction='student_to_university', question='Admissions?',question_hash='admissions',raised_by_agent='Student',recipient_agent='University')
        response=self.client.get('/api/agent-queries/')
        self.assertEqual(response.status_code,200)
        self.assertEqual([r['id'] for r in response.data['results']],[allowed.pk])
        response=self.client.post(f'/api/agent-queries/{denied.pk}/answer/',{'answer':'Hidden','confirmed':True},format='json')
        self.assertEqual(response.status_code,404)
        with patch('agent_queries.services.notify_query'), patch('agent_queries.services.message'):
            response=self.client.post(f'/api/agent-queries/{allowed.pk}/answer/',{'answer':'Correct fees','confirmed':True},format='json')
        self.assertEqual(response.status_code,200,response.data)
        allowed.refresh_from_db()
        self.assertEqual(allowed.answered_by,self.staff)

    def test_raise_query_requires_interest_and_uses_logged_in_university(self):
        from django_api.models import StudentProfile, UniversityInterestEvent
        from agent_queries.models import AgentQuery
        student=StudentProfile.objects.create(name='Applicant')
        self.client.force_authenticate(self.admin)
        response=self.client.post('/api/agent-queries/',{'student_id':str(student.uuid),'question':'What is your graduation year?'},format='json')
        self.assertEqual(response.status_code,403)
        UniversityInterestEvent.objects.create(student=student,university_id=str(self.uni.uuid),source='searched')
        with patch('agent_queries.services.notify_query'), patch('agent_queries.services.message'):
            response=self.client.post('/api/agent-queries/',{'student_id':str(student.uuid),'question':'What is your graduation year?'},format='json')
        self.assertEqual(response.status_code,201,response.data)
        self.assertEqual(AgentQuery.objects.get(pk=response.data['query_id']).conversation.university,self.uni)

    def test_combined_student_inbox_filters_departments_search_and_archive(self):
        from agent_queries.models import AgentConversation, AgentQuery
        from django_api.models import StudentProfile
        conv=AgentConversation.objects.create(student=StudentProfile.objects.create(name='Applicant'), university=self.uni)
        agent=AgentQuery.objects.create(conversation=conv,group=self.money,direction='student_to_university',question='More fees?',question_hash='more',raised_by_agent='Student',recipient_agent='University')
        base='/api/agent-queries/?include_department_queries=true&direction=student_to_university'
        response=self.client.get(base+'&status=unanswered')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['pagination']['total'],2)
        self.assertEqual({(r['record_type'],r['id']) for r in response.data['results']},{('agent',agent.pk),('department',self.allowed.pk)})
        self.assertEqual(self.client.get(base+'&status=unanswered&search=Admissions').data['results'],[])
        self.allowed.status='ignored';self.allowed.save()
        archived=self.client.get(base+'&status=archived')
        self.assertEqual([r['id'] for r in archived.data['results']],[self.allowed.pk])
        sent=self.client.get('/api/agent-queries/?include_department_queries=true&direction=university_to_student&status=unanswered')
        self.assertEqual(sent.data['results'],[])

    def test_combined_inbox_paginates_without_duplicate_or_missing_rows(self):
        for index in range(12):
            PendingQuery.objects.create(university_id=str(self.uni.uuid),question=f'Fee question {index}',group=self.money)
        base='/api/agent-queries/?include_department_queries=true&direction=student_to_university&status=unanswered'
        first=self.client.get(base).data
        second=self.client.get(base+'&page=2').data
        self.assertEqual(first['pagination']['total'],13)
        self.assertEqual(len(first['results']),10)
        self.assertEqual(len(second['results']),3)
        self.assertEqual(len({r['id'] for r in first['results']+second['results']}),13)
