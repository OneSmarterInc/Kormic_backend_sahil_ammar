import uuid

from django.db import models
from django.utils import timezone
from pgvector.django import VectorField


class StudentProfile(models.Model):
    """
    Persistent student profile, replacing the previous profiles/<student_id>.json file store.

    Fields that always have a well-known scalar shape are modeled as real
    columns. Nested/list structures produced by the various agents (resume
    parser, GitHub/LinkedIn analysis, fit assessments, roadmap planner, etc.)
    are modeled as dedicated JSON columns. `extra_data` is an overflow bucket
    for any additional keys those AI-driven agents may attach to a profile
    that aren't covered by an explicit column, so no data is ever dropped.
    """

    # Public, non-guessable identifier used in every API URL, cache key, and
    # cross-table string reference. The integer auto `id` stays purely
    # internal (FKs, joins, admin). Replaces the previous email-derived slug.
    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    # Display name for this student's personal agent -- the single entry
    # point the student talks to. Auto-assigned on first use (see
    # agents.agent_identity.generate_unique_agent_name) and student-editable
    # afterward. null=True (not "") so multiple not-yet-assigned rows don't
    # collide on the unique constraint.
    agent_name = models.CharField(max_length=100, unique=True, null=True, blank=True, db_index=True)

    name = models.CharField(max_length=255, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")
    country = models.CharField(max_length=255, blank=True, default="")
    institution = models.CharField(max_length=500, blank=True, default="")
    major = models.CharField(max_length=255, blank=True, default="")
    program = models.CharField(max_length=255, blank=True, default="")
    graduation_year = models.IntegerField(null=True, blank=True)

    gpa = models.FloatField(null=True, blank=True)
    gpa_scale = models.CharField(max_length=50, blank=True, default="")
    gpa_text = models.CharField(max_length=100, blank=True, default="")

    gre_quant = models.FloatField(null=True, blank=True)
    gre_verbal = models.FloatField(null=True, blank=True)
    toefl = models.FloatField(null=True, blank=True)
    ielts = models.FloatField(null=True, blank=True)
    english_score_text = models.CharField(max_length=100, blank=True, default="")

    budget = models.FloatField(null=True, blank=True)
    budget_text = models.CharField(max_length=100, blank=True, default="")
    work_months = models.FloatField(null=True, blank=True, default=0)

    github = models.CharField(max_length=500, blank=True, default="")
    github_assessment = models.JSONField(default=dict, blank=True)

    linkedin_url = models.CharField(max_length=500, blank=True, default="")
    linkedin_profile = models.JSONField(default=dict, blank=True)

    profile_image_path = models.CharField(max_length=1000, blank=True, default="")

    notes = models.TextField(blank=True, default="")
    source = models.CharField(max_length=100, blank=True, default="api")
    verified = models.BooleanField(default=False)

    skills = models.JSONField(default=list, blank=True)
    technical_skills = models.JSONField(default=list, blank=True)
    soft_skills = models.JSONField(default=list, blank=True)

    projects = models.JSONField(default=list, blank=True)

    research = models.TextField(blank=True, default="")
    research_interests = models.JSONField(default=list, blank=True)
    publications = models.JSONField(default=list, blank=True)
    publications_count = models.IntegerField(null=True, blank=True)

    career_goals = models.JSONField(default=list, blank=True)
    conversation_insights = models.JSONField(default=list, blank=True)
    assessments = models.JSONField(default=dict, blank=True)
    preferences = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)

    academic_intelligence = models.JSONField(default=dict, blank=True)
    technical_intelligence = models.JSONField(default=dict, blank=True)
    research_intelligence = models.JSONField(default=dict, blank=True)
    behaviour_intelligence = models.JSONField(default=dict, blank=True)

    overall_profile_score = models.IntegerField(default=0, blank=True)
    overall_profile = models.JSONField(default=dict, blank=True)
    profile_completeness = models.JSONField(default=dict, blank=True)

    strengths = models.JSONField(default=list, blank=True)
    weaknesses = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)

    ai_summary = models.TextField(blank=True, default="")
    summary = models.TextField(blank=True, default="")

    roadmap = models.JSONField(default=dict, blank=True)

    disciplines = models.JSONField(default=list, blank=True)
    gaps = models.JSONField(default=list, blank=True)
    parser_status = models.CharField(max_length=100, blank=True, default="")
    parser_engine = models.CharField(max_length=100, blank=True, default="")
    response_mode = models.CharField(max_length=100, blank=True, default="")
    work_experience_summary = models.TextField(blank=True, default="")

    extra_data = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"StudentProfile({self.uuid})"


