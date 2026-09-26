from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from accounts.permissions import IsTOTPEnrolled
from rest_framework.permissions import IsAuthenticated
from django_api.models import AgentJob
from pure_multi_agent.jobs import serialize


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTOTPEnrolled])
def job_status(request, job_id=None):
    account = request.user.account
    if account.role == "student":
        key = "student:" + str(account.student_uuid)
    elif account.role == "university" and account.university_uuid:
        key = "university:" + str(account.university_uuid)
    else:
        return Response({"message": "Not found."}, status=404)
    rows = AgentJob.objects.filter(owner_key=key)
    job = rows.filter(pk=job_id).first() if job_id else rows.filter(status__in=["queued", "processing"]).first()
    if job is None and job_id is None:
        latest = rows.order_by("-created_at").first()
        if latest and latest.status == "failed":
            job = latest
    if job is None:
        return Response({"status": "idle"}) if job_id is None else Response({"message": "Not found."}, status=404)
    return Response(serialize(job))
