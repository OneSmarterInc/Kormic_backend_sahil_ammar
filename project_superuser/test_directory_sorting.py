from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from institutes.models import Institute
from institutes_list.models import InstituteStudentList, ListedStudent
from universities.models import University


class DirectorySortingTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user('directory-admin')
        Account.objects.create(user=admin, role='superuser')
        TOTPDevice.objects.create(user=admin, secret_encrypted='fixture', confirmed_at=timezone.now())
        self.client = APIClient()
        self.client.force_authenticate(admin)
        for model, role, relation in (
            (University, 'university', 'university'), (Institute, 'institute', 'institute'),
        ):
            for name, officer in [('zeta', 'Amy'), ('Alpha', 'Zoe')]:
                entity = model.objects.create(name=name, country='US' if role == 'university' else 'IN')
                user = User.objects.create_user(f'{role}-{name}', first_name=officer, is_active=officer == 'Amy')
                Account.objects.create(user=user, role=role, **{relation: entity})
                if officer == 'Amy':
                    TOTPDevice.objects.create(user=user, secret_encrypted='fixture', confirmed_at=timezone.now())
                if role == 'institute':
                    source = InstituteStudentList.objects.create(institute=entity, contact_email='office@example.test')
                    ListedStudent.objects.create(
                        source_list=source, institute_id=str(entity.uuid),
                        full_name=name, email=f'{name}@example.test',
                        program_name='' if name == 'Alpha' else 'Zoology', field_of_study='Art',
                    )

    def test_entity_sorting_precedes_pagination_and_matches_displayed_admin(self):
        for endpoint in ('universities', 'institutes'):
            for field, expected in [('name', 'Alpha'), ('admin', 'zeta'), ('status', 'Alpha'), ('totp', 'Alpha')]:
                with self.subTest(endpoint=endpoint, field=field):
                    response = self.client.get(f'/api/superuser/{endpoint}/', {
                        'sort': field, 'direction': 'asc', 'page': 1, 'page_size': 1,
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data['pagination']['total'], 2)
                    self.assertEqual(response.data[endpoint][0]['name'], expected)
                    reverse = self.client.get(f'/api/superuser/{endpoint}/', {
                        'sort': field, 'direction': 'desc', 'page': 1, 'page_size': 1,
                    })
                    self.assertNotEqual(reverse.data[endpoint][0]['name'], expected)

    def test_roster_sorts_fallback_study_and_paginates(self):
        for field in ('name', 'institute', 'study'):
            response = self.client.get('/api/superuser/roster-students/', {
                'sort': field, 'direction': 'asc', 'page_size': 1,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['results'][0]['full_name'], 'Alpha')

    def test_users_sort_with_role_filter(self):
        response = self.client.get('/api/superuser/users/', {
            'role': 'university', 'sort': 'name', 'direction': 'desc', 'page': 1, 'page_size': 1,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['users'][0]['name'], 'Zoe')
        self.assertEqual(response.data['pagination']['total'], 2)

    def test_all_directories_reject_unapproved_sort_fields(self):
        for endpoint in ('universities', 'institutes', 'roster-students', 'users'):
            for params in ({'sort': 'password'}, {'direction': 'sideways'}):
                self.assertEqual(self.client.get(f'/api/superuser/{endpoint}/', params).status_code, 400)