class IntakeSession(models.Model):
    """
    Profile-intake chat session state, replacing data/api_intake_sessions.json.
    """

    student_key = models.CharField(max_length=255, unique=True, db_index=True)
    student_id = models.CharField(max_length=255)
    step = models.IntegerField(default=0)
    completed = models.BooleanField(default=False)
    answers = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"IntakeSession({self.student_key}, step={self.step}, completed={self.completed})"


class ChatMessage(models.Model):
    """
    Persistent per-turn chat transcript for Aria, university, profile-presenter,
    and intake chat, replacing the in-process-only agent-cache dicts (now in
    agents/commons.py) that were lost on every server restart.

    student_id/university_id are plain strings (StudentProfile.uuid /
    University.uuid, not FKs) since a chat turn can happen before a
    StudentProfile row is ever saved.
    """

    class Channel(models.TextChoices):
        # The student's single persistent chat thread with their own agent.
        # Formerly "aria" -- renamed since the agent's display name is now
        # per-student and student-editable, not a fixed product name.
        AGENT = "agent", "Agent"
        UNIVERSITY = "university", "University"
        PRESENTER = "presenter", "Presenter"
        INTAKE = "intake", "Intake"

    class Sender(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    channel = models.CharField(max_length=20, choices=Channel.choices, db_index=True)
    student_id = models.CharField(max_length=255, db_index=True)
    university_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    sender = models.CharField(max_length=20, choices=Sender.choices)
    content = models.TextField()
    meta = models.JSONField(default=dict, blank=True)

    edited_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"ChatMessage({self.channel}, {self.student_id}, {self.sender})"


class ChatAttachment(models.Model):
    """
    A file (screenshot/document) a student attached to one of their own
    ChatMessage turns. Stored on disk the same way as ResumeUpload/
    LinkedInAnalysis uploads; only ever served back through an
    ownership-checked endpoint, never raw MEDIA_URL.
    """

    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="attachments")
    file_path = models.CharField(max_length=1000)
    original_filename = models.CharField(max_length=500)
    content_type = models.CharField(max_length=150, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"ChatAttachment({self.message_id}, {self.original_filename})"


class ResumeUpload(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="resume_uploads")
    file_path = models.CharField(max_length=1000)
    original_filename = models.CharField(max_length=500)
    extracted_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ResumeUpload({self.student.uuid}, {self.original_filename})"


class GitHubAnalysis(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="github_analyses")
    github_url = models.CharField(max_length=500)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"GitHubAnalysis({self.student.uuid}, {self.github_url})"


class GitHubProfileSnapshot(models.Model):
    student = models.OneToOneField(StudentProfile, on_delete=models.CASCADE, related_name="github_snapshot")
    connection = models.OneToOneField("accounts.GitHubOAuthConnection", on_delete=models.CASCADE, related_name="snapshot")
    github_user_id = models.BigIntegerField()
    identity = models.JSONField(default=dict)
    statistics = models.JSONField(default=dict)
    languages = models.JSONField(default=list)
    topics = models.JSONField(default=list)
    technologies = models.JSONField(default=list)
    domains = models.JSONField(default=list)
    organizations = models.JSONField(default=list)
    recent_activity = models.JSONField(default=list)
    academic_guidance = models.JSONField(default=dict)
    summary = models.TextField(blank=True)
    summary_kind = models.CharField(max_length=32, default="factual")
    coverage = models.JSONField(default=dict)
    warnings = models.JSONField(default=list)
    synced_at = models.DateTimeField(null=True, blank=True)


class GitHubSyncRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(GitHubProfileSnapshot, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=16, default="queued", db_index=True)
    progress = models.CharField(max_length=300, default="Waiting to collect GitHub profile")
    result = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    stage = models.CharField(max_length=20, default="collect")
    work = models.JSONField(default=dict)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    failures = models.PositiveIntegerField(default=0)
    model_calls = models.PositiveIntegerField(default=0)
    reserved_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["profile"], condition=models.Q(status__in=["queued", "running"]), name="one_active_github_sync")]


class GitHubRepository(models.Model):
    profile = models.ForeignKey(GitHubProfileSnapshot, on_delete=models.CASCADE, related_name="repositories")
    github_id = models.BigIntegerField()
    name = models.CharField(max_length=255)
    full_name = models.CharField(max_length=500)
    owner_login = models.CharField(max_length=255)
    private = models.BooleanField(default=False)
    fork = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict)
    languages = models.JSONField(default=dict)
    readme = models.TextField(blank=True)
    readme_url = models.URLField(max_length=1200, blank=True)
    readme_truncated = models.BooleanField(default=False)
    details_complete = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["full_name", "id"]
        constraints = [models.UniqueConstraint(fields=["profile", "github_id"], name="github_repo_per_profile")]
        indexes = [models.Index(fields=["profile", "active", "full_name"], name="github_repo_page")]


