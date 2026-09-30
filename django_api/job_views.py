from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from accounts.permissions import IsTOTPEnrolled
from rest_framework.permissions import IsAuthenticated
from django_api.models import AgentJob
from pure_multi_agent.jobs import serialize


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsTOTPEnrolled])
def job_status(request, job_id=None):
    account = request.user.account
    if account.role == "student":
        key = "student:" + str(account.student_uuid)
    elif account.role == "university" and account.university_uuid:
        key = "university:" + str(account.university_uuid)
    else:
        return Response({"message": "Not found."}, status=404)
    if account.role == 'university':
        # Ownership remains university-scoped even though execution locks are per conversation.
        rows = AgentJob.objects.filter(university_id=str(account.university_uuid))
        if job_id is None:
            subject = request.query_params.get('student_id', '')
            rows = rows.filter(owner_key=key + (':student:' + subject if subject else ''))
    else:
        rows = AgentJob.objects.filter(student_id=str(account.student_uuid), university_id='') if job_id else AgentJob.objects.filter(owner_key=key)
    job = rows.filter(pk=job_id).first() if job_id else rows.filter(status__in=["queued", "processing"]).first()
    if job is None and job_id is None:
        latest = rows.order_by("-created_at").first()
        if latest and latest.status == "failed":
            job = latest
    if job is None:
        return Response({"status": "idle"}) if job_id is None else Response({"message": "Not found."}, status=404)
    if request.method == 'POST':
        if job_id is None or job.status != 'completed':
            return Response({'message':'A completed reply must be received first.'},status=409)
        from university_research.services import publish_delivered_evidence
        publish_delivered_evidence(job.pk)
        return Response({'status':'saved'})
    return Response(serialize(job))


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsTOTPEnrolled])
def agent_activity(request):
    from pure_multi_agent.activity import read_activity
    account = request.user.account
    if account.role == 'student' and account.student_uuid:
        key = 'student:' + str(account.student_uuid)
    elif account.role == 'university' and account.university_uuid:
        key = 'university:' + str(account.university_uuid)
    else:
        return Response({'message': 'Not found.'}, status=404)
    return Response(read_activity(key))
