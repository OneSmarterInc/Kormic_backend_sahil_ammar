from django.contrib import admin

from notifications.models import NotificationLog, PushToken


@admin.register(PushToken)
class PushTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "platform", "is_active", "updated_at")
    list_filter = ("platform", "is_active")
    search_fields = ("token", "account__user__email")


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "event_type", "status", "created_at")
    list_filter = ("event_type", "status")
    search_fields = ("account__user__email", "title", "body")
