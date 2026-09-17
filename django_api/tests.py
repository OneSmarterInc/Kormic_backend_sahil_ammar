from unittest import mock

import pyotp
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from agents import commons as agents_commons
from django_api.models import FitAssessment, ResumeUpload, StudentProfile


def _reset_inprocess_agent_caches():
    # agents.commons keeps plain module-level dict caches (_university_agents /
    # _profile_presenters) that persist across TestCase classes within the
    # same test process -- clear them so a mocked agent from one test doesn't
    # leak into another via the get_*_agent() cache lookup. There is no
    # per-student context cache to clear on the pure_multi_agent chat path --
    # it loads student_profile/memory/agent_name fresh from the database on
    # every turn.
    agents_commons._university_agents.clear()
    agents_commons._profile_presenters.clear()


def _enroll_totp_and_get_tokens(client, *, email, password="S3curePassw0rd!"):
    access = client.post("/api/auth/login/", {"email": email, "password": password}, format="json").data["access"]
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    secret = client.post("/api/auth/totp/enroll/").data["secret"]
    code = pyotp.TOTP(secret).now()
    client.post("/api/auth/totp/verify-enrollment/", {"code": code}, format="json")

    client.credentials()
    mfa_token = client.post("/api/auth/login/", {"email": email, "password": password}, format="json").data[
        "mfa_token"
    ]
    code = pyotp.TOTP(secret).now()
    tokens = client.post("/api/auth/verify-totp/", {"mfa_token": mfa_token, "code": code}, format="json").data
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return tokens


def _register_and_enroll(client, *, role, email, password="S3curePassw0rd!", **extra):
    payload = {"email": email, "password": password, "role": role, "name": "Test User"}
    payload.update(extra)
    client.post("/api/auth/register/", payload, format="json")
    return _enroll_totp_and_get_tokens(client, email=email, password=password)


def make_student_client(email="student_a@example.com"):
    from accounts.models import Account

    client = APIClient()
    _register_and_enroll(client, role="student", email=email)
    uuid = str(Account.objects.get(user__email=email).student_profile.uuid)
    return client, uuid


def make_university_client(email="officer_a@wsu.edu", university_id="wright_state_cs", password="S3curePassw0rd!"):
    """
    Universities can no longer self-register (see accounts.serializers.
    RegisterSerializer) -- only a superuser can create the single admin
    account for a university, via POST /api/superuser/universities/. Tests
    provision that fixture directly instead of going through the superuser
    HTTP API, since most callers only care about a ready-to-use university
    client and a deterministic university_id.
    """
    from django.contrib.auth.models import User

    from accounts.models import Account
    from universities.identity import ensure_agent_name
    from universities.models import University

    university, _created = University.objects.get_or_create(name=university_id)
    ensure_agent_name(university)

    if not Account.objects.filter(university=university, role=Account.Role.UNIVERSITY).exists():
        admin_user = User.objects.create_user(username=email, email=email, password=password)
        Account.objects.create(user=admin_user, role=Account.Role.UNIVERSITY, university=university)

    client = APIClient()
    _enroll_totp_and_get_tokens(client, email=email, password=password)
    return client, str(university.uuid)


class OwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.student_a, self.student_a_id = make_student_client(email="a@example.com")
        self.student_b, self.student_b_id = make_student_client(email="b@example.com")
        self.officer_wsu, self.wsu_id = make_university_client(email="officer1@wsu.edu", university_id="wright_state_cs")
        self.officer_franklin, self.franklin_id = make_university_client(email="officer1@franklin.edu", university_id="franklin_cs")

    def test_student_can_create_and_read_own_profile(self):
        resp = self.student_a.post("/api/profile/", {"name": "Alice"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["student_id"], self.student_a_id)

        get_resp = self.student_a.get(f"/api/profile/{self.student_a_id}/")
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)

    def test_student_cannot_read_other_students_profile(self):
        self.student_a.post("/api/profile/", {"name": "Alice"}, format="json")
        resp = self.student_b.get(f"/api/profile/{self.student_a_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_client_supplied_student_id_is_ignored(self):
        resp = self.student_a.post(
            "/api/profile/", {"student_id": self.student_b_id, "name": "Alice"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["student_id"], self.student_a_id)
        # student B's profile is untouched -- the write went to A's row.
        self.assertNotEqual(StudentProfile.objects.get(uuid=self.student_b_id).name, "Alice")

    def test_program_and_english_score_are_saved(self):
        resp = self.student_a.post(
            "/api/profile/",
            {
                "name": "Sakshi Bhagat",
                "program": "Computer Science",
                "english_score": 75,
                "english_score_text": "75",
                "toefl": 56,
                "ielts": 89,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["profile"]["program"], "Computer Science")
        self.assertEqual(resp.data["profile"]["english_score_text"], "75")

        # A second update with different values should overwrite, not stick
        # to the first POST's values.
        resp = self.student_a.post(
            "/api/profile/", {"program": "MS Computer Science", "english_score": 90}, format="json"
        )
        self.assertEqual(resp.data["profile"]["program"], "MS Computer Science")
        self.assertEqual(resp.data["profile"]["english_score_text"], "90")

    def test_blank_numeric_fields_do_not_400(self):
        resp = self.student_a.post(
            "/api/profile/",
            {
                "name": "Alice",
                "graduation_year": "",
                "gpa": "",
                "gre_quant": "",
                "gre_verbal": "",
                "toefl": "",
                "ielts": "",
                "budget": "",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_university_officer_can_read_own_dashboard(self):
        resp = self.officer_wsu.get(f"/api/university/{self.wsu_id}/profiles/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_university_officer_cannot_read_other_universitys_dashboard(self):
        resp = self.officer_wsu.get(f"/api/university/{self.franklin_id}/profiles/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_student_token_rejected_on_university_only_endpoint(self):
        resp = self.student_a.get(f"/api/university/{self.wsu_id}/profiles/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_university_token_rejected_on_student_only_endpoint(self):
        self.student_a.post("/api/profile/", {"name": "Alice"}, format="json")
        resp = self.officer_wsu.get(f"/api/profile/{self.student_a_id}/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_requests_now_rejected(self):
        anon = APIClient()
        resp = anon.get(f"/api/profile/{self.student_a_id}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class ChatHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        _reset_inprocess_agent_caches()
        self.student, self.student_id = make_student_client(email="c@example.com")
        self.student.post("/api/profile/", {"name": "Carol"}, format="json")

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_agent_chat_persists_and_returns_history(self, mock_run_turn):
        mock_run_turn.side_effect = [
            ("Nova", "Hi there!"),
            ("Nova", "Sure, here's more info."),
        ]

        self.student.post("/api/chat/agent/", {"message": "Hello"}, format="json")
        self.student.post("/api/chat/agent/", {"message": "Tell me more"}, format="json")

        history = self.student.get("/api/chat/agent/history/")
        self.assertEqual(history.status_code, status.HTTP_200_OK)
        self.assertEqual(history.data["count"], 4)
        senders = [m["sender"] for m in history.data["messages"]]
        self.assertEqual(senders, ["user", "assistant", "user", "assistant"])

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_new_chat_clears_history_and_conversation_state(self, mock_run_turn):
        # Checked through the checkpointer's public get_tuple()/delete_thread()
        # interface rather than a backend-specific internal (e.g. MemorySaver's
        # .storage dict) so this test doesn't care which checkpointer backend
        # pure_multi_agent.runtime is configured with -- see runtime.py's
        # _build_checkpointer().
        from pure_multi_agent.runtime import _checkpointer, seed_conversation

        mock_run_turn.side_effect = [("Nova", "Hi there!")]
        self.student.post("/api/chat/agent/", {"message": "Hello"}, format="json")
        self.assertEqual(self.student.get("/api/chat/agent/history/").data["count"], 2)

        seed_conversation(self.student_id, [("user", "Hello"), ("assistant", "Hi there!")])
        config = {"configurable": {"thread_id": self.student_id}}
        self.assertIsNotNone(_checkpointer.get_tuple(config))

        resp = self.student.post("/api/chat/agent/new/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["messages_deleted"], 2)
        self.assertEqual(self.student.get("/api/chat/agent/history/").data["count"], 0)
        self.assertIsNone(_checkpointer.get_tuple(config))

    def test_new_chat_does_not_touch_other_students_history(self):
        other, other_id = make_student_client(email="other-new-chat@example.com")
        other.post("/api/profile/", {"name": "Other"}, format="json")

        from django_api.models import ChatMessage

        ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=self.student_id, sender="user", content="mine"
        )
        ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=other_id, sender="user", content="theirs"
        )

        self.student.post("/api/chat/agent/new/")

        self.assertEqual(self.student.get("/api/chat/agent/history/").data["count"], 0)
        self.assertEqual(other.get("/api/chat/agent/history/").data["count"], 1)


class ChatEditAndAttachmentTests(TestCase):
    def setUp(self):
        cache.clear()
        _reset_inprocess_agent_caches()
        self.student, self.student_id = make_student_client(email="e@example.com")
        self.student.post("/api/profile/", {"name": "Erin"}, format="json")

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_agent_chat_with_image_attachment_is_saved_and_sent_to_model(self, mock_run_turn):
        mock_run_turn.return_value = ("Nova", "I see the screenshot.")

        image = SimpleUploadedFile("screenshot.png", b"fake-png-bytes", content_type="image/png")
        resp = self.student.post(
            "/api/chat/agent/", {"message": "Check this out", "attachments": [image]}, format="multipart"
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["attachments"]), 1)
        self.assertEqual(resp.data["attachments"][0]["filename"], "screenshot.png")

        from django_api.models import ChatAttachment

        self.assertEqual(ChatAttachment.objects.count(), 1)

        # The model should have received an image content block, not just text.
        call_args, call_kwargs = mock_run_turn.call_args
        self.assertEqual(call_args[1], "Check this out")
        self.assertTrue(call_kwargs["image_blocks"])
        self.assertEqual(call_kwargs["image_blocks"][0]["type"], "image")

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_agent_chat_rejects_unsupported_attachment_type(self, mock_run_turn):
        bad_file = SimpleUploadedFile("virus.exe", b"whatever", content_type="application/x-msdownload")
        resp = self.student.post(
            "/api/chat/agent/", {"message": "hi", "attachments": [bad_file]}, format="multipart"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_run_turn.assert_not_called()

        from django_api.models import ChatMessage

        self.assertEqual(ChatMessage.objects.filter(student_id=self.student_id).count(), 0)

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_edit_message_truncates_and_regenerates(self, mock_run_turn):
        mock_run_turn.side_effect = [
            ("Nova", "First reply"),
            ("Nova", "Second reply"),
            ("Nova", "Regenerated reply"),
        ]

        self.student.post("/api/chat/agent/", {"message": "First question"}, format="json")
        self.student.post("/api/chat/agent/", {"message": "Second question"}, format="json")

        from django_api.models import ChatMessage

        first_user_msg = (
            ChatMessage.objects.filter(student_id=self.student_id, sender=ChatMessage.Sender.USER)
            .order_by("created_at")
            .first()
        )

        edit_resp = self.student.patch(
            f"/api/chat/agent/{first_user_msg.id}/edit/", {"message": "Edited question"}, format="json"
        )
        self.assertEqual(edit_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(edit_resp.data["reply"], "Regenerated reply")

        history = self.student.get("/api/chat/agent/history/").data["messages"]
        # The stale second question/reply pair is gone -- only the edited
        # question and its fresh regenerated reply remain.
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["content"], "Edited question")
        self.assertIsNotNone(history[0]["edited_at"])
        self.assertEqual(history[1]["content"], "Regenerated reply")

    def test_edit_message_requires_ownership(self):
        other, other_id = make_student_client(email="f@example.com")
        other.post("/api/profile/", {"name": "Frank"}, format="json")

        from django_api.models import ChatMessage

        msg = ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT, student_id=other_id, sender=ChatMessage.Sender.USER, content="theirs"
        )

        resp = self.student.patch(f"/api/chat/agent/{msg.id}/edit/", {"message": "hijack"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("pure_multi_agent.runtime.run_turn")
    def test_attachment_download_requires_ownership(self, mock_run_turn):
        mock_run_turn.return_value = ("Nova", "ok")
        image = SimpleUploadedFile("shot.png", b"bytes", content_type="image/png")
        resp = self.student.post("/api/chat/agent/", {"message": "hi", "attachments": [image]}, format="multipart")
        attachment_id = resp.data["attachments"][0]["id"]

        other, _ = make_student_client(email="g@example.com")
        other.post("/api/profile/", {"name": "Gina"}, format="json")

        own_resp = self.student.get(f"/api/chat/agent/attachments/{attachment_id}/")
        self.assertEqual(own_resp.status_code, status.HTTP_200_OK)

        other_resp = other.get(f"/api/chat/agent/attachments/{attachment_id}/")
        self.assertEqual(other_resp.status_code, status.HTTP_403_FORBIDDEN)


class SubResourceHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        _reset_inprocess_agent_caches()
        self.student, self.student_id = make_student_client(email="d@example.com")
        self.student.post("/api/profile/", {"name": "Dave"}, format="json")
        self.officer_wsu, self.wsu_id = make_university_client(email="officer2@wsu.edu", university_id="wright_state_cs")
        self.officer_franklin, self.franklin_id = make_university_client(email="officer2@franklin.edu", university_id="franklin_cs")

    @mock.patch("agents.resume_parser.ResumeParserAgent")
    def test_resume_upload_history_accumulates_without_overwriting_files(self, MockParser):
        MockParser.return_value.parse.return_value = {"skills": ["Python"]}

        file1 = SimpleUploadedFile("resume.pdf", b"first-version", content_type="application/pdf")
        file2 = SimpleUploadedFile("resume.pdf", b"second-version", content_type="application/pdf")

        self.student.post("/api/profile/resume/", {"file": file1}, format="multipart")
        self.student.post("/api/profile/resume/", {"file": file2}, format="multipart")

        rows = ResumeUpload.objects.filter(student__uuid=self.student_id)
        self.assertEqual(rows.count(), 2)
        file_paths = {r.file_path for r in rows}
        self.assertEqual(len(file_paths), 2)  # distinct on-disk paths, no clobbering

        resp = self.student.get(f"/api/profile/{self.student_id}/resumes/")
        self.assertEqual(resp.data["count"], 2)

    @mock.patch("agents.resume_parser.ResumeParserAgent")
    def test_resume_download_scoped_to_owner_and_delete_removes_it(self, MockParser):
        MockParser.return_value.parse.return_value = {"skills": ["Python"]}
        file1 = SimpleUploadedFile("resume.pdf", b"resume-bytes", content_type="application/pdf")
        self.student.post("/api/profile/resume/", {"file": file1}, format="multipart")
        resume_id = ResumeUpload.objects.get(student__uuid=self.student_id).id

        download_resp = self.student.get(f"/api/profile/resume/{resume_id}/")
        self.assertEqual(download_resp.status_code, status.HTTP_200_OK)

        other_student, _ = make_student_client(email="e@example.com")
        forbidden_resp = other_student.get(f"/api/profile/resume/{resume_id}/")
        self.assertEqual(forbidden_resp.status_code, status.HTTP_403_FORBIDDEN)

        delete_resp = self.student.delete(f"/api/profile/resume/{resume_id}/")
        self.assertEqual(delete_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ResumeUpload.objects.filter(id=resume_id).exists())

    def test_resume_detail_404_for_missing_resume(self):
        resp = self.student.get("/api/profile/resume/999999/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @mock.patch("agents.linkedin_agent.LinkedInAgent")
    def test_linkedin_image_history_download_and_ownership(self, MockLinkedInAgent):
        MockLinkedInAgent.return_value.extract.return_value = {"skills": ["Leadership"]}

        image = SimpleUploadedFile("screenshot.png", b"fake-image-bytes", content_type="image/png")
        upload_resp = self.student.post("/api/profile/linkedin/", {"images": image}, format="multipart")
        self.assertEqual(upload_resp.status_code, status.HTTP_200_OK)
        analysis_id = upload_resp.data["analysis_id"]
        self.assertTrue(
            upload_resp.data["images"][0]["uploaded_image_url"].endswith(
                f"/api/profile/linkedin/{analysis_id}/images/0/"
            )
        )

        history_resp = self.student.get(f"/api/profile/{self.student_id}/linkedin-history/")
        self.assertEqual(history_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(history_resp.data["analyses"][0]["id"], analysis_id)
        self.assertEqual(len(history_resp.data["analyses"][0]["images"]), 1)

        download_resp = self.student.get(f"/api/profile/linkedin/{analysis_id}/images/0/")
        self.assertEqual(download_resp.status_code, status.HTTP_200_OK)

        out_of_range_resp = self.student.get(f"/api/profile/linkedin/{analysis_id}/images/5/")
        self.assertEqual(out_of_range_resp.status_code, status.HTTP_404_NOT_FOUND)

        other_student, _ = make_student_client(email="f@example.com")
        forbidden_resp = other_student.get(f"/api/profile/linkedin/{analysis_id}/images/0/")
        self.assertEqual(forbidden_resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_profile_image_upload_replace_download_delete_and_officer_visibility(self):
        image1 = SimpleUploadedFile("avatar.png", b"first-image-bytes", content_type="image/png")
        upload_resp = self.student.post("/api/profile/image/", {"image": image1}, format="multipart")
        self.assertEqual(upload_resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            upload_resp.data["profile_image_url"].endswith(f"/api/profile/{self.student_id}/image/")
        )

        first_path = StudentProfile.objects.get(uuid=self.student_id).profile_image_path
        self.assertNotEqual(first_path, "")

        download_resp = self.student.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(download_resp.status_code, status.HTTP_200_OK)

        # Officers can view any student's picture (dashboard roster use case).
        officer_resp = self.officer_wsu.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(officer_resp.status_code, status.HTTP_200_OK)

        # Another student is forbidden.
        other_student, _ = make_student_client(email="g@example.com")
        forbidden_resp = other_student.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(forbidden_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Re-uploading replaces rather than accumulating, and removes the old file.
        image2 = SimpleUploadedFile("avatar2.png", b"second-image-bytes", content_type="image/png")
        self.student.post("/api/profile/image/", {"image": image2}, format="multipart")
        second_path = StudentProfile.objects.get(uuid=self.student_id).profile_image_path
        self.assertNotEqual(first_path, second_path)
        from pathlib import Path
        self.assertFalse(Path(first_path).exists())

        # Officers cannot delete a student's picture -- owner only.
        officer_delete_resp = self.officer_wsu.delete(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(officer_delete_resp.status_code, status.HTTP_403_FORBIDDEN)

        delete_resp = self.student.delete(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(delete_resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(StudentProfile.objects.get(uuid=self.student_id).profile_image_path, "")

        missing_resp = self.student.get(f"/api/profile/{self.student_id}/image/")
        self.assertEqual(missing_resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_profile_image_upload_rejects_non_image_file(self):
        not_an_image = SimpleUploadedFile("resume.pdf", b"pdf-bytes", content_type="application/pdf")
        resp = self.student.post("/api/profile/image/", {"image": not_an_image}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_fit_assessment_history_dual_mode_visibility(self):
        # Fit assessments are only ever produced by the student's personal
        # agent via agents.commons.generate_fit_assessment (chat-triggered,
        # no direct student-facing POST endpoint anymore) -- create the rows
        # directly here to test the read-only history/detail views.
        student = StudentProfile.objects.get(uuid=self.student_id)
        FitAssessment.objects.create(
            student=student, university_id=self.wsu_id, assessment={"match_tier": "target", "match_score": 70}
        )
        FitAssessment.objects.create(
            student=student, university_id=self.franklin_id, assessment={"match_tier": "target", "match_score": 70}
        )

        student_view = self.student.get(f"/api/assessments/{self.student_id}/")
        self.assertEqual(student_view.data["count"], 2)

        wsu_view = self.officer_wsu.get(f"/api/assessments/{self.student_id}/")
        self.assertEqual(wsu_view.data["count"], 1)
        self.assertEqual(wsu_view.data["assessments"][0]["university_id"], self.wsu_id)

        franklin_view = self.officer_franklin.get(f"/api/assessments/{self.student_id}/")
        self.assertEqual(franklin_view.data["count"], 1)
        self.assertEqual(franklin_view.data["assessments"][0]["university_id"], self.franklin_id)

        self.assertEqual(FitAssessment.objects.filter(student__uuid=self.student_id).count(), 2)
