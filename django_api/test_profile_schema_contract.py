from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

from django_api.serializers import ProfileCreateUpdateSerializer


class StudentProfileSchemaContractTests(SimpleTestCase):
    def test_profile_openapi_fields_match_serializer_exactly(self):
        schema = SchemaGenerator(urlconf="django_api.contract_urls").get_schema(request=None, public=True)
        operation = schema["paths"]["/api/v1/profile/"]["post"]
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        component_name = request_schema["$ref"].rsplit("/", 1)[-1]
        component = schema["components"]["schemas"][component_name]

        documented_fields = set(component.get("properties", {}))
        serializer_fields = set(ProfileCreateUpdateSerializer().fields)

        self.assertEqual(documented_fields, serializer_fields)
        self.assertNotIn("dateOfBirth", documented_fields)
        self.assertNotIn("yearInCollege", documented_fields)
        self.assertIn("date_of_birth", documented_fields)
        self.assertIn("year_in_college", documented_fields)

