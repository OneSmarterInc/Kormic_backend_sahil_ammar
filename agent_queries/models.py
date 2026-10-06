import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q


class AgentConversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    student = models.ForeignKey("django_api.StudentProfile", on_delete=models.CASCADE)
    university = models.ForeignKey("universities.University", on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "university"], name="unique_agent_conversation_pair")]
        ordering = ["-updated_at", "id"]


class AgentQuery(models.Model):
    class Direction(models.TextChoices):
        STUDENT = "student_to_university", "Student agent to university agent"
        UNIVERSITY = "university_to_student", "University agent to student agent"
    conversation = models.ForeignKey(AgentConversation, on_delete=models.CASCADE, related_name="queries")
    direction = models.CharField(max_length=30, choices=Direction.choices)
    question = models.TextField()
    group = models.ForeignKey("universities.KnowledgeGroup", null=True, blank=True, on_delete=models.SET_NULL)
    question_hash = models.CharField(max_length=64)
    raised_by_agent = models.CharField(max_length=255)
    recipient_agent = models.CharField(max_length=255)
    status = models.CharField(max_length=15, choices=[("unanswered", "Unanswered"), ("answered", "Answered")], default="unanswered", db_index=True)
    answer = models.TextField(blank=True)
    answer_scope = models.CharField(max_length=20, default="private")
    answered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    knowledge_entry = models.ForeignKey("django_api.UniversityKnowledgeEntry", null=True, blank=True, on_delete=models.SET_NULL)
    answered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["conversation", "direction", "question_hash"], condition=Q(status="unanswered"), name="unique_open_agent_query")]
        indexes = [models.Index(fields=["conversation", "direction", "status", "-created_at"], name="agent_query_inbox_idx")]


class AgentConversationMessage(models.Model):
    conversation = models.ForeignKey(AgentConversation, on_delete=models.CASCADE, related_name="messages")
    query = models.ForeignKey(AgentQuery, null=True, blank=True, on_delete=models.SET_NULL)
    actor = models.CharField(max_length=30)
    actor_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=30, default="message")
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["conversation", "created_at", "id"], name="agent_message_thread_idx")]


class AgentActivity(models.Model):
    owner_key = models.CharField(max_length=100, unique=True)
    run_id = models.UUIDField()
    status = models.CharField(max_length=20, default='working')
    label = models.CharField(max_length=180, default='Thinking…')
    updated_at = models.DateTimeField(auto_now=True)


class AgentCapacitySlot(models.Model):
    key = models.CharField(max_length=180)
    number = models.PositiveIntegerField()
    token = models.UUIDField(null=True)
    expires_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['key', 'number'], name='unique_agent_capacity_slot')]


class AgentRateWindow(models.Model):
    key = models.CharField(max_length=180, unique=True)
    started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)
