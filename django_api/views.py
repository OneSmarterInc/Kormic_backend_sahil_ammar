from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.github_oauth import GitHubNotConnectedError
from accounts.models import Account
from accounts.permissions import (
    IsStudentOrUniversityRole,
    IsStudentRole,
    IsTOTPEnrolled,
    IsUniversityRole,
    ScopedToOwnStudentId,
    ScopedToOwnUniversityId,
    get_account,
)
from django_api.models import (
    ChatAttachment,
    ChatMessage,
    FitAssessment,
    GitHubAnalysis,
    IntakeSession,
    LinkedInAnalysis,
    PendingQuery,
    ResumeUpload,
    RoadmapVersion,
    StudentProfile,
    UniversityQuestionLog,
    VerifiedAnswer,
)
from django_api.serializers import (
    ProfileCreateUpdateSerializer,
    ResumeUploadSerializer,
)
from django_api.services import (
    CHAT_ATTACHMENT_MAX_PER_MESSAGE,
    build_image_content_blocks,
    compute_priority_tier,
    create_or_update_profile,
    delete_profile_image,
    format_profile_response,
    get_profile,
    get_profile_image_path,
    get_priority_tier_bounds,
    get_priority_tier_counts,
    get_shortlisted_profiles,
    parse_resume,
    analyze_github,
    analyze_linkedin,
    load_profile_data,
    save_chat_attachment,
    save_profile_data,
    student_has_university_interest,
    upload_profile_image,
    ProfileValidationError,
)
from institutes_list.models import ListedStudent

logger = logging.getLogger(__name__)

STUDENT_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]
STUDENT_OWNER_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole, ScopedToOwnStudentId]
UNIVERSITY_OWNER_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole, ScopedToOwnUniversityId]

# Chat-history endpoints return a bounded window of the most recent turns
# (still in chronological order) instead of the entire transcript -- a long
# conversation would otherwise grow the response without limit. A caller can
# pass ?limit= for a smaller window; the cap protects the server regardless.
CHAT_HISTORY_LIMIT_DEFAULT = 200
CHAT_HISTORY_LIMIT_MAX = 500


def chat_history_limit(request) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return CHAT_HISTORY_LIMIT_DEFAULT
    try:
        return min(max(int(raw), 1), CHAT_HISTORY_LIMIT_MAX)
    except (TypeError, ValueError):
        return CHAT_HISTORY_LIMIT_DEFAULT


def log_chat_turn(
    *, channel, student_id, university_id="", user_message="", assistant_message="", meta=None, user_meta=None
):
    user_msg = ChatMessage.objects.create(
        channel=channel,
        student_id=student_id,
        university_id=university_id or "",
        sender=ChatMessage.Sender.USER,
        content=user_message,
        meta=user_meta or {},
    )
    assistant_msg = ChatMessage.objects.create(
        channel=channel,
        student_id=student_id,
        university_id=university_id or "",
        sender=ChatMessage.Sender.ASSISTANT,
        content=assistant_message,
        meta=meta or {},
    )
    return user_msg, assistant_msg


# ---------------------------------------------------------------------
# Home / health check
# ---------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([AllowAny])
def api_home(request):
    from agents import commons

    return Response({
        "message": "Kormic Commons Django REST API is running",
        "profile_apis": {
            "create_update_profile": "POST /api/profile/",
            "get_profile": "GET /api/profile/<student_id>/",
            "resume": "POST /api/profile/resume/",
            "resume_download": "GET /api/profile/resume/<resume_id>/",
            "resume_delete": "DELETE /api/profile/resume/<resume_id>/",
            "github": "POST /api/profile/github/",
            "linkedin": "POST /api/profile/linkedin/",
            "linkedin_history": "GET /api/profile/<student_id>/linkedin-history/",
            "linkedin_image": "GET /api/profile/linkedin/<analysis_id>/images/<index>/",
            "profile_image_upload": "POST /api/profile/image/",
            "profile_image": "GET /api/profile/<student_id>/image/",
            "profile_image_delete": "DELETE /api/profile/<student_id>/image/",
            "agent_name": "GET/PATCH /api/profile/agent-name/",
        },
        "chat_apis": {
            "profile_intake": "POST /api/chat/intake/",
            "agent_chat": "POST /api/chat/agent/ (multipart: message, attachments[])",
            "agent_chat_history": "GET /api/chat/agent/history/",
            "agent_chat_new": "POST /api/chat/agent/new/",
            "agent_chat_edit": "PATCH /api/chat/agent/<message_id>/edit/",
            "chat_attachment": "GET /api/chat/agent/attachments/<attachment_id>/",
        },
        "core_apis": {
            "roadmap": "GET /api/roadmap/<student_id>/",
            "pending_queries": "GET /api/queries/pending/",
            "answer_query": "POST /api/queries/answer/",
            "edit_query": "POST /api/queries/<query_id>/edit/",
            "profile_pdf": "GET /api/exports/pdf/<student_id>/",
        },
        "verification_apis": {
            "verification_status": "GET /api/verification/status/",
            "verification_items": "GET /api/verification/items/",
        },
        "assessment_apis": {
            "assessment_history": "GET /api/assessments/<student_id>/",
            "assessment_detail": "GET /api/assessments/<university_id>/<student_id>/",
        },
        "university_dashboard_apis": {
            "profiles": "GET /api/university/<university_id>/profiles/",
            "profile_presenter_chat": "POST /api/university/<university_id>/profile/<student_id>/chat/",
            "questions": "GET /api/university/<university_id>/questions/",
            "queries": "GET /api/university/<university_id>/queries/",
            "active_queries": "GET /api/university/<university_id>/queries/active/",
            "archive_queries": "GET /api/university/<university_id>/queries/archive/",
            "verified_knowledge": "GET /api/university/<university_id>/knowledge/verified/",
        },
        "university_ids": commons.list_university_ids(),
        "notes": (
            "University agents and the verification/fit-assessment action endpoints are "
            "no longer directly student-facing -- the student's personal agent (agent_chat) "
            "is the single entry point and reaches them in the background."
        ),
    })


# ---------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------

def api_error(message: str, http_status=status.HTTP_400_BAD_REQUEST):
    return Response({"status": "error", "message": str(message)}, status=http_status)


def load_intake_session(student_key: str) -> Optional[Dict[str, Any]]:
    session = IntakeSession.objects.filter(student_key=student_key).first()

    if session is None:
        return None

    return {
        "student_id": session.student_id,
        "step": session.step,
        "answers": dict(session.answers or {}),
        "completed": session.completed,
    }


def save_intake_session(student_key: str, session: Dict[str, Any]) -> None:
    obj, _ = IntakeSession.objects.get_or_create(student_key=student_key)
    obj.student_id = session.get("student_id", student_key)
    obj.step = int(session.get("step", 0))
    obj.completed = bool(session.get("completed", False))
    obj.answers = dict(session.get("answers", {}) or {})
    obj.save()


