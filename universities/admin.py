from django.contrib import admin

from universities.models import KnowledgeGroup, University


@admin.register(University)
class UniversityAdmin(admin.ModelAdmin):
    list_display = ("id", "uuid", "name", "agent_name", "location", "created_at", "updated_at")
    search_fields = ("name", "agent_name", "location")


@admin.register(KnowledgeGroup)
class KnowledgeGroupAdmin(admin.ModelAdmin):
    list_display = ("university", "slug", "escalation_contact_name", "escalation_contact_email", "updated_at")
    search_fields = ("university__name", "escalation_contact_email")
    list_filter = ("slug",)
