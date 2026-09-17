from django.contrib import admin

from django_api.models import (
    AgentConversationLog,
    AgentIdentity,
    ChatAttachment,
    ChatMessage,
    FitAssessment,
    GitHubAnalysis,
    IntakeSession,
    LinkedInAnalysis,
    ResumeUpload,
    RoadmapVersion,
    StudentProfile,
)


@admin.register(StudentProfile)
class StudentProfileAdmin(admin.ModelAdmin):
    list_display = ("uuid", "name", "email", "institution", "major", "updated_at")
    search_fields = ("name", "email", "institution")


@admin.register(IntakeSession)
class IntakeSessionAdmin(admin.ModelAdmin):
    list_display = ("student_key", "student_id", "step", "completed", "updated_at")
    search_fields = ("student_key", "student_id")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("student_id", "channel", "university_id", "sender", "edited_at", "created_at")
    search_fields = ("student_id", "university_id", "content")
    list_filter = ("channel", "sender")


@admin.register(ChatAttachment)
class ChatAttachmentAdmin(admin.ModelAdmin):
    list_display = ("message", "original_filename", "content_type", "size_bytes", "created_at")
    search_fields = ("original_filename", "message__student_id")
    list_filter = ("content_type",)


@admin.register(ResumeUpload)
class ResumeUploadAdmin(admin.ModelAdmin):
    list_display = ("student", "original_filename", "created_at")
    search_fields = ("original_filename",)


@admin.register(GitHubAnalysis)
class GitHubAnalysisAdmin(admin.ModelAdmin):
    list_display = ("student", "github_url", "created_at")
    search_fields = ("github_url",)


@admin.register(LinkedInAnalysis)
class LinkedInAnalysisAdmin(admin.ModelAdmin):
    list_display = ("student", "created_at")
    search_fields = ()


@admin.register(FitAssessment)
class FitAssessmentAdmin(admin.ModelAdmin):
    list_display = ("student", "university_id", "created_at")
    search_fields = ("university_id",)


@admin.register(RoadmapVersion)
class RoadmapVersionAdmin(admin.ModelAdmin):
    list_display = ("student", "created_at")
    search_fields = ()


@admin.register(AgentIdentity)
class AgentIdentityAdmin(admin.ModelAdmin):
    list_display = ("agent_id", "owner_type", "owner_id", "agent_name", "created_at")
    search_fields = ("owner_id", "agent_name")
    list_filter = ("owner_type",)


@admin.register(AgentConversationLog)
class AgentConversationLogAdmin(admin.ModelAdmin):
    list_display = ("asker", "responder", "knowledge_source", "confidence", "created_at")
    search_fields = ("asker__owner_id", "responder__owner_id", "question")
    list_filter = ("knowledge_source",)