def extract_number(value: str):
    match = re.search(r"(\d+(?:\.\d+)?)", str(value or ""))

    if not match:
        return None

    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def load_profile_or_404(student_id):
    try:
        profile_data = get_profile(student_id)
        return profile_data, None

    except FileNotFoundError:
        return None, Response(
            {
                "status": "failed",
                "message": f"Profile not found for student_id: {student_id}",
            },
            status=status.HTTP_404_NOT_FOUND,
        )

    except Exception as exc:
        return None, Response(
            {
                "status": "failed",
                "message": "Could not load student profile",
                "error": str(exc),
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def call_first_available_method(obj, method_names, *args):
    for method_name in method_names:
        if not hasattr(obj, method_name):
            continue

        method = getattr(obj, method_name)

        try:
            return method(*args)
        except TypeError:
            try:
                return method()
            except TypeError:
                continue

    raise AttributeError(
        f"No supported method found. Tried: {', '.join(method_names)}"
    )


# ---------------------------------------------------------------------
# APIs 1-5: Profile Management
# ---------------------------------------------------------------------

class ProfileCreateUpdateAPIView(APIView):
    """
    POST /api/profile/
    Create or update base student profile.
    """

    permission_classes = STUDENT_PERMISSIONS

    def post(self, request):
        serializer = ProfileCreateUpdateSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                {"status": "error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            data = dict(serializer.validated_data)
            data["student_id"] = request.user.account.student_uuid
            result = create_or_update_profile(data)
            return Response(
                {
                    "status": "success",
                    "message": "Profile updated",
                    "student_id": result["student_id"],
                    "profile_file": result["profile_file"],
                    "profile": result["profile"],
                },
                status=status.HTTP_200_OK,
            )
        except ProfileValidationError as exc:
            return Response(
                {"status": "error", "errors": exc.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProfileDetailAPIView(APIView):
    """
    GET /api/profile/<student_id>/
    Fetch saved student profile JSON.
    """

    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        try:
            profile = get_profile(student_id)
            profile["profile_image_url"] = (
                request.build_absolute_uri(f"/api/profile/{student_id}/image/")
                if get_profile_image_path(student_id)
                else None
            )
            return Response(format_profile_response(profile), status=status.HTTP_200_OK)
        except FileNotFoundError as exc:
            return api_error(str(exc), status.HTTP_404_NOT_FOUND)
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProfileImageUploadAPIView(APIView):
    """
    POST /api/profile/image/
    Upload/replace the authenticated student's profile picture. Unlike
    resume/LinkedIn uploads, this is not a history -- each call replaces
    the previous image.
    """

    permission_classes = STUDENT_PERMISSIONS

    def post(self, request):
        student_id = request.user.account.student_uuid
        image = request.FILES.get("image")

        if not image:
            return api_error("An image file is required using key 'image'.")

        if image.content_type and not image.content_type.startswith("image/"):
            return api_error(f"Unsupported file type: {image.content_type}. Upload an image file.")

        try:
            upload_profile_image(student_id=student_id, uploaded_file=image)
            return Response(
                {
                    "status": "success",
                    "student_id": student_id,
                    "profile_image_url": request.build_absolute_uri(f"/api/profile/{student_id}/image/"),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


class ProfileImageDetailAPIView(APIView):
    """
    GET /api/profile/<student_id>/image/ — download the current profile picture.
    Readable by the owning student or any university officer (dashboard
    rosters list every student's picture), matching UniversityProfilesListView.
    DELETE /api/profile/<student_id>/image/ — remove it; owner-only.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentOrUniversityRole]

    def get(self, request, student_id):
        account = get_account(request)
        if account.role == "student" and account.student_uuid != student_id:
            return api_error("You may only access your own profile picture.", status.HTTP_403_FORBIDDEN)

        image_path = get_profile_image_path(student_id)
        if not image_path:
            return api_error("No profile picture uploaded for this student.", status.HTTP_404_NOT_FOUND)

        file_path = Path(image_path)
        if not file_path.exists():
            return api_error("Profile picture file is missing on the server.", status.HTTP_404_NOT_FOUND)

        content = file_path.read_bytes()
        return FileResponse(io.BytesIO(content), as_attachment=False, filename=file_path.name)

    def delete(self, request, student_id):
        account = get_account(request)
        if account.role != "student" or account.student_uuid != student_id:
            return api_error("You may only delete your own profile picture.", status.HTTP_403_FORBIDDEN)

        removed = delete_profile_image(student_id)
        if not removed:
            return api_error("No profile picture uploaded for this student.", status.HTTP_404_NOT_FOUND)

        return Response(status=status.HTTP_204_NO_CONTENT)


class ResumeUploadAPIView(APIView):
    """
    POST /api/profile/resume/
    Upload and parse resume PDF/DOCX.
    """

    permission_classes = STUDENT_PERMISSIONS

    def post(self, request):
        data = request.data.copy()
        data["student_id"] = request.user.account.student_uuid
        serializer = ResumeUploadSerializer(data=data)

        if not serializer.is_valid():
            return Response(
                {"status": "error", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = parse_resume(
                student_id=serializer.validated_data["student_id"],
                uploaded_file=serializer.validated_data["file"],
            )
            return Response(
                {
                    "status": "success",
                    "student_id": result["student_id"],
                    "resume_id": result["resume_id"],
                    "resume_url": request.build_absolute_uri(f"/api/profile/resume/{result['resume_id']}/"),
                    "extracted_data": result["extracted_data"],
                    "profile": result["profile"],
                },
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


class ResumeDetailAPIView(APIView):
    """
    GET /api/profile/resume/<resume_id>/ — download the original resume file.
    DELETE /api/profile/resume/<resume_id>/ — remove a resume upload (file + row).
    """

    permission_classes = STUDENT_PERMISSIONS

    def _get_owned_resume(self, request, resume_id):
        try:
            resume = ResumeUpload.objects.select_related("student").get(pk=resume_id)
        except ResumeUpload.DoesNotExist:
            return None, api_error("Resume not found.", status.HTTP_404_NOT_FOUND)

        if str(resume.student.uuid) != request.user.account.student_uuid:
            return None, api_error("You may only access your own resumes.", status.HTTP_403_FORBIDDEN)

        return resume, None

    def get(self, request, resume_id):
        resume, error_response = self._get_owned_resume(request, resume_id)
        if error_response:
            return error_response

        file_path = Path(resume.file_path)
        if not file_path.exists():
            return api_error("Resume file is missing on the server.", status.HTTP_404_NOT_FOUND)

        # Read fully into memory rather than handing FileResponse an open
        # file handle: on Windows an open handle blocks a same-request-cycle
        # delete/unlink of that file, and this keeps the handle short-lived
        # regardless of platform.
        content = file_path.read_bytes()
        return FileResponse(io.BytesIO(content), as_attachment=True, filename=resume.original_filename)

    def delete(self, request, resume_id):
        resume, error_response = self._get_owned_resume(request, resume_id)
        if error_response:
            return error_response

        file_path = Path(resume.file_path)
        if file_path.exists():
            file_path.unlink(missing_ok=True)

        resume.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class GitHubAnalyzeAPIView(APIView):
    """
    POST /api/profile/github/
    Analyzes the student's own OAuth-connected GitHub account (§3.5) --
    no request body. There is no way to point this at someone else's
    GitHub profile; the username always comes from the verified OAuth
    connection, never from client input.
    """

    permission_classes = STUDENT_PERMISSIONS

    def post(self, request):
        student_id = request.user.account.student_uuid

        try:
            result = analyze_github(student_id=student_id)
            return Response(
                {
                    "status": "success",
                    "student_id": result["student_id"],
                    "github_username": result["github_username"],
                    "skills_added": result["skills_added"],
                    "github_result": result["github_result"],
                },
                status=status.HTTP_200_OK,
            )
        except GitHubNotConnectedError as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


def build_linkedin_images_payload(request, analysis_id: int, image_paths):
    """
    Turns the absolute on-disk paths stored on a LinkedInAnalysis row into a
    frontend-usable shape: a full, directly-fetchable URL to the image
    (raw MEDIA_URL static serving isn't used here since these screenshots
    are private -- serving must stay behind the same JWT+ownership check as
    the rest of the API, so the URL still requires an Authorization header
    when fetched -- it just isn't a bare relative path anymore).
    """
    payload = []

    for index, path in enumerate(image_paths or []):
        relative_url = f"/api/profile/linkedin/{analysis_id}/images/{index}/"

        payload.append({
            "index": index,
            "uploaded_image_url": request.build_absolute_uri(relative_url),
        })

    return payload


class LinkedInAnalyzeAPIView(APIView):
    """
    POST /api/profile/linkedin/
    Upload LinkedIn screenshots and update profile. Safe to call again later
    to add more screenshots -- each call is a new history entry (same
    pattern as repeated resume uploads), nothing is overwritten.
    """

    permission_classes = STUDENT_PERMISSIONS

    def post(self, request):
        student_id = request.user.account.student_uuid
        images = request.FILES.getlist("images")

        if not images:
            return api_error("At least one image is required using key 'images'.")

        try:
            result = analyze_linkedin(student_id=student_id, uploaded_images=images)
            return Response(
                {
                    "status": "success",
                    "student_id": result["student_id"],
                    "analysis_id": result["analysis_id"],
                    "images": build_linkedin_images_payload(request, result["analysis_id"], result["image_paths"]),
                    "skills_added": result["skills_added"],
                    "extracted": result["extracted"],
                },
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return api_error(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)


class LinkedInImageDetailAPIView(APIView):
    """
    GET /api/profile/linkedin/<analysis_id>/images/<index>/
    Streams one uploaded LinkedIn screenshot, scoped to the owning student.
    """

    permission_classes = STUDENT_PERMISSIONS

    def get(self, request, analysis_id, index):
        try:
            analysis = LinkedInAnalysis.objects.select_related("student").get(pk=analysis_id)
        except LinkedInAnalysis.DoesNotExist:
            return api_error("LinkedIn analysis not found.", status.HTTP_404_NOT_FOUND)

        if str(analysis.student.uuid) != request.user.account.student_uuid:
            return api_error("You may only access your own LinkedIn images.", status.HTTP_403_FORBIDDEN)

        image_paths = analysis.image_paths or []
        if index < 0 or index >= len(image_paths):
            return api_error("Image index out of range.", status.HTTP_404_NOT_FOUND)

        file_path = Path(image_paths[index])
        if not file_path.exists():
            return api_error("Image file is missing on the server.", status.HTTP_404_NOT_FOUND)

        content = file_path.read_bytes()
        return FileResponse(io.BytesIO(content), as_attachment=False, filename=file_path.name)


# ---------------------------------------------------------------------
# API 6: Profile Intake Chat
# ---------------------------------------------------------------------

INTAKE_FIELDS = [
    {"key": "name", "question": "What is your full name?"},
    {"key": "target_country", "question": "Which country are you targeting for graduate study?"},
    {"key": "program", "question": "Which program or degree are you targeting? Example: MS Computer Science."},
    {"key": "major", "question": "What was your undergraduate major?"},
    {"key": "institution", "question": "What is your undergraduate institution name?"},
    {"key": "gpa", "question": "What is your GPA and scale? Example: 8.2/10 or 3.4/4."},
    {"key": "budget", "question": "What is your annual budget in USD? Example: 40000."},
    {"key": "gre_quant", "question": "What is your GRE Quant score? If not taken, say not taken."},
    {"key": "toefl", "question": "What is your TOEFL score? If you have IELTS instead, mention IELTS."},
    {"key": "research", "question": "Briefly describe your research, internships, or important projects."},
]


def map_intake_answer(field_key: str, answer: str) -> Dict[str, Any]:
    answer = str(answer or "").strip()

    if field_key == "gpa":
        profile_update = {"gpa_text": answer}
        numbers = re.findall(r"\d+(?:\.\d+)?", answer)
        if numbers:
            profile_update["gpa"] = float(numbers[0])
        if len(numbers) >= 2:
            profile_update["gpa_scale"] = str(numbers[1])
        return profile_update

    if field_key == "budget":
        return {"budget": extract_number(answer), "budget_text": answer}

    if field_key == "toefl":
        lower = answer.lower()
        if "ielts" in lower:
            return {"ielts": extract_number(answer), "toefl": None, "english_score_text": answer}
        if "not" in lower or "no" in lower or lower == "na":
            return {"toefl": None}
        return {"toefl": extract_number(answer)}

    if field_key == "gre_quant":
        lower = answer.lower()
        if "not" in lower or "no" in lower or lower == "na":
            return {"gre_quant": None}
        return {"gre_quant": extract_number(answer)}

    return {field_key: answer}


@api_view(["POST"])
@permission_classes(STUDENT_PERMISSIONS)
def profile_intake_chat(request):
    student_id = request.user.account.student_uuid
    answer = request.data.get("answer", "")

    key = student_id
    session = load_intake_session(key)

    if session is None or answer == "":
        session = {"student_id": student_id, "step": 0, "answers": {}, "completed": False}
        save_intake_session(key, session)
        log_chat_turn(
            channel=ChatMessage.Channel.INTAKE,
            student_id=student_id,
            user_message=answer,
            assistant_message=INTAKE_FIELDS[0]["question"],
        )
        return Response({
            "completed": False,
            "next_question": INTAKE_FIELDS[0]["question"],
            "step": 0,
            "total_steps": len(INTAKE_FIELDS),
        })

    if session.get("completed"):
        return Response({
            "completed": True,
            "message": "Profile intake is already completed.",
            "profile": load_profile_data(student_id),
        })

    step = int(session.get("step", 0))

    if step >= len(INTAKE_FIELDS):
        return Response({
            "completed": True,
            "message": "Profile intake already completed.",
            "profile": load_profile_data(student_id),
        })

    field = INTAKE_FIELDS[step]
    session["answers"].update(map_intake_answer(field["key"], answer))
    session["step"] = step + 1

    if session["step"] >= len(INTAKE_FIELDS):
        session["completed"] = True
        save_intake_session(key, session)
        profile = load_profile_data(student_id)
        profile.update(session["answers"])
        save_profile_data(student_id, profile)
        log_chat_turn(
            channel=ChatMessage.Channel.INTAKE,
            student_id=student_id,
            user_message=answer,
            assistant_message="Profile intake completed.",
        )
        return Response({
            "completed": True,
            "message": "Profile intake completed.",
            "profile": profile,
        })

    save_intake_session(key, session)
    log_chat_turn(
        channel=ChatMessage.Channel.INTAKE,
        student_id=student_id,
        user_message=answer,
        assistant_message=INTAKE_FIELDS[session["step"]]["question"],
    )
    return Response({
        "completed": False,
        "next_question": INTAKE_FIELDS[session["step"]]["question"],
        "step": session["step"],
        "total_steps": len(INTAKE_FIELDS),
    })


# ---------------------------------------------------------------------
# Agent name (setup + rename)
# ---------------------------------------------------------------------

class AgentNameAPIView(APIView):
    """
    GET /api/profile/agent-name/ — return this student's personal agent's
    display name, auto-assigning a default one on first call.
    PATCH /api/profile/agent-name/ — rename it. Body: {"agent_name": "..."}.
    Names must be unique (case-insensitive) across all students.
    """

    permission_classes = STUDENT_PERMISSIONS

    def get(self, request):
        from agents.agent_identity import ensure_agent_name

        student_id = request.user.account.student_uuid
        profile, _ = StudentProfile.objects.get_or_create(uuid=student_id)
        agent_name = ensure_agent_name(profile)
        return Response({"agent_name": agent_name})

    def patch(self, request):
        from agents.agent_identity import is_agent_name_available

        student_id = request.user.account.student_uuid
        student_key = student_id
        new_name = str(request.data.get("agent_name", "")).strip()

        if not new_name:
            return api_error("agent_name is required.")
        if len(new_name) > 100:
            return api_error("agent_name must be 100 characters or fewer.")
        if not is_agent_name_available(new_name, exclude_student_id=student_key):
            return api_error("This agent name is already taken. Please choose another.", status.HTTP_409_CONFLICT)

        profile, _ = StudentProfile.objects.get_or_create(uuid=student_key)
        profile.agent_name = new_name
        profile.save(update_fields=["agent_name", "updated_at"])

        # No cache to evict -- pure_multi_agent.runtime loads agent_name fresh
        # from the database on every turn, so the new name is already live.
        return Response({"agent_name": profile.agent_name})


# ---------------------------------------------------------------------
# API 7: Personal Agent Chat -- the student's single entry point.
# Verification, GitHub/LinkedIn/resume interpretation, and university fit
# checks all happen through this one endpoint, via the LangGraph multi-agent
# runtime in pure_multi_agent/.
# ---------------------------------------------------------------------

def _serialize_attachment(request, attachment: "ChatAttachment") -> Dict[str, Any]:
    from django.urls import reverse

    return {
        "id": attachment.id,
        "filename": attachment.original_filename,
        "content_type": attachment.content_type,
        "size_bytes": attachment.size_bytes,
        "url": request.build_absolute_uri(reverse("chat-attachment-detail", args=[attachment.id])),
    }


def _notify_agent_reply(student_id: str, agent_name: str, reply: str) -> None:
    # A notification failure must never fail the chat response itself.
    try:
        from notifications.services import notify_agent_reply

        notify_agent_reply(student_id=student_id, agent_name=agent_name, reply=reply or "")
    except Exception:
        logger.exception("Failed to queue agent-reply push notification for student_id=%s", student_id)


def _existing_pending_query_ids(student_id: str) -> set:
    return set(PendingQuery.objects.filter(student_id=student_id).values_list("id", flat=True))


def _new_pending_query(student_id: str, existing_ids: set) -> Optional["PendingQuery"]:
  
    return (
        PendingQuery.objects.filter(student_id=student_id)
        .exclude(id__in=existing_ids)
        .order_by("id")
        .first()
    )


def _escalation_meta(pending_query: Optional["PendingQuery"]) -> Dict[str, Any]:
    if pending_query is None:
        return {}
    return {
        "type": "escalation_pending",
        "query_id": pending_query.id,
        "university_id": pending_query.university_id,
    }


@api_view(["POST"])
@permission_classes(STUDENT_PERMISSIONS)
def agent_chat(request):
    from pure_multi_agent.runtime import run_turn

    student_id = request.user.account.student_uuid
    message = str(request.data.get("message") or "")
    uploaded_files = request.FILES.getlist("attachments")

    if not message.strip() and not uploaded_files:
        return api_error("message or at least one attachment is required.")
    if len(uploaded_files) > CHAT_ATTACHMENT_MAX_PER_MESSAGE:
        return api_error(f"You can attach at most {CHAT_ATTACHMENT_MAX_PER_MESSAGE} files per message.")

    user_msg = ChatMessage.objects.create(
        channel=ChatMessage.Channel.AGENT,
        student_id=student_id,
        sender=ChatMessage.Sender.USER,
        content=message,
    )

    attachments = []
    try:
        for uploaded_file in uploaded_files:
            attachments.append(save_chat_attachment(student_id, user_msg, uploaded_file))
    except ValueError as exc:
        user_msg.delete()
        return api_error(str(exc))

    image_blocks = build_image_content_blocks(attachments)
    # A message made up of only attachments still needs some text for the
    # model turn -- describe what was shared instead of sending empty content.
    effective_message = message.strip() or (
        "(Shared file(s) with no additional message: "
        + ", ".join(a.original_filename for a in attachments) + ")"
    )

    try:
        _existing_pq_ids = _existing_pending_query_ids(student_id)
        agent_name, reply = run_turn(student_id, effective_message, image_blocks=image_blocks or None)

        _pq = _new_pending_query(student_id, _existing_pq_ids)
        ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT,
            student_id=student_id,
            sender=ChatMessage.Sender.ASSISTANT,
            content=reply or "",
            meta=_escalation_meta(_pq),
        )
        _notify_agent_reply(student_id, agent_name, reply or "")

        return Response({
            "agent": agent_name,
            "student_id": student_id,
            "reply": reply,
            "message_id": user_msg.id,
            "pending": bool(_pq),
            "query_id": _pq.id if _pq else None,
            "attachments": [_serialize_attachment(request, a) for a in attachments],
        })
    except Exception as exc:
        return api_error(f"Agent chat failed: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["PATCH"])
@permission_classes(STUDENT_PERMISSIONS)
def agent_chat_edit(request, message_id):
    """
    PATCH /api/chat/agent/<message_id>/edit/
    Edits a previously-sent user message and regenerates the AI reply from
    that point: the old reply and everything sent after it are discarded --
    they were responding to a question that no longer exists once the
    message is changed -- and the in-process LangGraph conversation state is
    rebuilt to match (see pure_multi_agent.runtime.seed_conversation) before
    asking the model again. Attachments already on the message are left
    alone and are still sent to the model along with the edited text.
    """
    from django.utils import timezone

    from pure_multi_agent.runtime import run_turn, seed_conversation

    student_id = request.user.account.student_uuid
    new_message = str(request.data.get("message", "")).strip()

    if not new_message:
        return api_error("message is required.")

    target = ChatMessage.objects.filter(
        pk=message_id,
        channel=ChatMessage.Channel.AGENT,
        student_id=student_id,
        sender=ChatMessage.Sender.USER,
    ).first()
    if target is None:
        return api_error("Message not found.", status.HTTP_404_NOT_FOUND)

    prior_turns = list(
        ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT, student_id=student_id, pk__lt=target.pk)
        .order_by("created_at")
        .values_list("sender", "content")
    )

    # Everything after the edited message is now stale -- it was generated
    # in response to a question that no longer exists.
    ChatMessage.objects.filter(
        channel=ChatMessage.Channel.AGENT, student_id=student_id, pk__gt=target.pk
    ).delete()

    image_blocks = build_image_content_blocks(target.attachments.all())

    target.content = new_message
    target.edited_at = timezone.now()
    target.save(update_fields=["content", "edited_at"])

    seed_conversation(student_id, prior_turns)

    try:
        _existing_pq_ids = _existing_pending_query_ids(student_id)
        agent_name, reply = run_turn(student_id, new_message, image_blocks=image_blocks or None)
        _pq = _new_pending_query(student_id, _existing_pq_ids)
        ChatMessage.objects.create(
            channel=ChatMessage.Channel.AGENT,
            student_id=student_id,
            sender=ChatMessage.Sender.ASSISTANT,
            content=reply or "",
            meta=_escalation_meta(_pq),
        )
        _notify_agent_reply(student_id, agent_name, reply or "")

        return Response({
            "agent": agent_name,
            "student_id": student_id,
            "reply": reply,
            "message_id": target.id,
            "edited_at": target.edited_at,
            "pending": bool(_pq),
            "query_id": _pq.id if _pq else None,
        })
    except Exception as exc:
        return api_error(f"Agent chat failed: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR)


class ChatAttachmentDetailAPIView(APIView):
    """
    GET /api/chat/agent/attachments/<attachment_id>/
    Streams one chat-uploaded screenshot/document, scoped to the owning
    student via the parent ChatMessage -- same pattern as
    LinkedInImageDetailAPIView.
    """

    permission_classes = STUDENT_PERMISSIONS

    def get(self, request, attachment_id):
        try:
            attachment = ChatAttachment.objects.select_related("message").get(pk=attachment_id)
        except ChatAttachment.DoesNotExist:
            return api_error("Attachment not found.", status.HTTP_404_NOT_FOUND)

        if attachment.message.student_id != request.user.account.student_uuid:
            return api_error("You may only access your own chat attachments.", status.HTTP_403_FORBIDDEN)

        file_path = Path(attachment.file_path)
        if not file_path.exists():
            return api_error("Attachment file is missing on the server.", status.HTTP_404_NOT_FOUND)

        content = file_path.read_bytes()
        return FileResponse(
            io.BytesIO(content),
            as_attachment=False,
            filename=attachment.original_filename,
            content_type=attachment.content_type or None,
        )


@api_view(["GET"])
@permission_classes(STUDENT_PERMISSIONS)
def agent_chat_history(request):
    student_id = request.user.account.student_uuid
    limit = chat_history_limit(request)
    base_qs = ChatMessage.objects.filter(channel=ChatMessage.Channel.AGENT, student_id=student_id)
    total = base_qs.count()
    # Take the most recent `limit` turns, then flip back to chronological
    # order for the client.
    messages = base_qs.prefetch_related("attachments").order_by("-created_at")[:limit]
    # Escalation state, computed at read time: collect every query_id any
    # message's meta references, fetch their current status in one query, and
    # annotate. A message tagged escalation_pending whose query has since been
    # answered will therefore read status "resolved" -- the app can flip the
    # old "checking..." bubble without waiting for a new message.
    _msgs = list(messages)[::-1]
    _qids = {
        m.meta.get("query_id")
        for m in _msgs
        if isinstance(m.meta, dict) and m.meta.get("query_id") is not None
    }
    _status_by_id = {
        pq.id: pq.status
        for pq in PendingQuery.objects.filter(id__in=_qids)
    } if _qids else {}

    def _escalation(m):
        if not (isinstance(m.meta, dict) and m.meta.get("query_id") is not None):
            return None
        qid = m.meta.get("query_id")
        return {"query_id": qid, "status": _status_by_id.get(qid, "unknown")}

    return Response({
        "count": len(_msgs),
        "total": total,
        "truncated": total > len(_msgs),
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "content": m.content,
                "created_at": m.created_at,
                "edited_at": m.edited_at,
                "meta": m.meta,
                "escalation": _escalation(m),
                "attachments": [_serialize_attachment(request, a) for a in m.attachments.all()],
            }
            for m in _msgs
        ],
    })


@api_view(["POST"])
@permission_classes(STUDENT_PERMISSIONS)
def agent_chat_new(request):
    """
    POST /api/chat/agent/new/
    Starts a new conversation with the student's personal agent: deletes the
    persisted transcript (what chat/agent/history/ returns) and resets the
    in-process LangGraph conversation state (pure_multi_agent.runtime's
    checkpointer), so the next chat/agent/ turn begins with no prior turns
    in context. Deliberately does not touch AriaMemory or the student's
    profile -- durable facts learned about the student (GPA, universities
    discussed, etc.) survive a "new chat" the same way they'd survive the
    student opening a new tab; only the turn-by-turn conversation resets.
    """
    from pure_multi_agent.runtime import reset_conversation

    student_id = request.user.account.student_uuid
    deleted_count, _ = ChatMessage.objects.filter(
        channel=ChatMessage.Channel.AGENT, student_id=student_id
    ).delete()
    reset_conversation(student_id)

    return Response({"status": "ok", "student_id": student_id, "messages_deleted": deleted_count})


class AssessmentHistoryView(APIView):
    """
    GET /api/assessments/<student_id>/
    Dual-mode: the owning student sees history across every university;
    a university officer sees only their own university's history for
    that student.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentOrUniversityRole]

    def get(self, request, student_id):
        account = get_account(request)

        if account.role == "student":
            if account.student_uuid != student_id:
                return api_error("You may only access your own assessment history.", status.HTTP_403_FORBIDDEN)
            rows = FitAssessment.objects.filter(student__uuid=student_id)
        else:
            rows = FitAssessment.objects.filter(student__uuid=student_id, university_id=account.university_uuid)

        return Response({
            "student_id": student_id,
            "count": rows.count(),
            "assessments": [
                {"university_id": r.university_id, "assessment": r.assessment, "created_at": r.created_at}
                for r in rows
            ],
        })


class AssessmentDetailView(APIView):
    """
    GET /api/assessments/<university_id>/<student_id>/
    Returns only the latest fit assessment for one university+student pair,
    without having to filter the full cross-university history client-side.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentOrUniversityRole]

    def get(self, request, university_id, student_id):
        account = get_account(request)

        if account.role == "student" and account.student_uuid != student_id:
            return api_error("You may only access your own assessment history.", status.HTTP_403_FORBIDDEN)
        if account.role != "student" and account.university_uuid != university_id:
            return api_error("You may only access your own university's assessment history.", status.HTTP_403_FORBIDDEN)

        row = (
            FitAssessment.objects.filter(student__uuid=student_id, university_id=university_id)
            .order_by("-created_at")
            .first()
        )

        if not row:
            return api_error(
                f"No fit assessment found for student_id={student_id}, university_id={university_id}.",
                status.HTTP_404_NOT_FOUND,
            )

        return Response({
            "student_id": student_id,
            "university_id": university_id,
            "assessment": row.assessment,
            "created_at": row.created_at,
        })


# ---------------------------------------------------------------------
# API 10: Roadmap
# ---------------------------------------------------------------------

class RoadmapView(APIView):
    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        profile, error_response = load_profile_or_404(student_id)
        if error_response:
            return error_response

        user_message = request.query_params.get("message", "").strip()
        if not user_message:
            user_message = "Generate a personalized roadmap for this student's application process or exam preparation based on the saved profile."

        try:
            try:
                from roadmap.roadmap_planner import RoadmapPlanner
            except ImportError:
                from roadmap_planner import RoadmapPlanner

            planner = RoadmapPlanner()
            if hasattr(planner, "generate_application_roadmap"):
                roadmap = planner.generate_application_roadmap(profile, user_message)
            else:
                roadmap = call_first_available_method(
                    planner,
                    ["generate", "generate_roadmap", "create_roadmap", "build_roadmap", "plan"],
                    profile,
                    user_message,
                )

            if isinstance(roadmap, dict):
                profile["roadmap"] = roadmap
                save_profile_data(student_id, profile)
                # The profile JSON can exist before a StudentProfile row does
                # -- don't 500 on the history write in that case.
                student_row, _ = StudentProfile.objects.get_or_create(uuid=student_id)
                RoadmapVersion.objects.create(
                    student=student_row,
                    request_message=user_message,
                    roadmap=roadmap,
                )

            return Response({"status": "success", "student_id": student_id, "request": user_message, "roadmap": roadmap})
        except ImportError as exc:
            return Response(
                {
                    "status": "failed",
                    "message": "Roadmap planner file not found.",
                    "error": str(exc),
                    "expected_file": "roadmap_planner.py or roadmap/roadmap_planner.py",
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )
        except Exception as exc:
            return Response(
                {"status": "failed", "message": "Roadmap generation failed.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class RoadmapHistoryView(APIView):
    """GET /api/roadmap/<student_id>/history/ — every roadmap generated for this student."""

    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        rows = RoadmapVersion.objects.filter(student__uuid=student_id)
        return Response({
            "student_id": student_id,
            "count": rows.count(),
            "versions": [
                {"request_message": r.request_message, "roadmap": r.roadmap, "created_at": r.created_at}
                for r in rows
            ],
        })


# ---------------------------------------------------------------------
# Persistent GET APIs for profile sub-resources (resume/GitHub/LinkedIn history)
# ---------------------------------------------------------------------

class ResumeHistoryView(APIView):
    """GET /api/profile/<student_id>/resumes/ — every resume uploaded by this student."""

    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        rows = ResumeUpload.objects.filter(student__uuid=student_id)
        return Response({
            "student_id": student_id,
            "count": rows.count(),
            "resumes": [
                {
                    "id": r.id,
                    "original_filename": r.original_filename,
                    "resume_url": request.build_absolute_uri(f"/api/profile/resume/{r.id}/"),
                    "extracted_data": r.extracted_data,
                    "created_at": r.created_at,
                }
                for r in rows
            ],
        })


class GitHubHistoryView(APIView):
    """GET /api/profile/<student_id>/github-history/ — every GitHub analysis run for this student."""

    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        rows = GitHubAnalysis.objects.filter(student__uuid=student_id)
        return Response({
            "student_id": student_id,
            "count": rows.count(),
            "analyses": [
                {"github_url": r.github_url, "result": r.result, "created_at": r.created_at}
                for r in rows
            ],
        })


class LinkedInHistoryView(APIView):
    """GET /api/profile/<student_id>/linkedin-history/ — every LinkedIn analysis run for this student."""

    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        rows = LinkedInAnalysis.objects.filter(student__uuid=student_id)
        return Response({
            "student_id": student_id,
            "count": rows.count(),
            "analyses": [
                {
                    "id": r.id,
                    "images": build_linkedin_images_payload(request, r.id, r.image_paths),
                    "extracted": r.extracted,
                    "created_at": r.created_at,
                }
                for r in rows
            ],
        })


# ---------------------------------------------------------------------
# API 11: Pending Queries
# ---------------------------------------------------------------------

def _own_university_id_or_error(request):
    """(university_id, None) or (None, error Response). A university-role
    account with no university_id set is a misconfiguration -- fail closed
    rather than letting `filter(university_id=None)` match orphaned rows."""
    own_university_id = request.user.account.university_uuid
    if not own_university_id:
        return None, Response(
            {"status": "failed", "message": "No university is associated with this account."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return own_university_id, None


def _get_scoped_pending_query(request, query_id):
    """
    Look a PendingQuery up *within the caller's own university*. A query that
    belongs to another university -- or an orphaned one with a blank
    university_id -- is a plain 404, never reachable for mutation. Returns
    (query, None) or (None, error Response).
    """
    own_university_id, error = _own_university_id_or_error(request)
    if error:
        return None, error

    query = PendingQuery.objects.filter(id=query_id, university_id=own_university_id).first()
    if query is None:
        return None, Response(
            {"status": "failed", "message": f"Pending query not found for query_id: {query_id}"},
            status=status.HTTP_404_NOT_FOUND,
        )
    return query, None


class PendingQueriesView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def get(self, request):
        own_university_id, error = _own_university_id_or_error(request)
        if error:
            return error
        rows = PendingQuery.objects.filter(university_id=own_university_id).exclude(
            status__in=[PendingQuery.Status.RESOLVED, PendingQuery.Status.IGNORED]
        )

        pending_queries = [
            {
                "id": r.id,
                "query_id": r.id,
                "student_id": r.student_id,
                "student_name": r.student_name,
                "university_id": r.university_id,
                "agent_name": r.agent_name,
                "program": r.program,
                "question": r.question,
                "priority": r.priority,
                "urgency_reason": r.urgency_reason,
                "status": r.status,
                "confidence": r.confidence,
                "timestamp": r.created_at,
            }
            for r in rows
        ]

        return Response({"pending_queries": pending_queries, "count": len(pending_queries)})


# ---------------------------------------------------------------------
# API 12: Answer Pending Query
# ---------------------------------------------------------------------

class AnswerPendingQueryView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def post(self, request):
        query_id = request.data.get("query_id")
        answer = request.data.get("answer")
        answered_by = request.data.get("answered_by", "Admin")

        if query_id is None:
            return Response({"status": "failed", "message": "query_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        if not answer:
            return Response({"status": "failed", "message": "answer is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            query_id_int = int(query_id)
        except (TypeError, ValueError):
            return Response({"status": "failed", "message": "query_id must be a number"}, status=status.HTTP_400_BAD_REQUEST)

        selected_query, error = _get_scoped_pending_query(request, query_id_int)
        if error:
            return error

        if selected_query.status == PendingQuery.Status.RESOLVED:
            return Response(
                {"status": "failed", "message": f"Query {query_id_int} is already resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Guaranteed non-empty and == the caller's own university by
        # _get_scoped_pending_query.
        university_id = selected_query.university_id

        try:
            from agents import commons
            agent = commons.get_university_agent(university_id)
            resolved = agent.resolve_pending_query(query_id=query_id_int, answer=answer, answered_by=answered_by)
            if not resolved:
                return Response({"status": "failed", "message": "Could not resolve pending query."}, status=status.HTTP_400_BAD_REQUEST)

            # Surface the resolution back to the student: drop it into their
            # agent chat thread and push-notify them, even if they're not
            # in the app right now. 
            if selected_query.student_id:
                try:
                    from notifications.services import notify_pending_query_resolved

                    student_facing_answer = agent.format_answer_for_student(
                        question=selected_query.question,
                        answer=answer,
                    )

                    notify_pending_query_resolved(
                        student_id=selected_query.student_id,
                        university_id=university_id,
                        question=selected_query.question,
                        answer=student_facing_answer,
                        query_id=query_id_int,
                    )
                except Exception:
                    logger.exception(
                        "Failed to notify student_id=%s of resolved query_id=%s",
                        selected_query.student_id,
                        query_id_int,
                    )

            return Response(
                {"status": "success", "message": "Saved to Knowledge Base", "query_id": query_id_int, "university_id": university_id},
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return Response(
                {"status": "failed", "message": "Failed to save human-verified answer.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------
# API 13: Export PDF
# ---------------------------------------------------------------------

class ExportProfilePDFView(APIView):
    permission_classes = STUDENT_OWNER_PERMISSIONS

    def get(self, request, student_id):
        profile_data, error_response = load_profile_or_404(student_id)
        if error_response:
            return error_response

        try:
            pdf_path = None
            try:
                from student_profile.student_profile import StudentProfile
                profile_obj = StudentProfile.load(student_id)
                if hasattr(profile_obj, "export_pdf"):
                    pdf_path = profile_obj.export_pdf()
            except Exception:
                pass

            if not pdf_path:
                try:
                    from agents.profile_presenter import ProfilePresenter
                    presenter = ProfilePresenter()
                    pdf_path = call_first_available_method(
                        presenter,
                        ["export_pdf", "generate_pdf", "create_pdf", "build_pdf", "render_pdf"],
                        profile_data,
                    )
                except ImportError:
                    pass

            if not pdf_path:
                return Response(
                    {
                        "status": "failed",
                        "message": "PDF export feature is not available. Expected StudentProfile.export_pdf() or agents/profile_presenter.py.",
                    },
                    status=status.HTTP_501_NOT_IMPLEMENTED,
                )

            pdf_path = Path(pdf_path)
            if not pdf_path.exists():
                return Response(
                    {"status": "failed", "message": "PDF was generated but file was not found.", "pdf_path": str(pdf_path)},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Read fully into memory rather than handing FileResponse an open
            # file handle -- matches every other download view here and keeps
            # the handle short-lived (an open handle blocks unlink on Windows).
            content = pdf_path.read_bytes()
            return FileResponse(
                io.BytesIO(content), as_attachment=True, filename=pdf_path.name, content_type="application/pdf"
            )
        except Exception as exc:
            return Response(
                {"status": "failed", "message": "PDF export failed.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ---------------------------------------------------------------------
# University Dashboard APIs
#
# Ported from the legacy standalone FastAPI admin service that used to live
# at api/university_interface.py (removed — superseded by these routes).
# ---------------------------------------------------------------------

def serialize_pending_query(query: "PendingQuery") -> Dict[str, Any]:
    return {
        "id": query.id,
        "query_id": query.id,
        "university_id": query.university_id,
        "university": query.university_name,
        "agent_name": query.agent_name,
        "student_id": query.student_id,
        "student_name": query.student_name,
        "program": query.program,
        "question": query.question,
        "status": query.status,
        "priority": query.priority,
        "urgency_reason": query.urgency_reason,
        "display_status": query.display_status,
        "escalation_chain": query.escalation_chain,
        "group": query.group.slug if query.group_id else None,
        "routed_to_name": query.routed_to_name,
        "routed_to_email": query.routed_to_email,
        "confidence": query.confidence,
        "answer": query.answer,
        "answered_by": query.answered_by,
        "answered_at": query.answered_at,
        "timestamp": query.created_at,
    }


class UniversityProfilesListView(APIView):
    """
    GET /api/university/<university_id>/profiles/
    Officer-facing shortlist, not a full student directory: only students who
    have shown real interest in this university (searched it or run a fit
    check via their own agent -- see django_api.models.UniversityInterestEvent)
    AND whose latest fit score clears the university's configured
    min_fit_score_threshold. See django_api.services.get_shortlisted_profiles.
    Sorted by match_score descending.

    Every returned profile carries a "priority_tier" (high/medium/low/
    unranked), computed against the university's configured
    priority_tier_bounds (default high>=80, medium>=60, low>=40) -- see
    django_api.services.compute_priority_tier. "tier_counts" in the response
    tallies every interested+scored student by tier regardless of any tier
    filter applied, so a dashboard can render filter buttons with counts.

    Optional query params:
      min_score=<0-100>            Override the university's saved threshold for this request only.
      include_all_interested=true  Show every interested student regardless of score (same as min_score=0).
      priority_tier=high,medium     Comma-separated subset of high/medium/low/unranked to show.

    Each profile also carries account-derived fields where available:
    student_email/is_active/date_joined (from the student's auth.User via
    accounts.Account) and institute_name. Any field
    with no value is omitted entirely rather than sent as null, so the
    frontend can auto-map whatever happens to be present.

    Each row only carries the essentials needed to render a roster card
    (identity, headline stats, match/priority). The full profile -- skills,
    projects, intelligence breakdowns, etc. -- is fetched on demand via
    GET /api/university/<university_id>/profile/<student_id>/ once an
    officer clicks into a card, so this list stays cheap regardless of how
    many students are shortlisted.
    """

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        raw_priority_tier = request.query_params.get("priority_tier")
        priority_tiers = None
        if raw_priority_tier:
            priority_tiers = {tier.strip().lower() for tier in raw_priority_tier.split(",") if tier.strip()}
            invalid_tiers = priority_tiers - {"high", "medium", "low", "unranked"}
            if invalid_tiers:
                return Response(
                    {"error": f"Invalid priority_tier value(s): {', '.join(sorted(invalid_tiers))}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if request.query_params.get("include_all_interested", "").lower() == "true":
            min_score = 0
        else:
            raw_min_score = request.query_params.get("min_score")
            if raw_min_score is None:
                min_score = None
            else:
                try:
                    min_score = max(0, min(100, int(raw_min_score)))
                except ValueError:
                    return Response(
                        {"error": "min_score must be an integer between 0 and 100."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        profiles = []

        for entry in get_shortlisted_profiles(university_id, min_score=min_score, priority_tiers=priority_tiers):
            row = StudentProfile.objects.filter(uuid=entry["student_id"]).first()
            if row is None:
                continue

            row_uuid = str(row.uuid)
            data = load_profile_data(row_uuid)
            assessment = entry["assessment"]

            account = Account.objects.filter(student_profile=row).select_related("user").first()
            listed_student = (
                ListedStudent.objects.filter(claimed_student_id=row_uuid, status=ListedStudent.Status.CLAIMED)
                .select_related("source_list__institute")
                .order_by("-claimed_at")
                .first()
            )

            profile = {
                "profile_id": data.get("student_id"),
                "name": data.get("name"),
                "student_email": (account.user.email if account else None) or data.get("email") or None,
                "institute_name": listed_student.source_list.institute.name if listed_student else None,
                "is_active": account.user.is_active if account else None,
                "date_joined": account.user.date_joined if account else None,
                "profile_image_url": (
                    request.build_absolute_uri(f"/api/profile/{row_uuid}/image/")
                    if row.profile_image_path
                    else None
                ),
                "institution": data.get("institution") or None,
                "major": data.get("major") or None,
                "gpa": data.get("gpa"),
                "gpa_scale": data.get("gpa_scale") or None,
                "overall_profile_score": data.get("overall_profile_score"),
                "profile_completeness": data.get("profile_completeness"),
                "match_tier": assessment.get("match_tier", "unassessed"),
                "match_score": assessment.get("match_score"),
                "priority_tier": entry["priority_tier"],
                "fit_summary": assessment.get("fit_summary", data.get("summary", "")),
                "recommendation": assessment.get("recommendation", "review"),
            }

            profiles.append({key: value for key, value in profile.items() if value is not None})

        return Response({
            "university_id": university_id,
            "priority_tier_bounds": get_priority_tier_bounds(university_id),
            "tier_counts": get_priority_tier_counts(university_id),
            "profiles": profiles,
        })


def _student_in_university_scope_or_404(university_id: str, student_id: str):
    """
    Access boundary for the officer dashboard's per-student endpoints: an
    officer may only reach a student who has actually engaged with their
    university (an UniversityInterestEvent -- searched it, ran a fit check,
    etc.), the same population UniversityProfilesListView is built from.

    Returns an error Response to send back, or None if the student is in
    scope. 404 (not 403) is deliberate -- an officer has no business learning
    whether a given student_id exists at all outside their own scope.
    """
    if student_has_university_interest(student_id, university_id):
        return None
    return api_error(
        "No such student in this university's dashboard.", status.HTTP_404_NOT_FOUND
    )


class UniversityProfileDetailAPIView(APIView):
    """
    GET /api/university/<university_id>/profile/<student_id>/
    Full profile for one student, for the officer dashboard's card
    click-through view. Companion to UniversityProfilesListView, which only
    returns per-student essentials for the roster list -- this is where the
    rest (skills, projects, intelligence breakdowns, etc.) lives.

    university_id is scoped via ScopedToOwnUniversityId; student_id is
    additionally gated to students who have shown interest in this university
    (see _student_in_university_scope_or_404) so one university's officer
    can't read arbitrary students' profiles.
    """

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str, student_id: str):
        scope_error = _student_in_university_scope_or_404(university_id, student_id)
        if scope_error:
            return scope_error

        try:
            profile = get_profile(student_id)
        except FileNotFoundError as exc:
            return api_error(str(exc), status.HTTP_404_NOT_FOUND)

        row = StudentProfile.objects.filter(uuid=student_id).first()
        profile["profile_image_url"] = (
            request.build_absolute_uri(f"/api/profile/{student_id}/image/")
            if row and row.profile_image_path
            else None
        )

        response_data = format_profile_response(profile)

        account = Account.objects.filter(student_profile__uuid=student_id).select_related("user").first()
        listed_student = (
            ListedStudent.objects.filter(claimed_student_id=student_id, status=ListedStudent.Status.CLAIMED)
            .select_related("source_list__institute")
            .order_by("-claimed_at")
            .first()
        )
        assessment = (profile.get("assessments") or {}).get(university_id, {}) or {}
        match_score = assessment.get("match_score")

        response_data["student_email"] = (account.user.email if account else None) or profile.get("email")
        response_data["is_active"] = account.user.is_active if account else None
        response_data["date_joined"] = account.user.date_joined if account else None
        response_data["institute_name"] = listed_student.source_list.institute.name if listed_student else None
        response_data["match_tier"] = assessment.get("match_tier", "unassessed")
        response_data["match_score"] = match_score
        response_data["priority_tier"] = (
            compute_priority_tier(match_score, get_priority_tier_bounds(university_id))
            if match_score is not None
            else "unranked"
        )
        response_data["fit_summary"] = assessment.get("fit_summary", profile.get("summary", ""))
        response_data["recommendation"] = assessment.get("recommendation", "review")

        return Response(response_data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes(UNIVERSITY_OWNER_PERMISSIONS)
def university_profile_presenter_chat(request, university_id: str, student_id: str):
    """
    POST /api/university/<university_id>/profile/<student_id>/chat/
    University-officer-facing chat about one student (ProfilePresenterAgent) —
    distinct from /api/chat/agent/, which is the student-facing agent.
    university_id is scoped via ScopedToOwnUniversityId; student_id is
    additionally gated to students who have shown interest in this university
    (see _student_in_university_scope_or_404), so an officer can't ask about
    -- or trigger a fit assessment for -- an arbitrary student.
    """
    question = request.data.get("question")
    history = request.data.get("history", []) or []

    if not question:
        return api_error("question is required.")

    scope_error = _student_in_university_scope_or_404(university_id, student_id)
    if scope_error:
        return scope_error

    try:
        profile = get_profile(student_id)
    except FileNotFoundError:
        return Response({"answer": "Profile not found."})

    try:
        from agents import commons

        if not (profile.get("assessments") or {}).get(university_id):
            try:
                commons.generate_fit_assessment(student_id, university_id)
                profile = get_profile(student_id)
            except Exception:
                pass

        presenter = commons.get_profile_presenter(university_id)
        answer = presenter.answer(question=question, profile=profile, conversation_history=history)
        log_chat_turn(
            channel=ChatMessage.Channel.PRESENTER,
            student_id=student_id,
            university_id=university_id,
            user_message=question,
            assistant_message=answer or "",
        )
        return Response({"answer": answer})
    except Exception as exc:
        return Response({
            "answer": "Profile Presenter failed.",
            "error": (
                "AI profile explanation failed. Check API key, model name, "
                f"credits, network, or profile data. Details: {exc}"
            ),
        })


@api_view(["GET"])
@permission_classes(UNIVERSITY_OWNER_PERMISSIONS)
def university_profile_presenter_chat_history(request, university_id: str, student_id: str):
    scope_error = _student_in_university_scope_or_404(university_id, student_id)
    if scope_error:
        return scope_error

    limit = chat_history_limit(request)
    base_qs = ChatMessage.objects.filter(
        channel=ChatMessage.Channel.PRESENTER, student_id=student_id, university_id=university_id
    )
    total = base_qs.count()
    messages = list(base_qs.order_by("-created_at")[:limit])[::-1]
    return Response({
        "count": len(messages),
        "total": total,
        "truncated": total > len(messages),
        "messages": [
            {"sender": m.sender, "content": m.content, "created_at": m.created_at, "meta": m.meta}
            for m in messages
        ],
    })


@api_view(["POST"])
@permission_classes(UNIVERSITY_OWNER_PERMISSIONS)
def university_agent_chat(request, university_id: str):
    """
    POST /api/university/<university_id>/chat/
    University-officer-facing chat with their OWN program agent
    (agents.university_agent.UniversityAgent -- the exact same agent a
    student's ask_university tool consults), so an officer can preview/test
    how it answers. Distinct from university_profile_presenter_chat, which
    answers about one student's profile instead of about the program.
    """
    message = request.data.get("message") or request.data.get("question")

    if not message:
        return api_error("message is required.")

    try:
        from agents import commons
        agent = commons.get_university_agent(university_id)
    except ValueError as exc:
        return api_error(str(exc), status.HTTP_404_NOT_FOUND)

    try:
        result = agent.answer(message, caller_role="officer")
        reply = result.get("answer", "")
        log_chat_turn(
            channel=ChatMessage.Channel.UNIVERSITY,
            student_id="",
            university_id=university_id,
            user_message=message,
            assistant_message=reply,
            meta={
                "confidence": result.get("confidence"),
                "trust": result.get("trust"),
                "pending": result.get("pending", False),
                "pending_query": result.get("pending_query"),
                "knowledge_gap": result.get("knowledge_gap", False),
                "source": result.get("source"),
            },
        )
        return Response({
            "university_id": university_id,
            "agent_name": result.get("agent_name"),
            "reply": reply,
            "pending": result.get("pending", False),
            "pending_query": result.get("pending_query"),
            "knowledge_gap": result.get("knowledge_gap", False),
            "unsupported_topics": result.get("unsupported_topics"),
            "confidence": result.get("confidence"),
            "trust": result.get("trust"),
        })
    except Exception as exc:
        return api_error(f"University agent chat failed: {exc}", status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(["GET", "DELETE"])
@permission_classes(UNIVERSITY_OWNER_PERMISSIONS)
def university_agent_chat_history(request, university_id: str):
    """
    GET /api/university/<university_id>/chat/history/
    DELETE /api/university/<university_id>/chat/history/
    """
    if request.method == "DELETE":
        deleted_count, _ = ChatMessage.objects.filter(
            channel=ChatMessage.Channel.UNIVERSITY, university_id=university_id
        ).delete()
        return Response({"status": "ok", "university_id": university_id, "messages_deleted": deleted_count})

    limit = chat_history_limit(request)
    base_qs = ChatMessage.objects.filter(channel=ChatMessage.Channel.UNIVERSITY, university_id=university_id)
    total = base_qs.count()
    messages = list(base_qs.order_by("-created_at")[:limit])[::-1]
    return Response({
        "count": len(messages),
        "total": total,
        "truncated": total > len(messages),
        "messages": [
            {"sender": m.sender, "content": m.content, "created_at": m.created_at, "meta": m.meta}
            for m in messages
        ],
    })


class UniversityQuestionsView(APIView):
    """GET /api/university/<university_id>/questions/ — officer question log."""

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        rows = UniversityQuestionLog.objects.filter(university_id=university_id)
        questions = [
            {
                "university_id": r.university_id,
                "student_name": r.student_name,
                "question": r.question,
                "topic": r.topic,
                "created_at": r.created_at,
            }
            for r in rows
        ]
        return Response({"university_id": university_id, "questions": questions})


class UniversityQueriesView(APIView):
    """GET /api/university/<university_id>/queries/ — all escalated queries for one university."""

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        rows = PendingQuery.objects.filter(university_id=university_id)
        matched = [serialize_pending_query(r) for r in rows]
        return Response({"university_id": university_id, "queries": matched})


class UniversityActiveQueriesView(APIView):
    """GET /api/university/<university_id>/queries/active/ — pending + urgent only."""

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        rows = PendingQuery.objects.filter(university_id=university_id).exclude(
            status__in=[PendingQuery.Status.RESOLVED, PendingQuery.Status.IGNORED]
        )
        active = [serialize_pending_query(r) for r in rows]
        return Response({"university_id": university_id, "queries": active})


class UniversityArchiveQueriesView(APIView):
    """GET /api/university/<university_id>/queries/archive/ — resolved or ignored (i.e. no longer active)."""

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        rows = PendingQuery.objects.filter(
            university_id=university_id,
            status__in=[PendingQuery.Status.RESOLVED, PendingQuery.Status.IGNORED],
        )
        archive = [serialize_pending_query(r) for r in rows]
        return Response({"university_id": university_id, "queries": archive})


class VerifiedKnowledgeView(APIView):
    """GET /api/university/<university_id>/knowledge/verified/ — durable human-verified answers."""

    permission_classes = UNIVERSITY_OWNER_PERMISSIONS

    def get(self, request, university_id: str):
        rows = VerifiedAnswer.objects.filter(university_id=university_id)
        matched = [
            {
                "query_id": r.query_id,
                "university_id": r.university_id,
                "question": r.question,
                "answer": r.answer,
                "answered_by": r.answered_by,
                "source": r.source,
                "source_type": "human_verified",
                "confidence": r.confidence,
                "synced_at": r.created_at,
            }
            for r in rows
        ]

        return Response({"university_id": university_id, "verified_answers": matched})


class EditPendingQueryView(APIView):
    """
    POST /api/queries/<query_id>/edit/
    Updates the answer on a query, including one that's already resolved —
    unlike /api/queries/answer/, which refuses to touch an already-resolved query.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def post(self, request, query_id: int):
        answer = request.data.get("answer")
        answered_by = request.data.get("answered_by", "Admin")

        if not answer:
            return Response({"status": "failed", "message": "answer is required"}, status=status.HTTP_400_BAD_REQUEST)

        selected_query, error = _get_scoped_pending_query(request, query_id)
        if error:
            return error

        university_id = selected_query.university_id

        try:
            from agents import commons
            agent = commons.get_university_agent(university_id)
            resolved = agent.resolve_pending_query(query_id=query_id, answer=answer, answered_by=answered_by)
            if not resolved:
                return Response({"status": "failed", "message": "Could not edit query."}, status=status.HTTP_400_BAD_REQUEST)
            return Response(
                {"status": "success", "message": "Query answer updated", "query_id": query_id, "university_id": university_id},
                status=status.HTTP_200_OK,
            )
        except Exception as exc:
            return Response(
                {"status": "failed", "message": "Failed to update query answer.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class IgnorePendingQueryView(APIView):
    """
    POST /api/queries/<query_id>/ignore/
    Soft-dismisses a query (e.g. a duplicate, spam, or something already
    handled outside the system) without providing a real answer -- unlike
    /api/queries/answer/, which requires one and marks the query resolved.
    The row (and the student's original question) stays in the database for
    history/audit; it just drops out of the active pending list. An officer
    can still answer it normally later via /api/queries/answer/ or
    /api/queries/<id>/edit/, which will move it out of "ignored" the same
    way any other status transition would.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def post(self, request, query_id: int):
        from django.utils import timezone

        reason = request.data.get("reason", "")
        ignored_by = request.data.get("ignored_by", "Admin")

        selected_query, error = _get_scoped_pending_query(request, query_id)
        if error:
            return error

        university_id = selected_query.university_id

        if selected_query.status == PendingQuery.Status.RESOLVED:
            return Response(
                {"status": "failed", "message": f"Query {query_id} is already resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        selected_query.status = PendingQuery.Status.IGNORED
        selected_query.answer = reason
        selected_query.answered_by = ignored_by
        selected_query.answered_at = timezone.now()
        selected_query.save()

        return Response(
            {"status": "success", "message": "Query ignored", "query_id": query_id, "university_id": university_id},
            status=status.HTTP_200_OK,
        )


class DeletePendingQueryView(APIView):
    """
    DELETE /api/queries/<query_id>/
    Permanently removes a query row -- for spam, test data, or anything
    that shouldn't exist at all, as opposed to /api/queries/<id>/ignore/,
    which dismisses a real query while keeping its record. There is no
    undo; prefer ignore/ for anything worth keeping a history of.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsUniversityRole]

    def delete(self, request, query_id: int):
        selected_query, error = _get_scoped_pending_query(request, query_id)
        if error:
            return error

        selected_query.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
