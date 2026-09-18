from django.test import TestCase

from accounts.serializers import student_onboarding_status
from django_api.models import StudentProfile
from django_api.serializers import ProfileCreateUpdateSerializer
from django_api.services import create_or_update_profile


class BasicInfoPersistenceTests(TestCase):
    def setUp(self):
        self.profile = StudentProfile.objects.create(
            name="Priya Sharma",
            email="priya@example.com",
        )
        self.payload = {
            "student_id": str(self.profile.uuid),
            "name": "Priya Sharma",
            "email": "priya@example.com",
            "phone": "9876543210",
            "date_of_birth": "02/03/2004",
            "city": "Pune",
            "region": "Maharashtra",
            "country": "India",
            "institution": "COEP Technological University, Pune",
            "major": "Computer Science",
            "program": "Bachelor's",
            "year_in_college": "3rd Year",
            "graduation_year": 2027,
            "interests": ["Internship", "Study abroad"],
            "target_degree_or_field": "MS in Computer Science",
        }

    def test_profile_serializer_accepts_all_student_app_basic_info_fields(self):
        serializer = ProfileCreateUpdateSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["phone"], "+919876543210")
        self.assertEqual(serializer.validated_data["date_of_birth"], "02/03/2004")
        self.assertEqual(serializer.validated_data["city"], "Pune")
        self.assertEqual(serializer.validated_data["region"], "Maharashtra")
        self.assertEqual(serializer.validated_data["year_in_college"], "3rd Year")
        self.assertEqual(serializer.validated_data["interests"], ["Internship", "Study abroad"])
        self.assertEqual(serializer.validated_data["target_degree_or_field"], "MS in Computer Science")

    def test_basic_info_payload_is_persisted_and_completion_is_derived_separately(self):
        before = student_onboarding_status(str(self.profile.uuid))
        self.assertTrue(before["profile_exists"])
        self.assertFalse(before["basic_info_complete"])

        serializer = ProfileCreateUpdateSerializer(data=self.payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        create_or_update_profile(serializer.validated_data)

        self.profile.refresh_from_db()
        saved = self.profile.evidence.get("manual_profile_api", {})
        self.assertEqual(saved["phone"], "+919876543210")
        self.assertEqual(saved["date_of_birth"], "02/03/2004")
        self.assertEqual(saved["city"], "Pune")
        self.assertEqual(saved["region"], "Maharashtra")
        self.assertEqual(saved["year_in_college"], "3rd Year")
        self.assertEqual(saved["interests"], ["Internship", "Study abroad"])
        self.assertEqual(saved["target_degree_or_field"], "MS in Computer Science")

        after = student_onboarding_status(str(self.profile.uuid))
        self.assertTrue(after["profile_exists"])
        self.assertTrue(after["basic_info_complete"])
        self.assertFalse(after["setup_complete"])