class GitHubSourceEvidence(models.Model):
    repository = models.ForeignKey(GitHubRepository, on_delete=models.CASCADE, related_name="source_evidence")
    sha = models.CharField(max_length=64)
    path = models.CharField(max_length=1000)
    url = models.URLField(max_length=1600)
    excerpt = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repository", "sha", "path"], name="github_source_at_commit")]


class GitHubRepositoryReport(models.Model):
    repository = models.ForeignKey(GitHubRepository, on_delete=models.CASCADE, related_name="reports")
    sha = models.CharField(max_length=64)
    analysis_version = models.PositiveIntegerField(default=2)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=100)
    data = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["repository", "sha", "analysis_version"], name="github_report_at_commit")]


class GitHubAgentCheckpoint(models.Model):
    """LangGraph checkpoints are scoped to a run and repository/commit thread."""
    run = models.ForeignKey(GitHubSyncRun, on_delete=models.CASCADE, related_name="checkpoints")
    thread = models.CharField(max_length=180)
    namespace = models.CharField(max_length=180, default="")
    checkpoint_id = models.CharField(max_length=64)
    parent_id = models.CharField(max_length=64, blank=True)
    payload_type = models.CharField(max_length=32)
    payload = models.BinaryField()
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "thread", "namespace", "checkpoint_id"], name="github_agent_checkpoint_unique")]


class GitHubAgentWrite(models.Model):
    checkpoint = models.ForeignKey(GitHubAgentCheckpoint, on_delete=models.CASCADE, related_name="writes")
    task_id = models.CharField(max_length=64)
    index = models.IntegerField()
    channel = models.CharField(max_length=100)
    payload_type = models.CharField(max_length=32)
    payload = models.BinaryField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["checkpoint", "task_id", "index"], name="github_agent_write_unique")]


class GitHubModelPool(models.Model):
    provider = models.CharField(max_length=20, primary_key=True)
    blocked_until = models.DateTimeField(null=True)
    window_started_at = models.DateTimeField(default=timezone.now)
    requests = models.PositiveIntegerField(default=0)
    reserved_tokens = models.PositiveIntegerField(default=0)


class GitHubModelSlot(models.Model):
    provider = models.CharField(max_length=20)
    number = models.PositiveIntegerField()
    token = models.UUIDField(null=True)
    expires_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "number"], name="github_model_slot_unique")]


class LinkedInAnalysis(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="linkedin_analyses")
    image_paths = models.JSONField(default=list, blank=True)
    extracted = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"LinkedInAnalysis({self.student.uuid})"


class FitAssessment(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="fit_assessments")
    university_id = models.CharField(max_length=255, db_index=True)
    assessment = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"FitAssessment({self.student.uuid}, {self.university_id})"


class UniversityInterestEvent(models.Model):
    """
    Signal that a student has actually engaged with a specific university --
    asked it a question, or had a fit assessment run for it -- rather than
    the officer's own dashboard lookups (which must never write here, since
    that would make "interest" reflect officer curiosity instead of student
    intent). Feeds UniversityProfilesListView's shortlist: a student with no
    row here for a given university_id never appears in that university's
    officer-facing profile list, regardless of fit score.
    """

    class Source(models.TextChoices):
        SEARCHED = "searched", "Searched"
        FIT_CHECK = "fit_check", "Fit Check"

    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="university_interest_events")
    university_id = models.CharField(max_length=255, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["university_id", "student"])]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "university_id", "source"],
                name="unique_university_interest_event_per_source",
            )
        ]

    def __str__(self) -> str:
        return f"UniversityInterestEvent({self.student.uuid}, {self.university_id}, {self.source})"


class RoadmapVersion(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="roadmap_versions")
    request_message = models.TextField(blank=True, default="")
    roadmap = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"RoadmapVersion({self.student.uuid})"


class AriaMemory(models.Model):
    """
    Persistent Aria long-term memory per student, replacing the previous
    memory/<student_key>_memory.json file store.
    """

    student_id = models.CharField(max_length=255, unique=True, db_index=True)
    important_points = models.JSONField(default=list, blank=True)
    universities_discussed = models.JSONField(default=list, blank=True)
    github_profiles_analyzed = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"AriaMemory({self.student_id})"


class KnowledgeIndexWork(models.Model):
    university_id = models.CharField(max_length=255, unique=True)
    revision = models.PositiveBigIntegerField(default=0)
    indexed_revision = models.PositiveBigIntegerField(default=0)
    error = models.CharField(max_length=255, blank=True, default="")


class AgentJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_key = models.CharField(max_length=300)
    idempotency_key = models.CharField(max_length=100)
    kind = models.CharField(max_length=30)
    student_id = models.CharField(max_length=255, blank=True)
    university_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, default="queued", db_index=True)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    dispatched_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner_key", "idempotency_key"], name="agent_job_idempotency"),
            models.UniqueConstraint(fields=["owner_key"], condition=models.Q(status__in=["queued", "processing"]), name="agent_one_active_turn"),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="agent_job_dispatch")]


class AgentQueueGate(models.Model):
    """One short transaction lock for queue admission; never held for model I/O."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)


class KnowledgeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        relevant = bool(set(kwargs) & {"topic", "content", "confidence", "source_type", "source_url", "group_id"})
        if not relevant:
            return super().update(**kwargs)
        from django.db import transaction
        with transaction.atomic():
            ids = list(self.order_by().values_list("university_id", flat=True).distinct())
            if set(kwargs) & {"topic", "content"}:
                kwargs.update(embedding=None, embedding_hash="", embedding_model="")
            result = super().update(**kwargs)
            for uid in ids:
                work, _ = KnowledgeIndexWork.objects.get_or_create(university_id=uid)
                KnowledgeIndexWork.objects.filter(pk=work.pk).update(revision=models.F("revision") + 1)
            return result

    def bulk_create(self, objs, **kwargs):
        from django.db import transaction
        objs = list(objs)
        with transaction.atomic():
            result = super().bulk_create(objs, **kwargs)
            for uid in set(row.university_id for row in objs):
                work, _ = KnowledgeIndexWork.objects.get_or_create(university_id=uid)
                KnowledgeIndexWork.objects.filter(pk=work.pk).update(revision=models.F("revision") + 1)
            return result


class UniversityKnowledgeEntry(models.Model):
    """
    Persistent knowledge-base fact for a university agent, replacing the
    previous in-memory-only UniversityKnowledgeBase that was rebuilt from
    seed data (and lost any scraped/learned facts) on every server restart.
    """

    university_id = models.CharField(max_length=255, db_index=True)
    objects = KnowledgeQuerySet.as_manager()
  
    group = models.ForeignKey(
        "universities.KnowledgeGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="knowledge_entries",
    )
    topic = models.CharField(max_length=500)
    content = models.TextField()
    source_type = models.CharField(max_length=50, default="unknown")
    source_url = models.CharField(max_length=1000, blank=True, null=True)
    confidence = models.FloatField(default=1.0)
    times_used = models.IntegerField(default=0)
    # pgvector on PostgreSQL; nullable so SQLite and pre-indexed imports work.
    embedding = VectorField(dimensions=384, null=True, blank=True)
    embedding_hash = models.CharField(max_length=64, blank=True, default="")
    embedding_model = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-confidence", "-times_used"]

    def __str__(self) -> str:
        return f"UniversityKnowledgeEntry({self.university_id}, {self.topic[:40]})"


class PendingQuery(models.Model):
    """
    Escalated student question awaiting human/university verification,
    replacing the previous data/pending_queries.json file store.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RESOLVED = "resolved", "Resolved"
        IGNORED = "ignored", "Ignored"

    class Priority(models.TextChoices):
        NORMAL = "normal", "Normal"
        URGENT = "urgent", "Urgent"

    university_id = models.CharField(max_length=255, db_index=True)
    university_name = models.CharField(max_length=255, blank=True, default="")
    agent_name = models.CharField(max_length=255, blank=True, default="")
    student_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    student_name = models.CharField(max_length=255, blank=True, default="")
    program = models.CharField(max_length=255, blank=True, default="")

    group = models.ForeignKey(
        "universities.KnowledgeGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="escalations",
    )
    routed_to_name = models.CharField(max_length=255, blank=True, default="")
    routed_to_email = models.CharField(max_length=255, blank=True, default="")
    question = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.NORMAL)
    urgency_reason = models.TextField(blank=True, default="")
    escalation_chain = models.JSONField(default=list, blank=True)
    answer = models.TextField(blank=True, default="")
    answered_by = models.CharField(max_length=255, blank=True, default="")
    answered_at = models.DateTimeField(null=True, blank=True)
    
    confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def display_status(self) -> str:
        if self.status == self.Status.RESOLVED:
            return "answered"
        if self.status == self.Status.IGNORED:
            return "ignored"
        return "urgent" if self.priority == self.Priority.URGENT else "pending"

    def __str__(self) -> str:
        return f"PendingQuery(#{self.id}, {self.university_id}, {self.status})"


