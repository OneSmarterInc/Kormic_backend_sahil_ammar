from django.urls import path

from . import claim_views, views

urlpatterns = [
    path("institute-lists/upload/", views.upload_list, name="institute-list-upload"),
    path("institute-lists/lists/", views.list_lists, name="institute-list-lists"),
    path("institute-lists/lists/<int:list_id>/file/", views.download_source_file, name="institute-list-source-file"),
    path("institute-lists/lists/<int:list_id>/students/", views.list_students, name="institute-list-students"),
    path("institute-lists/lists/<int:list_id>/send-invites/", views.send_invites, name="institute-list-send-invites"),
    path(
        "institute-lists/lists/<int:list_id>/students/<int:student_id>/send-invite/",
        views.send_invite,
        name="institute-list-send-invite",
    ),
    path("claim/start/", claim_views.start_claim, name="claim-start"),
    path("claim/verify/", views.verify_claim, name="claim-verify"),
    path("claim/confirm/", views.confirm_claim, name="claim-confirm"),
]
