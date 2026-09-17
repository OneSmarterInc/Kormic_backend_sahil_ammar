from django.contrib import admin

from project_superuser.models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor_email", "target_email", "target_role", "created_at")
    search_fields = ("actor_email", "target_email")
    list_filter = ("action", "target_role")
