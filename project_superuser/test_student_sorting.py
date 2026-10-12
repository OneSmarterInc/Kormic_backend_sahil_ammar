from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice
from django_api.models import StudentProfile


class StudentDirectorySortingTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user('sort-admin')
        Account.objects.create(user=admin, role='superuser')
        TOTPDevice.objects.create(
            user=admin, secret_encrypted='fixture', confirmed_at=timezone.now(),
        )
        self.client = APIClient()
        self.client.force_authenticate(admin)
        self.url = '/api/superuser/students/'
        self.users = []
        for index, name in enumerate(('zoe', 'Amy', 'bob')):
            user = User.objects.create_user(
                f'{name}@example.test', email=f'{name}@example.test',
                first_name=name, is_active=index != 0,
                date_joined=timezone.now() - timedelta(days=index),
            )
            profile = StudentProfile.objects.create(
                name=name, email=user.email,
                institution=('Zulu', 'Alpha', 'Beta')[index],
                major=('Zoology', 'Art', 'Biology')[index],
            )
            Account.objects.create(user=user, role='student', student_profile=profile)
            self.users.append(user)
        TOTPDevice.objects.create(
            user=self.users[1], secret_encrypted='fixture', confirmed_at=timezone.now(),
        )

    def test_case_insensitive_sort_is_applied_before_pagination(self):
        names = []
        for page in (1, 2, 3):
            response = self.client.get(self.url, {
                'sort': 'name', 'direction': 'asc', 'page': page, 'page_size': 1,
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['pagination']['total'], 3)
            names.append(response.data['students'][0]['name'])
        self.assertEqual(names, ['Amy', 'bob', 'zoe'])

    def test_each_column_sorts_in_both_directions(self):
        for column in ('institution', 'major', 'status', 'totp', 'joined'):
            with self.subTest(column=column):
                asc = self.client.get(self.url, {'sort': column, 'direction': 'asc'})
                desc = self.client.get(self.url, {'sort': column, 'direction': 'desc'})
                self.assertEqual(asc.status_code, 200)
                self.assertEqual(desc.status_code, 200)
                ids = lambda response: [row['user_id'] for row in response.data['students']]
                self.assertEqual(ids(asc), list(reversed(ids(desc))))
                expected_first = {
                    'institution': self.users[1], 'major': self.users[1],
                    'status': self.users[0], 'totp': self.users[0],
                    'joined': self.users[2],
                }
                self.assertEqual(ids(asc)[0], expected_first[column].pk)

    def test_search_and_sort_combine(self):
        response = self.client.get(self.url, {'search': 'Amy@', 'sort': 'name'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['name'] for row in response.data['students']], ['Amy'])

    def test_invalid_ordering_is_rejected(self):
        for params in ({'sort': 'user__password'}, {'direction': 'invalid'}):
            self.assertEqual(self.client.get(self.url, params).status_code, 400)

    def test_student_cannot_read_directory(self):
        self.client.force_authenticate(self.users[1])
        self.assertEqual(self.client.get(self.url, {'sort': 'name'}).status_code, 403)
