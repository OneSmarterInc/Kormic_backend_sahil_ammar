from __future__ import annotations

from datetime import timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsTOTPEnrolled, get_account
from notifications.models import NotificationLog, PushToken

#  keep the page small so one poll can't pull a large backlog (and a large `data`
# blob per row) in a single response.
POLL_LATEST_LIMIT_DEFAULT = 5
POLL_LATEST_LIMIT_MAX = 20


class RegisterPushTokenView(APIView):
    """
    POST /api/notifications/register-token/
    Body: {"token": "ExponentPushToken[...]", "platform": "ios" | "android"}

    Called by the Expo app right after it obtains its push token (typically
    on login and on app start). Re-registering an existing token reassigns
    it to the current account -- correct behavior when a different student
    logs in on a previously-used device.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled]

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        if not token:
            return Response({"error": "token is required."}, status=status.HTTP_400_BAD_REQUEST)

        platform = request.data.get("platform", PushToken.Platform.UNKNOWN)
        if platform not in PushToken.Platform.values:
            platform = PushToken.Platform.UNKNOWN

        account = get_account(request)
        if account is None:
            return Response({"error": "No account associated with this user."}, status=status.HTTP_403_FORBIDDEN)

        push_token, _ = PushToken.objects.update_or_create(
            token=token,
            defaults={"account": account, "platform": platform, "is_active": True, "last_error": ""},
        )
        return Response(
            {"id": push_token.id, "token": push_token.token, "platform": push_token.platform},
            status=status.HTTP_200_OK,
        )


class UnregisterPushTokenView(APIView):
    """
    POST /api/notifications/unregister-token/
    Body: {"token": "ExponentPushToken[...]"}

    Called on logout / notification permission revocation so a signed-out
    device stops receiving pushes for the account that registered it.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled]

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        if not token:
            return Response({"error": "token is required."}, status=status.HTTP_400_BAD_REQUEST)

        account = get_account(request)
        updated = PushToken.objects.filter(token=token, account=account).update(is_active=False)
        return Response({"deactivated": bool(updated)}, status=status.HTTP_200_OK)


class PollNotificationsView(APIView):
    """
    GET /api/notifications/poll/?since=<ISO8601 timestamp>&limit=<int>

    Testing/fallback path for clients where real Expo push doesn't work
    (web, Expo Go). Not used by the production mobile app -- that relies on
    actual push delivery. Returns NotificationLog rows for the authenticated
    account, newest first, optionally filtered to those created after
    `since`. Pass the `server_time` from this response as `since` on the
    next poll (safer than the last item's `created_at`, which can tie with
    another row created in the same instant).

    This is a lightweight "what did I miss" check, not a history feed:
    `limit` defaults to POLL_LATEST_LIMIT_DEFAULT (5) and is hard-capped at
    POLL_LATEST_LIMIT_MAX (20) so a poll can never pull a large backlog in
    one call.
    """

    permission_classes = [IsAuthenticated, IsTOTPEnrolled]

    def get(self, request):
        account = get_account(request)
        if account is None:
            return Response({"error": "No account associated with this user."}, status=status.HTTP_403_FORBIDDEN)

        qs = NotificationLog.objects.filter(account=account)

        since = request.query_params.get("since")
        if since:
            since_dt = parse_datetime(since)
            if since_dt is None:
                return Response(
                    {"error": "Invalid 'since' timestamp; use ISO 8601."}, status=status.HTTP_400_BAD_REQUEST
                )
            if timezone.is_naive(since_dt):
                since_dt = timezone.make_aware(since_dt, dt_timezone.utc)
            qs = qs.filter(created_at__gt=since_dt)

        try:
            limit = min(
                max(int(request.query_params.get("limit", POLL_LATEST_LIMIT_DEFAULT)), 1),
                POLL_LATEST_LIMIT_MAX,
            )
        except (TypeError, ValueError):
            limit = POLL_LATEST_LIMIT_DEFAULT

        logs = list(qs.order_by("-created_at")[:limit])

        return Response(
            {
                "results": [
                    {
                        "id": log.id,
                        "event_type": log.event_type,
                        "title": log.title,
                        "body": log.body,
                        "data": log.data,
                        "status": log.status,
                        "created_at": log.created_at.isoformat(),
                    }
                    for log in logs
                ],
                "server_time": timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK,
        )
