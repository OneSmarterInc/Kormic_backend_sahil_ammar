"""Chat GitHub access uses the student's OAuth-owned LangGraph extraction."""
from langchain_core.tools import tool
from pure_multi_agent.telemetry import traced_operation


@traced_operation('GitHub Agent', mode='saved_evidence')
def github_evidence(student_id):
    from accounts.github_oauth import get_connection_for_student_id
    from django_api.models import GitHubProfileSnapshot
    connection = get_connection_for_student_id(student_id)
    if not connection:
        return {'status': 'not_connected', 'action': 'Open Profile, select Connect GitHub, and authorize the GitHub OAuth connection. Return to Kormic after authorization.'}
    row = GitHubProfileSnapshot.objects.filter(student__uuid=student_id, connection=connection, github_user_id=connection.github_user_id).first()
    if not row:
        return {'status': 'not_synced', 'username': connection.github_username}
    run = row.runs.first()
    processing = bool(run and run.status in ('queued', 'running'))
    result = {'status': 'processing' if processing else (run.status if run else 'not_synced'),
        'username': connection.github_username, 'progress': run.progress if run else '',
        'job_id': str(run.pk) if run else None, 'synced_at': row.synced_at.isoformat() if row.synced_at else None}
    if processing:
        saved = row.student.github_assessment or {}
        if isinstance(saved, dict) and str(saved.get('username', '')).casefold() == connection.github_username.casefold():
            result['saved_analysis'] = saved
            result['saved_analysis_available'] = True
            result['instruction'] = 'A refresh is running, but saved_analysis is the previous completed analysis. Answer review questions from that evidence now, label its generated_at date, and do not start another analysis unless explicitly requested. Do not claim these are results of the running refresh.'
        else:
            result['saved_analysis_available'] = False
            result['instruction'] = 'No previous completed analysis is available for this connected account. Report actual progress; do not invent findings or promise an automatic follow-up.'
    elif row.synced_at:
        result.update(summary=row.summary, technologies=row.technologies, languages=row.languages,
            domains=row.domains, academic_guidance=row.academic_guidance, coverage=row.coverage, warnings=row.warnings)
    if run and run.status == 'failed':
        result['error'] = run.error
    return result


def build_tools(ctx):
    @tool
    def get_github_processing_status() -> dict:
        """Read the connected student's live GitHub sync state and completed findings. Check before GitHub advice."""
        return github_evidence(ctx['canonical_student_id'])

    @tool
    def analyze_github_profile(github_input: str = '') -> dict:
        """Start a NEW sync only when the student explicitly asks to refresh,
        rescan or run a new analysis of their LINKED GitHub account. For review,
        advice, follow-ups or previous results use get_github_processing_status.
        Never call if the student says not to analyze again. Reuses an active job. Returns processing status;
        never claim completed extraction before the worker finishes. Other
        people's usernames and uploads must not replace this OAuth-owned source."""
        from accounts.github_oauth import get_connection_for_student_id
        from github_profiles.sync import queue_sync
        connection = get_connection_for_student_id(ctx['canonical_student_id'])
        if not connection:
            return {'status': 'not_connected', 'action': 'Connect GitHub on the profile screen first.'}
        if github_input and github_input.rstrip('/').split('/')[-1].casefold() != connection.github_username.casefold():
            return {'error': 'This tool only analyzes your connected GitHub account.'}
        from rest_framework.exceptions import ValidationError
        try:
            queue_sync(ctx['canonical_student_id'])
        except ValidationError:
            return {'status': 'selection_required', 'action': 'Open the GitHub page, select between one and five repositories, then choose Analyse selected repositories.'}
        return github_evidence(ctx['canonical_student_id'])

    return [get_github_processing_status, analyze_github_profile]
