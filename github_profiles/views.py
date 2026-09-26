import re

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStudentRole, IsTOTPEnrolled
from accounts.github_oauth import get_connection_for_user
from django_api.models import GitHubProfileSnapshot, GitHubSyncRun

PERMISSIONS = [IsAuthenticated, IsStudentRole, IsTOTPEnrolled]


def run_payload(run, include_result=False):
    data = {'job_id': str(run.pk), 'status': run.status, 'progress': run.progress, 'error': run.error,
        'updated_at': run.updated_at, 'poll_after_ms': 3000,
        'stage': run.stage, 'model_calls': run.model_calls,
        'completed_repositories': len(run.work.get('reports', {})),
        'total_repositories': len(run.work.get('repositories', []))}
    if include_result and run.status == 'completed':
        data['result'] = run.result
    return data


class GitHubSyncStatusView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request, job_id):
        run = GitHubSyncRun.objects.filter(pk=job_id, profile__student_id=request.user.account.student_profile_id,
            profile__connection__user=request.user).first()
        if run is None or run.profile.github_user_id != run.profile.connection.github_user_id:
            return Response({'detail': 'GitHub sync not found.'}, status=404)
        return Response(run_payload(run, include_result=True))


class GitHubOverviewView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request):
        connection = get_connection_for_user(request.user)
        if connection is None:
            return Response({'connected': False, 'profile': None, 'sync': None})
        snapshot = GitHubProfileSnapshot.objects.filter(connection=connection, github_user_id=connection.github_user_id,
            student_id=request.user.account.student_profile_id).first()
        if snapshot is None:
            return Response({'connected': True, 'profile': None, 'sync': None})
        # The dedicated overview never exposes per-repository descriptions, README
        # bodies or source snippets. Those remain in the normalized private store.
        overview = re.sub(r'## Project Experience\n.*?(?=\n## |\Z)', '', snapshot.summary, flags=re.S).strip()
        latest = snapshot.runs.first()
        return Response({'connected': True, 'sync': run_payload(latest) if latest else None,
            'profile': {'identity': snapshot.identity, 'overview': overview, 'statistics': snapshot.statistics,
                'languages': snapshot.languages, 'technologies': snapshot.technologies, 'domains': snapshot.domains,
                'coverage': snapshot.coverage, 'warnings': snapshot.warnings, 'synced_at': snapshot.synced_at}})


class GitHubRepositoriesView(APIView):
    permission_classes = PERMISSIONS

    def get(self, request):
        try:
            page = int(request.query_params.get('page', '1'))
            if page < 1:
                raise ValueError
        except (ValueError, TypeError):
            return Response({'detail': 'Page must be a positive integer.'}, status=400)
        connection = get_connection_for_user(request.user)
        snapshot = GitHubProfileSnapshot.objects.filter(connection=connection,
            github_user_id=connection.github_user_id, student_id=request.user.account.student_profile_id).first() if connection else None
        queryset = snapshot.repositories.filter(active=True) if snapshot else None
        count = queryset.count() if queryset is not None else 0
        pages = max(1, (count + 9) // 10)
        if page > pages:
            return Response({'detail': 'Repository page not found.'}, status=404)
        rows = list(queryset.values('id', 'full_name')[(page-1)*10:page*10]) if queryset is not None else []
        return Response({'count': count, 'page': page, 'page_size': 10, 'total_pages': pages,
            'results': [{'id': r['id'], 'name': r['full_name']} for r in rows]})
