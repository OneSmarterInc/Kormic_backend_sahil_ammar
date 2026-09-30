"""Opt-in asynchronous upload extraction; older installed clients retain compatibility."""
import uuid
from contextlib import ExitStack
from pathlib import Path
from django.conf import settings
from django.core.files import File
from django.db import transaction
from rest_framework.response import Response
from django_api.models import AgentJob, AgentQueueGate
from pure_multi_agent.capacity import lease, AgentBusy
from pure_multi_agent.jobs import serialize, dispatch


def submit_document(request, kind, files):
    from django_api.services import validate_upload, save_uploaded_file, relative_upload_path, RESUME_ALLOWED_TYPES, LINKEDIN_ALLOWED_TYPES
    if len(files) > 10:
        return Response({'message':'Upload at most ten documents at once.'}, status=400)
    sid = str(request.user.account.student_uuid)
    key = 'student:' + sid + ':documents'
    idem = request.headers.get('Idempotency-Key') or str(uuid.uuid4())
    if len(idem) > 100:
        return Response({'message':'Idempotency-Key is too long.'}, status=400)
    for uploaded in files:
        validate_upload(uploaded, allowed_types=RESUME_ALLOWED_TYPES if kind == 'resume' else LINKEDIN_ALLOWED_TYPES, label=kind)
    try:
        with lease('thread:' + key, ttl=120), lease('queue:admission', ttl=10, wait=2), transaction.atomic():
            AgentQueueGate.objects.get_or_create(pk=1)
            AgentQueueGate.objects.select_for_update().get(pk=1)
            previous = AgentJob.objects.filter(owner_key=key, idempotency_key=idem).first()
            if previous:
                return Response(serialize(previous), status=202)
            if AgentJob.objects.filter(owner_key=key, kind=kind, status__in=['queued','processing']).exists():
                return Response({'message':'Your previous document is still processing.'}, status=409)
            if AgentJob.objects.filter(status__in=['queued','processing']).count() >= settings.AGENT_QUEUE_CAPACITY:
                return Response({'message':'Extraction queue is full. Please retry shortly.'}, status=429)
            documents = [{'path':relative_upload_path(save_uploaded_file(sid, upload, 'queued-documents')),
                'name':Path(upload.name).name, 'content_type':getattr(upload,'content_type','')} for upload in files]
            job = AgentJob.objects.create(owner_key=key, idempotency_key=idem, kind=kind, student_id=sid,
                payload={'files':documents, 'origin':request.build_absolute_uri('/').rstrip('/')})
            from pure_multi_agent.telemetry import emit
            emit('RUN_START', 'CV upload' if kind == 'resume' else 'LinkedIn import',
                actor='CV Agent' if kind == 'resume' else 'LinkedIn Agent', student_id=sid, run_id=job.pk,
                inputs={'activity_type':'cv_upload' if kind == 'resume' else 'linkedin_import', 'filenames':[entry['name'] for entry in documents]},
                outputs={'summary':('CV Agent' if kind == 'resume' else 'LinkedIn Agent') + ' received the upload and queued extraction.'})
            transaction.on_commit(lambda: dispatch(job.pk))
        return Response(serialize(job), status=202)
    except AgentBusy:
        return Response({'message':'Another upload is being submitted. Retry shortly.'}, status=409)


from pure_multi_agent.telemetry import trace_document_job


@trace_document_job
def run_document(job):
    from django_api.services import resolve_upload_path, parse_resume, analyze_linkedin
    from pure_multi_agent.document_progress import report
    report('reading')
    with ExitStack() as stack:
        uploads = []
        for entry in job.payload['files']:
            upload = File(stack.enter_context(resolve_upload_path(entry['path']).open('rb')), name=entry['name'])
            upload.content_type = entry['content_type']
            uploads.append(upload)
        if job.kind == 'resume':
            result = parse_resume(job.student_id, uploads[0])
            result['resume_url'] = job.payload['origin'] + f"/api/profile/resume/{result['resume_id']}/"
        else:
            from github_profiles.scheduling import CapacityBusy
            from pure_multi_agent.capacity import ResumeTurnLater
            try:
                result = analyze_linkedin(job.student_id, uploads)
            except (CapacityBusy, AgentBusy) as exc:
                # Extraction has not committed profile data. Keep the original
                # upload and retry through the durable queue, not the UI.
                raise ResumeTurnLater({}, delay=getattr(exc, 'delay', 10)) from exc
            result['images'] = [{'index':i, 'uploaded_image_url':job.payload['origin'] + f"/api/profile/linkedin/{result['analysis_id']}/images/{i}/"}
                for i in range(len(result.pop('image_paths', [])))]
        result['status'] = 'success'
        return result, None, {}


def cleanup_staging(job):
    from django_api.services import resolve_upload_path
    for entry in job.payload.get('files', []):
        path = resolve_upload_path(entry['path'])
        if 'queued-documents' in path.parts:
            path.unlink(missing_ok=True)
