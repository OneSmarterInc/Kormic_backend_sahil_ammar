from django.test import TestCase
from django.contrib.auth.models import User
from django.core.cache import cache
from rest_framework.test import APIClient
from accounts.models import Account
from accounts.tokens import RefreshToken
from accounts.mfa import create_mfa_session, get_mfa_session
from project_superuser.tests import make_superuser_client
from universities.models import University
from institutes.models import Institute


class AccountControlsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_superuser_client()

    def test_create_two_admins_share_existing_organization(self):
        for role, model, country in [('university', University, 'US'), ('institute', Institute, 'IN')]:
            organization = model.objects.create(name='Shared organization', country=country)
            for number in range(2):
                email = f'{role}{number}@example.com'
                response = self.admin.post('/api/superuser/users/', {
                    'name': 'Additional admin', 'email': email, 'password': 'Str0ng-Unique-Pass!',
                    'role': role, f'{role}_id': str(organization.uuid),
                }, format='json')
                self.assertEqual(response.status_code, 201, response.data)
                account = Account.objects.get(user__email=email)
                self.assertEqual(getattr(account, f'{role}_id'), organization.pk)
                self.assertEqual(account.role, role)
                self.assertTrue(account.user.check_password('Str0ng-Unique-Pass!'))
                login = APIClient().post('/api/auth/login/', {'email': email, 'password': 'Str0ng-Unique-Pass!'}, format='json')
                self.assertTrue(login.data['must_enroll_totp'])
            self.assertEqual(model.objects.filter(name='Shared organization').count(), 1)

    def test_creation_rejects_missing_institution_and_duplicate_email(self):
        base = {'name': 'Admin', 'email': 'root@example.com', 'password': 'Str0ng-Unique-Pass!', 'role': 'university'}
        response = self.admin.post('/api/superuser/users/', base, format='json')
        self.assertEqual(response.status_code, 400)
        base['email'] = 'new@example.com'
        self.assertEqual(self.admin.post('/api/superuser/users/', base, format='json').status_code, 400)

    def test_organization_search(self):
        University.objects.create(name='Searchable university', country='US')
        result = self.admin.get('/api/superuser/users/organizations/?role=university&search=Searchable')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data['results']), 1)
        self.assertEqual(APIClient().get('/api/superuser/users/organizations/?role=university').status_code, 401)

    def test_deactivated_login_only_discloses_status_after_correct_password(self):
        for role in ['student', 'university', 'institute', 'superuser', 'department']:
            user = User.objects.create_user(username=role, email=f'{role}@example.com', password='Str0ng-Unique-Pass!', is_active=False)
            Account.objects.create(user=user, role=role)
            client = APIClient()
            response = client.post('/api/auth/login/', {'email': user.email, 'password': 'Str0ng-Unique-Pass!'}, format='json')
            self.assertEqual(response.status_code, 403, response.data)
            self.assertEqual(response.data['code'], 'account_deactivated')
            response = client.post('/api/auth/login/', {'email': user.email, 'password': 'wrong'}, format='json')
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('deactivated', str(response.data))

    def test_revocation_invalidates_every_access_refresh_and_pending_mfa(self):
        user = User.objects.create_user(username='target', email='target@example.com', password='Str0ng-Unique-Pass!')
        Account.objects.create(user=user, role='university')
        tokens = [RefreshToken.for_user(user), RefreshToken.for_user(user)]
        pending = create_mfa_session(user.pk)
        result = self.admin.post(f'/api/superuser/users/{user.pk}/revoke-sessions/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(get_mfa_session(pending), {})
        for token in tokens:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
            self.assertEqual(client.get('/api/auth/me/').status_code, 401)
            self.assertEqual(APIClient().post('/api/auth/refresh/', {'refresh': str(token)}, format='json').status_code, 401)
        fresh = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {fresh.access_token}')
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)

