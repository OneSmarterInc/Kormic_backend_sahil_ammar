from django.test import SimpleTestCase
from django.urls import resolve, Resolver404
from rest_framework.exceptions import ValidationError
from django_api.phone import normalize_phone
from django_api.serializers import ProfileCreateUpdateSerializer


class PhoneNormalizationTests(SimpleTestCase):
    def test_international_and_country_based_numbers(self):
        for value, country, expected in [
            ("98765 43210", "India", "+919876543210"),
            ("(202) 555-0123", "United States", "+12025550123"),
            ("020 7946 0018", "United Kingdom", "+442079460018"),
            ("030 901820", "Germany", "+4930901820"),
            ("+1 202 555 0123", "India", "+12025550123"),
            ("+81 90 1234 5678", "Other", "+819012345678"),
        ]:
            with self.subTest(value=value):
                serializer = ProfileCreateUpdateSerializer(data={"phone": value, "country": country})
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data["phone"], expected)

    def test_invalid_numbers_are_field_errors(self):
        for value in ["12", "+1234567890123456", "call +1 202 555 0123", "+1 202 555 0123 ext 9", "++12025550123"]:
            with self.subTest(value=value):
                serializer = ProfileCreateUpdateSerializer(data={"phone": value, "country": "United States"})
                self.assertFalse(serializer.is_valid())
                self.assertIn("phone", serializer.errors)

    def test_unknown_region_requires_international_prefix(self):
        with self.assertRaises(ValidationError):
            normalize_phone("2025550123", "Other")
        self.assertEqual(normalize_phone("+12025550123"), "+12025550123")

    def test_unrelated_partial_updates_do_not_require_phone(self):
        serializer = ProfileCreateUpdateSerializer(data={"city": "London"})
        self.assertTrue(serializer.is_valid())
        self.assertNotIn("phone", serializer.validated_data)


class RoadmapUnavailableTests(SimpleTestCase):
    def test_unfinished_roadmap_routes_are_not_exposed(self):
        for path in ["/api/roadmap/student/", "/api/roadmap/student/history/"]:
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)
