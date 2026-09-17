from django.urls import path

from universities import views

urlpatterns = [
    path("profile/", views.UniversityProfileAPIView.as_view(), name="university-admin-profile"),
    path(
        "profile/completion/",
        views.UniversityProfileCompletionAPIView.as_view(),
        name="university-admin-profile-completion",
    ),
    path("agent-name/", views.UniversityAgentNameAPIView.as_view(), name="university-admin-agent-name"),
    path("scrape-urls/", views.ScrapeUrlsAPIView.as_view(), name="university-admin-scrape-urls"),
    path("scrape-urls/scrape-now/", views.ScrapeNowAPIView.as_view(), name="university-admin-scrape-now"),
    path(
        "scrape-urls/scrape-now/<int:job_id>/",
        views.ScrapeNowJobDetailAPIView.as_view(),
        name="university-admin-scrape-now-detail",
    ),
    path(
        "scrape-urls/auto-discover/",
        views.AutoDiscoverUrlsAPIView.as_view(),
        name="university-admin-auto-discover",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/",
        views.AutoDiscoverJobDetailAPIView.as_view(),
        name="university-admin-auto-discover-detail",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/stop/",
        views.AutoDiscoverJobStopAPIView.as_view(),
        name="university-admin-auto-discover-stop",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/results/",
        views.AutoDiscoverResultsAPIView.as_view(),
        name="university-admin-auto-discover-results",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/apply/",
        views.AutoDiscoverApplyAPIView.as_view(),
        name="university-admin-auto-discover-apply",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/clusters/",
        views.AutoDiscoverClusterMapAPIView.as_view(),
        name="university-admin-auto-discover-clusters",
    ),
    path(
        "scrape-urls/auto-discover/<int:job_id>/clusters/<str:category>/approve/",
        views.AutoDiscoverClusterApproveAPIView.as_view(),
        name="university-admin-auto-discover-cluster-approve",
    ),
    path("knowledge/", views.KnowledgeFactListCreateAPIView.as_view(), name="university-admin-knowledge"),
    path(
        "knowledge/sections/",
        views.KnowledgeSectionsAPIView.as_view(),
        name="university-admin-knowledge-sections",
    ),
    path(
        "knowledge/urls/",
        views.KnowledgeSourceUrlsAPIView.as_view(),
        name="university-admin-knowledge-urls",
    ),
    path(
        "knowledge/<int:fact_id>/",
        views.KnowledgeFactDetailAPIView.as_view(),
        name="university-admin-knowledge-detail",
    ),
    path(
        "knowledge-groups/",
        views.KnowledgeGroupListAPIView.as_view(),
        name="university-admin-knowledge-groups",
    ),
    path(
        "knowledge-groups/<str:slug>/",
        views.KnowledgeGroupDetailAPIView.as_view(),
        name="university-admin-knowledge-group-detail",
    ),
    path(
        "knowledge-groups/<str:slug>/knowledge_list/",
        views.KnowledgeGroupFactsAPIView.as_view(),
        name="university-admin-knowledge-group-knowledge-list",
    ),
    path(
        "knowledge-groups/<str:slug>/escalations_list/",
        views.KnowledgeGroupEscalationsAPIView.as_view(),
        name="university-admin-knowledge-group-escalations-list",
    ),
    path(
        "knowledge-groups/<str:slug>/escalations_list/notify/",
        views.KnowledgeGroupEscalationNotifyAPIView.as_view(),
        name="university-admin-knowledge-group-escalations-notify",
    ),
]
