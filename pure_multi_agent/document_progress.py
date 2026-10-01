"""Student-safe progress; no prompts, extracted data or internal errors."""
from pure_multi_agent.job_recovery import execution
from django_api.models import AgentJob

LABELS = {
    'queued': 'Upload received. Waiting for your document agent.',
    'reading': 'Reading your uploaded files.',
    'extracting': 'Extracting your profile details.',
    'saving': 'Updating your profile.',
    'completed': 'Your profile is ready.',
    'failed': 'Processing could not finish. Please try again.',
}

def report(stage):
    current = execution.get()
    if current and stage in LABELS:
        job_id, token = current
        AgentJob.objects.filter(pk=job_id, execution_token=token, status='processing',
            kind__in=['resume', 'linkedin']).update(result={'public_stage': stage})

def public_progress(job):
    stage = job.status if job.status in ('queued', 'completed', 'failed') else (job.result or {}).get('public_stage', 'reading')
    return {'stage': stage if stage in LABELS else 'reading', 'label': LABELS.get(stage, LABELS['reading'])}
