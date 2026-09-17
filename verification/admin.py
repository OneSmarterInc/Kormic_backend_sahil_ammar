from django.contrib import admin

from verification.models import VerificationCheck, VerificationItem


class VerificationItemInline(admin.TabularInline):
    model = VerificationItem
    extra = 0
    readonly_fields = ("key", "dimension", "sources", "severity", "expected_value", "found_value", "message", "created_at")
    fields = ("key", "severity", "expected_value", "found_value", "is_resolved", "resolution", "student_note")


@admin.register(VerificationCheck)
class VerificationCheckAdmin(admin.ModelAdmin):
    list_display = ("student", "status", "engine", "missing_sources", "last_analyzed_at", "updated_at")
    search_fields = ()
    list_filter = ("status", "engine")
    inlines = [VerificationItemInline]


@admin.register(VerificationItem)
class VerificationItemAdmin(admin.ModelAdmin):
    list_display = ("verification_check", "key", "severity", "is_resolved", "resolution", "created_at")
    search_fields = ("key", "message")
    list_filter = ("dimension", "severity", "is_resolved", "resolution")
