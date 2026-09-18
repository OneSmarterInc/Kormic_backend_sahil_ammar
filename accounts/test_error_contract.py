import uuid
from django.test import SimpleTestCase, override_settings
from django.urls import path
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, Throttled
from rest_framework.test import APIClient

class ErrorView(APIView):
    authentication_classes = []
    permission_classes = []
    def get(self, request, kind):
        if kind == "validation": raise ValidationError({"email": ["Enter a valid email address."]})
        if kind == "non_field": raise ValidationError("Keep an active owner.")
        if kind == "coded": return Response({"code": "CHAT_TIMEOUT", "message": "Provider secret"}, status=504)
        if kind == "permission": raise PermissionDenied()
        if kind == "throttle": raise Throttled(wait=30)
        if kind == "exception": raise RuntimeError("secret internal password")
        if kind == "explicit": return Response({"status": "failed", "message": "Profile not found.", "error": "debug data"}, status=404)
        if kind == "server": return Response({"message": "private SQL", "error": "secret"}, status=500)
        return Response({"success": True})

urlpatterns = [path("api/test/<str:kind>/", ErrorView.as_view())]

@override_settings(ROOT_URLCONF=__name__)
class ErrorContractTests(SimpleTestCase):
    def test_every_error_has_one_wire_shape_and_matching_request_id(self):
        client = APIClient()
        for kind, status in [("validation", 400), ("non_field", 400), ("coded", 504), ("permission", 403), ("throttle", 429), ("explicit", 404), ("server", 500), ("exception", 500)]:
            with self.subTest(kind=kind):
                response = client.get(f"/api/test/{kind}/")
                self.assertEqual(response.status_code, status)
                payload = response.json()
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(set(payload["error"]), {"code", "message", "details", "request_id"})
                self.assertEqual(payload["error"]["request_id"], response["X-Request-ID"])
                uuid.UUID(payload["error"]["request_id"])
                self.assertNotIn("secret", response.content.decode())
                self.assertNotIn("private SQL", response.content.decode())
        self.assertEqual(client.get("/api/test/validation/").json()["error"]["details"], {"email": ["Enter a valid email address."]})
        self.assertEqual(client.get("/api/test/throttle/")["Retry-After"], "30")
        self.assertEqual(client.get("/api/test/non_field/").json()["error"]["message"], "Keep an active owner.")
        self.assertEqual(client.get("/api/test/coded/").json()["error"]["code"], "CHAT_TIMEOUT")

    def test_django_404_and_success(self):
        client = APIClient()
        self.assertEqual(client.get("/api/no-such-route/").json()["error"]["code"], "NOT_FOUND")
        self.assertEqual(client.get("/api/test/success/").json(), {"success": True})
