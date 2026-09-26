"""Chat GitHub access uses the student's OAuth-owned LangGraph extraction."""
from langchain_core.tools import tool


def github_evidence(student_id):
    from accounts.github_oauth import get_connection_for_student_id
    from django_api.models import GitHubProfileSnapshot
    connection = get_connection_for_student_id(student_id)
    if not connection:
        return {'status': 'not_connected', 'action': 'Connect GitHub in your profile'}
    row = GitHubProfileSnapshot.objects.filter(student__uuid=student_id, connection=connection, github_user_id=connection.github_user_id).first()
    if not row:
        return {'status': 'not_synced', 'username': connection.github_username}
    run = row.runs.first()
    processing = bool(run and run.status in ('queued', 'running'))
    result = {'status': 'processing' if processing else (run.status if run else 'not_synced'),
        'username': connection.github_username, 'progress': run.progress if run else '',
        'job_id': str(run.pk) if run else None, 'synced_at': row.synced_at.isoformat() if row.synced_at else None}
    if processing:
        result['instruction'] = 'Analysis is under process. Tell the student; do not infer new GitHub findings until completion.'
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
        """Request/resume background LangGraph analysis of the student's connected GitHub account."""
        from accounts.github_oauth import get_connection_for_student_id
        from github_profiles.sync import queue_sync
        connection = get_connection_for_student_id(ctx['canonical_student_id'])
        if not connection:
            return {'status': 'not_connected', 'action': 'Connect GitHub on the profile screen first.'}
        if github_input and github_input.rstrip('/').split('/')[-1].casefold() != connection.github_username.casefold():
            return {'error': 'This tool only analyzes your connected GitHub account.'}
        queue_sync(ctx['canonical_student_id'])
        return github_evidence(ctx['canonical_student_id'])

    return [get_github_processing_status, analyze_github_profile]