class VerifiedAnswer(models.Model):
    """
    Durable human-verified answer for a university agent, replacing the
    previous knowledge/human_verified_answers.json file store.
    """

    query = models.ForeignKey(
        PendingQuery, on_delete=models.SET_NULL, null=True, blank=True, related_name="verified_answers"
    )
    university_id = models.CharField(max_length=255, db_index=True)
    question = models.TextField()
    answer = models.TextField()
    answered_by = models.CharField(max_length=255, blank=True, default="")
    source = models.CharField(max_length=100, blank=True, default="")
    confidence = models.FloatField(default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VerifiedAnswer({self.university_id}, {self.question[:40]})"


class UniversityQuestionLog(models.Model):
    """
    Officer-facing question log for the profile presenter chat, replacing
    the previous knowledge/university_questions.json file store.
    """

    university_id = models.CharField(max_length=255, db_index=True)
    student_name = models.CharField(max_length=255, blank=True, default="")
    question = models.TextField()
    topic = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"UniversityQuestionLog({self.university_id}, {self.topic})"


class PresenterAuditLog(models.Model):
    """
    Error/audit trail for ProfilePresenterAgent, replacing the previous
    knowledge/profile_presenter_audit.json file store.
    """

    university_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    event = models.CharField(max_length=100)
    message = models.TextField(blank=True, default="")
    details = models.TextField(blank=True, default="")
    profile_name = models.CharField(max_length=255, blank=True, default="")
    question = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PresenterAuditLog({self.event}, {self.created_at})"


class AgentIdentity(models.Model):
    """
    The birth record for one agent -- student or university -- created once,
    lazily, the first time that agent is actually used (see
    agents.identity_registry.get_or_create_identity(), called from
    agents.agent_identity.ensure_agent_name() for students and
    agents.university_agent.UniversityAgent.__init__() for universities).
    Never mutated except agent_name drifting to match a rename; agent_id and
    created_at are permanent -- this is the durable, sealed half of the
    "sealed identity, tamper-evident history" claim, with
    AgentConversationLog as the tamper-evident history half.
    """

    class OwnerType(models.TextChoices):
        STUDENT = "student", "Student"
        UNIVERSITY = "university", "University"

    agent_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_type = models.CharField(max_length=20, choices=OwnerType.choices, db_index=True)
    # StudentProfile.uuid or University.uuid -- deliberately a plain
    # string FK-by-convention (not a real FK) so an identity row, once
    # created, is never cascade-deleted by profile/university churn.
    owner_id = models.CharField(max_length=255, db_index=True)
    agent_name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["owner_type", "owner_id"], name="uq_agent_identity_owner"),
        ]

    def __str__(self) -> str:
        return f"AgentIdentity({self.owner_type}, {self.owner_id})"


class AgentConversationLog(models.Model):
    """
    One first-class row per agent-to-agent exchange -- currently the
    student-agent -> university-agent path (pure_multi_agent.tools.
    university_tools.ask_university / compare_all_universities). Rows are
    append-only: who asked whom, what, answered from which knowledge
    source, when. This is what agents.agent_identity / AgentIdentity was
    missing on its own -- an identity record proves an agent exists, this
    proves what it actually did.
    """

    asker = models.ForeignKey(AgentIdentity, on_delete=models.CASCADE, related_name="asked_conversations")
    responder = models.ForeignKey(AgentIdentity, on_delete=models.CASCADE, related_name="answered_conversations")
    question = models.TextField()
    answer = models.TextField()
    # e.g. "human_verified", "conversation", "kb_search", "pending" --
    # mirrors the source/trust.source_type values agents.university_agent
    # already returns from answer().
    knowledge_source = models.CharField(max_length=50, blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"AgentConversationLog({self.asker_id} -> {self.responder_id}, {self.created_at})"


class AgentAuditLog(models.Model):
    """
    System-level observability record capturing the internal "thought process" and tool
    usage of agents (like Aria and University agents), including reasoning, tool calls,
    and agent-to-agent communication triggers. Automatically purged after 90 days.
    """
    run_id = models.CharField(max_length=255, db_index=True)
    student_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    actor_agent = models.CharField(max_length=255)
    action_type = models.CharField(max_length=50)  # e.g., 'REASONING', 'TOOL_CALL', 'AGENT_COMMUNICATION'
    target = models.CharField(max_length=255, blank=True, default="")
    inputs = models.JSONField(default=dict, blank=True)
    outputs = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["student_id", "-timestamp"]),
        ]

    def __str__(self) -> str:
        return f"AgentAuditLog({self.actor_agent}, {self.action_type}, {self.timestamp})"
