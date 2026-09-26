"""Public research is separate from enrolled universities and private student data."""
import uuid
from django.conf import settings
from django.db import models
from pgvector.django import VectorField


class InformationPolicy(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    refresh_days = models.PositiveSmallIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)


class PublicUniversity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity_key = models.CharField(max_length=64, unique=True)
    registered_university = models.OneToOneField('universities.University', null=True, blank=True, on_delete=models.SET_NULL, related_name='website_research')
    name = models.CharField(max_length=400, db_index=True)
    country = models.CharField(max_length=120, blank=True)
    address = models.TextField(blank=True)
    website = models.URLField(max_length=1000)
    discovery_sources = models.JSONField(default=list)
    fetched_at = models.DateTimeField(null=True, db_index=True)
    coverage = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class ResearchRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    university = models.ForeignKey(PublicUniversity, on_delete=models.CASCADE, related_name='runs')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20, default='queued', db_index=True)
    progress = models.CharField(max_length=300, default='Waiting to research the official website')
    error = models.CharField(max_length=500, blank=True)
    # One LangGraph node per lease; state is checkpointed atomically with release.
    state = models.JSONField(default=dict)
    steps = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(db_index=True)
    lease_token = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['university'], condition=models.Q(status__in=['queued', 'running']), name='research_one_active')]
        indexes = [models.Index(fields=['status', 'available_at'], name='research_dispatch')]


class UniversityPage(models.Model):
    university = models.ForeignKey(PublicUniversity, on_delete=models.CASCADE, related_name='pages')
    url = models.URLField(max_length=1000)
    title = models.CharField(max_length=500, blank=True)
    content = models.TextField()
    content_hash = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()
    run = models.ForeignKey(ResearchRun, null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['university', 'url'], name='research_unique_page')]


class UniversityFact(models.Model):
    university = models.ForeignKey(PublicUniversity, on_delete=models.CASCADE, related_name='facts')
    page = models.ForeignKey(UniversityPage, on_delete=models.CASCADE)
    topic = models.CharField(max_length=200)
    content = models.TextField()
    source_quote = models.TextField()
    fetched_at = models.DateTimeField(db_index=True)
    embedding = VectorField(dimensions=384, null=True, blank=True)
    embedding_model = models.CharField(max_length=100, blank=True)


class UniversityCourse(models.Model):
    university = models.ForeignKey(PublicUniversity, on_delete=models.CASCADE, related_name='courses')
    page = models.ForeignKey(UniversityPage, on_delete=models.CASCADE)
    name = models.CharField(max_length=400)
    level = models.CharField(max_length=100, blank=True)
    duration = models.CharField(max_length=150, blank=True)
    study_mode = models.CharField(max_length=150, blank=True)
    tuition = models.CharField(max_length=500, blank=True)
    currency = models.CharField(max_length=20, blank=True)
    requirements = models.TextField(blank=True)
    source_quote = models.TextField()
    fetched_at = models.DateTimeField()


class UniversityIntake(models.Model):
    university = models.ForeignKey(PublicUniversity, on_delete=models.CASCADE, related_name='intakes')
    page = models.ForeignKey(UniversityPage, on_delete=models.CASCADE)
    course_name = models.CharField(max_length=400, blank=True)
    term = models.CharField(max_length=200)
    year = models.PositiveSmallIntegerField(null=True)
    deadline = models.CharField(max_length=300, blank=True)
    applicant_scope = models.CharField(max_length=300, blank=True)
    source_quote = models.TextField()
    fetched_at = models.DateTimeField()


class UniversitySearch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey('django_api.StudentProfile', on_delete=models.CASCADE)
    query = models.CharField(max_length=500)
    candidates = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class AdvisingArtifact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey('django_api.StudentProfile', on_delete=models.CASCADE, related_name='advising_artifacts')
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=300)
    content = models.JSONField(default=dict)
    sources = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
