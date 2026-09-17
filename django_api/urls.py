from django.urls import path

from django_api import views

urlpatterns = [
    path("", views.api_home, name="api-home"),

    # APIs 1-5: Profile Management
    path("profile/", views.ProfileCreateUpdateAPIView.as_view(), name="profile-create-update"),
    path("profile/image/", views.ProfileImageUploadAPIView.as_view(), name="profile-image-upload"),
    path("profile/resume/", views.ResumeUploadAPIView.as_view(), name="profile-resume"),
    path("profile/resume/<int:resume_id>/", views.ResumeDetailAPIView.as_view(), name="profile-resume-detail"),
    path("profile/github/", views.GitHubAnalyzeAPIView.as_view(), name="profile-github"),
    path("profile/linkedin/", views.LinkedInAnalyzeAPIView.as_view(), name="profile-linkedin"),
    path(
        "profile/linkedin/<int:analysis_id>/images/<int:index>/",
        views.LinkedInImageDetailAPIView.as_view(),
        name="profile-linkedin-image-detail",
    ),
    path("profile/agent-name/", views.AgentNameAPIView.as_view(), name="agent-name"),
    path("profile/<str:student_id>/", views.ProfileDetailAPIView.as_view(), name="profile-detail"),
    path("profile/<str:student_id>/image/", views.ProfileImageDetailAPIView.as_view(), name="profile-image-detail"),

    # Persistent GET APIs for profile sub-resource history
    path("profile/<str:student_id>/resumes/", views.ResumeHistoryView.as_view(), name="profile-resume-history"),
    path("profile/<str:student_id>/github-history/", views.GitHubHistoryView.as_view(), name="profile-github-history"),
    path("profile/<str:student_id>/linkedin-history/", views.LinkedInHistoryView.as_view(), name="profile-linkedin-history"),

    # API 6-7: Chat -- the student's personal agent is the only student-facing
    # chat endpoint. Verification/university-fit/university-chat happen only
    # as background orchestrator calls from inside agent_chat (see agents/commons.py).
    path("chat/intake/", views.profile_intake_chat, name="profile-intake-chat"),
    path("chat/agent/", views.agent_chat, name="agent-chat"),
    path("chat/agent/history/", views.agent_chat_history, name="agent-chat-history"),
    # "New chat" and "clear chat" are the same action here -- there's no
    # multi-thread concept per student, so starting fresh always means
    # wiping the one conversation there is. Kept as a single canonical
    # endpoint rather than two aliases that could quietly drift apart.
    path("chat/agent/new/", views.agent_chat_new, name="agent-chat-new"),
    path("chat/agent/<int:message_id>/edit/", views.agent_chat_edit, name="agent-chat-edit"),
    path(
        "chat/agent/attachments/<int:attachment_id>/",
        views.ChatAttachmentDetailAPIView.as_view(),
        name="chat-attachment-detail",
    ),

    # Fit assessment history is read-only now -- generation happens only via
    # agent_chat (agents.commons.generate_fit_assessment), never a direct POST.
    path("assessments/<str:student_id>/", views.AssessmentHistoryView.as_view(), name="assessment-history"),
    path(
        "assessments/<str:university_id>/<str:student_id>/",
        views.AssessmentDetailView.as_view(),
        name="assessment-detail",
    ),

    # APIs 10-13: Roadmap, Queries, Export
    path("roadmap/<str:student_id>/", views.RoadmapView.as_view(), name="roadmap"),
    path("roadmap/<str:student_id>/history/", views.RoadmapHistoryView.as_view(), name="roadmap-history"),
    path("queries/pending/", views.PendingQueriesView.as_view(), name="pending-queries"),
    path("queries/answer/", views.AnswerPendingQueryView.as_view(), name="answer-pending-query"),
    path("queries/<int:query_id>/edit/", views.EditPendingQueryView.as_view(), name="edit-pending-query"),
    path("queries/<int:query_id>/ignore/", views.IgnorePendingQueryView.as_view(), name="ignore-pending-query"),
    path("queries/<int:query_id>/", views.DeletePendingQueryView.as_view(), name="delete-pending-query"),
    path("exports/pdf/<str:student_id>/", views.ExportProfilePDFView.as_view(), name="export-profile-pdf"),

    # University Dashboard APIs
    path("university/<str:university_id>/profiles/", views.UniversityProfilesListView.as_view(), name="university-profiles"),
    path(
        "university/<str:university_id>/profile/<str:student_id>/",
        views.UniversityProfileDetailAPIView.as_view(),
        name="university-profile-detail",
    ),
    path(
        "university/<str:university_id>/profile/<str:student_id>/chat/",
        views.university_profile_presenter_chat,
        name="university-profile-chat",
    ),
    path(
        "university/<str:university_id>/profile/<str:student_id>/chat/history/",
        views.university_profile_presenter_chat_history,
        name="university-profile-chat-history",
    ),
    path("university/<str:university_id>/chat/", views.university_agent_chat, name="university-agent-chat"),
    path(
        "university/<str:university_id>/chat/history/",
        views.university_agent_chat_history,
        name="university-agent-chat-history",
    ),
    path("university/<str:university_id>/questions/", views.UniversityQuestionsView.as_view(), name="university-questions"),
    path("university/<str:university_id>/queries/", views.UniversityQueriesView.as_view(), name="university-queries"),
    path(
        "university/<str:university_id>/queries/active/",
        views.UniversityActiveQueriesView.as_view(),
        name="university-queries-active",
    ),
    path(
        "university/<str:university_id>/queries/archive/",
        views.UniversityArchiveQueriesView.as_view(),
        name="university-queries-archive",
    ),
    path(
        "university/<str:university_id>/knowledge/verified/",
        views.VerifiedKnowledgeView.as_view(),
        name="university-verified-knowledge",
    ),
]
