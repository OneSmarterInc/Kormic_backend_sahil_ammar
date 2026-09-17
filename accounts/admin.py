from django.contrib import admin

from accounts.models import Account, TOTPBackupCode, TOTPDevice


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "student_uuid", "university_uuid", "institute_uuid", "created_at")
    search_fields = ("user__email",)
    list_filter = ("role",)
    raw_id_fields = ("user", "student_profile", "university", "institute")


@admin.register(TOTPDevice)
class TOTPDeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "confirmed_at", "last_used_at", "created_at")
    search_fields = ("user__email",)


@admin.register(TOTPBackupCode)
class TOTPBackupCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "used_at", "created_at")
    search_fields = ("user__email",)
