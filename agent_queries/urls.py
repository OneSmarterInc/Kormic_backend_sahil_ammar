from django.urls import path
from .views import QueryListView, QueryAnswerView, ConversationListView, ConversationDetailView
urlpatterns = [path("", QueryListView.as_view()), path("<int:query_id>/answer/", QueryAnswerView.as_view()),
    path("conversations/", ConversationListView.as_view()), path("conversations/<uuid:conversation_id>/", ConversationDetailView.as_view())]
