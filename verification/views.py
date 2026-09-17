from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudentRole, IsTOTPEnrolled
from verification import services

STUDENT_PERMISSIONS = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole]


def _api_error(message: str, http_status=status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"status": "error", "message": str(message)}, status=http_status)


class VerificationStatusAPIView(APIView):
    """
    GET /api/verification/status/
    Full verification state, including every item (open and resolved).
    Always recomputed live -- safe to poll after any reupload.

    Triggering a fresh check and acting on flagged items (confirm/ignore/
    clarify) is chat-only now -- the student's personal agent runs
    verification and records decisions via agents.commons; this endpoint
    (and VerificationItemListAPIView below) exist only so a "past
    mismatches" screen can read that history directly.
    """

    permission_classes = STUDENT_PERMISSIONS

    def get(self, request):
        student_id = request.user.account.student_uuid
        return Response(services.run_verification(student_id, user=request.user), status=status.HTTP_200_OK)


class VerificationItemListAPIView(APIView):
    """
    GET /api/verification/items/?status=<filter> (default: open)
    The "verification pending list" -- one row per flagged disagreement.
    Read-only history; does not trigger a reanalysis and cannot resolve an
    item -- both of those happen only through chat with the student's
    personal agent.

    `status` accepts either a broad bucket (`open`, `resolved`, `all`) or
    one specific outcome (`confirmed`, `ignored`, `clarified`,
    `auto_cleared`, `superseded`) so a "history" screen can filter down to
    exactly one kind of resolution. `summary` in the response always has
    counts for every outcome, regardless of which filter was requested, so
    a single call can drive both the filtered list and a tab/count row.
    """

    permission_classes = STUDENT_PERMISSIONS

    def get(self, request):
        student_id = request.user.account.student_uuid
        filter_status = str(request.query_params.get("status", "open")).strip().lower()

        if filter_status not in services.VALID_ITEM_FILTERS:
            return _api_error(f"status must be one of {sorted(services.VALID_ITEM_FILTERS)}.")

        result = services.list_items(student_id, filter_status)
        return Response(
            {
                "student_id": student_id,
                "status_filter": filter_status,
                "count": len(result["items"]),
                "items": result["items"],
                "summary": result["summary"],
            },
            status=status.HTTP_200_OK,
        )
