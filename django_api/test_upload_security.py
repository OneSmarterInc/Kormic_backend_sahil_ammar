from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status

from django_api.models import LinkedInAnalysis, ResumeUpload
from django_api.tests import make_student_client


class UploadSecurityTests(TestCase):
    def setUp(self):
        self.client, self.student_id = make_student_client(email="upload-security@example.com")
        self.client.post("/api/profile/", {"name": "Upload Security"}, format="json")

    @mock.patch("agents.resume_parser.ResumeParserAgent")
    def test_resume_rejects_unsupported_type(self, mock_parser):
        upload = SimpleUploadedFile("resume.exe", b"not-a-resume", content_type="application/x-msdownload")
        response = self.client.post("/api/profile/resume/", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_parser.assert_not_called()

    @mock.patch("agents.resume_parser.ResumeParserAgent")
    def test_resume_rejects_oversized_file(self, mock_parser):
        upload = SimpleUploadedFile(
            "resume.pdf",
            b"x" * (10 * 1024 * 1024 + 1),
            content_type="application/pdf",
        )
        response = self.client.post("/api/profile/resume/", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        mock_parser.assert_not_called()

    @mock.patch("agents.resume_parser.ResumeParserAgent")
    def test_resume_path_is_stored_relative_to_media_root(self, mock_parser):
        mock_parser.return_value.parse.return_value = {"skills": ["Python"]}
        upload = SimpleUploadedFile("resume.pdf", b"test-resume", content_type="application/pdf")
        response = self.client.post("/api/profile/resume/", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = ResumeUpload.objects.get(pk=response.data["resume_id"])
        self.assertFalse(Path(row.file_path).is_absolute())

    @mock.patch("agents.linkedin_agent.LinkedInAgent")
    def test_linkedin_rejects_unsupported_type(self, mock_agent):
        upload = SimpleUploadedFile("profile.gif", b"gif-data", content_type="image/gif")
        response = self.client.post("/api/profile/linkedin/", {"images": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        mock_agent.assert_not_called()

    @mock.patch("agents.linkedin_agent.LinkedInAgent")
    def test_linkedin_rejects_oversized_image(self, mock_agent):
        upload = SimpleUploadedFile(
            "profile.jpg",
            b"x" * (10 * 1024 * 1024 + 1),
            content_type="image/jpeg",
        )
        response = self.client.post("/api/profile/linkedin/", {"images": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        mock_agent.assert_not_called()

    @mock.patch("agents.linkedin_agent.LinkedInAgent")
    def test_linkedin_paths_are_stored_relative_to_media_root(self, mock_agent):
        mock_agent.return_value.extract.return_value = {"skills": ["Leadership"]}
        upload = SimpleUploadedFile("profile.png", b"image-data", content_type="image/png")
        response = self.client.post("/api/profile/linkedin/", {"images": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = LinkedInAnalysis.objects.get(pk=response.data["analysis_id"])
        self.assertTrue(row.image_paths)
        self.assertTrue(all(not Path(path).is_absolute() for path in row.image_paths))

    @mock.patch("django_api.views.parse_resume", side_effect=RuntimeError(r"C:\\private\\db\\secret"))
    def test_unexpected_resume_error_does_not_leak_exception_text(self, _mock_parse):
        upload = SimpleUploadedFile("resume.pdf", b"test-resume", content_type="application/pdf")
        response = self.client.post("/api/profile/resume/", {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertEqual(response.data["message"], "Something went wrong. Please try again.")
        self.assertNotIn("private", str(response.data).lower())
        self.assertNotIn("secret", str(response.data).lower())
