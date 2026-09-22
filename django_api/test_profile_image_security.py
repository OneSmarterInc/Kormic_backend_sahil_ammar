import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework import status

from django_api.models import StudentProfile, UniversityInterestEvent
from django_api.tests import make_student_client, make_university_client


def _png_upload(name="avatar.png", content_type="image/png"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


class ProfileImageSecurityTests(TestCase):
    def setUp(self):
        self.student, self.student_id = make_student_client(email="image-student@example.com")
        self.officer_a, self.university_a_id = make_university_client(
            email="image-officer-a@example.edu", university_id="image-university-a"
        )
        self.officer_b, self.university_b_id = make_university_client(
            email="image-officer-b@example.edu", university_id="image-university-b"
        )

    def test_valid_png_is_reencoded_with_server_generated_name(self):
        response = self.student.post(
            "/api/profile/image/", {"image": _png_upload("attacker-name.svg")}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        path = StudentProfile.objects.get(uuid=self.student_id).profile_image_path
        self.assertTrue(path.endswith(".png"))
        self.assertNotIn("attacker-name", path)

    def test_svg_is_rejected(self):
        svg = SimpleUploadedFile(
            "avatar.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/svg+xml",
        )
        response = self.student.post("/api/profile/image/", {"image": svg}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_spoofed_svg_declared_as_png_is_rejected(self):
        svg = SimpleUploadedFile(
            "avatar.png",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            content_type="image/png",
        )
        response = self.student.post("/api/profile/image/", {"image": svg}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_upload_over_five_mb_is_rejected(self):
        oversized = SimpleUploadedFile(
            "avatar.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png"
        )
        response = self.student.post("/api/profile/image/", {"image": oversized}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    def test_failed_replacement_keeps_existing_image(self):
        first = self.student.post(
            "/api/profile/image/", {"image": _png_upload()}, format="multipart"
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        old_path = StudentProfile.objects.get(uuid=self.student_id).profile_image_path

        invalid = SimpleUploadedFile("bad.png", b"not an image", content_type="image/png")
        failed = self.student.post(
            "/api/profile/image/", {"image": invalid}, format="multipart"
        )
        self.assertEqual(failed.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            StudentProfile.objects.get(uuid=self.student_id).profile_image_path,
            old_path,
        )

    def test_download_uses_safe_headers(self):
        self.student.post(
            "/api/profile/image/", {"image": _png_upload()}, format="multipart"
        )
        response = self.student.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Content-Disposition"].startswith("attachment;"))

    def test_officer_from_other_university_gets_404(self):
        self.student.post(
            "/api/profile/image/", {"image": _png_upload()}, format="multipart"
        )
        student = StudentProfile.objects.get(uuid=self.student_id)
        UniversityInterestEvent.objects.create(
            student=student, university_id=self.university_a_id, source="test"
        )

        allowed = self.officer_a.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)

        denied = self.officer_b.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(denied.status_code, status.HTTP_404_NOT_FOUND)
