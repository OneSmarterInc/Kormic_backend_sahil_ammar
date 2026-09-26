from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView
from accounts.permissions import IsStudentRole, IsSuperUserRole, IsTOTPEnrolled
from .models import InformationPolicy, PublicUniversity
from .services import reference, queue_research


class RefreshThrottle(UserRateThrottle):
    rate = '20/hour'
    scope = 'university_research_refresh'


class UniversityResearchView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsStudentRole | IsSuperUserRole]

    def get(self, request, university_id):
        row = get_object_or_404(PublicUniversity, pk=university_id)
        return Response(reference(row))


class UniversityRefreshView(UniversityResearchView):
    throttle_classes = [RefreshThrottle]

    def post(self, request, university_id):
        row = get_object_or_404(PublicUniversity, pk=university_id)
        try:
            run = queue_research(row, request.user)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=429)
        return Response({'job_id': str(run.pk), 'status': run.status, 'university': reference(row)}, status=202)


class InformationPolicyView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]

    def get(self, request):
        policy, _ = InformationPolicy.objects.get_or_create(pk=1)
        return Response({'refresh_days': policy.refresh_days, 'updated_at': policy.updated_at})

    def patch(self, request):
        value = request.data.get('refresh_days')
        if type(value) is not int or not 1 <= value <= 365:
            return Response({'detail': 'Refresh age must be a whole number between 1 and 365 days.'}, status=400)
        policy, _ = InformationPolicy.objects.get_or_create(pk=1)
        policy.refresh_days, policy.updated_by = value, request.user
        policy.save()
        return Response({'refresh_days': policy.refresh_days, 'updated_at': policy.updated_at})


class ResearchUniversityListView(APIView):
    permission_classes = [IsAuthenticated, IsTOTPEnrolled, IsSuperUserRole]

    def get(self, request):
        try:
            page = int(request.query_params.get('page', 1))
            if page < 1:
                raise ValueError
        except (ValueError, TypeError):
            return Response({'detail': 'Invalid page.'}, status=400)
        rows = PublicUniversity.objects.all().order_by('name')
        if request.query_params.get('search'):
            rows = rows.filter(name__icontains=request.query_params['search'][:200])
        count = rows.count()
        results = []
        for row in rows[(page-1)*10:page*10]:
            item = reference(row)
            run = row.runs.order_by('-created_at').first()
            item.update(courses=row.courses.count(), intakes=row.intakes.count(),
                last_error=run.error if run and run.status == 'failed' else '',
                job_status=run.status if run else 'not_started')
            results.append(item)
        return Response({'count': count, 'page': page, 'total_pages': max(1, (count+9)//10), 'results': results})
