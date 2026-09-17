from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models


class ActivityLog(models.Model):
    """
    Audit trail covering both admin-initiated actions against another
    user's account (TOTP reset, forced password reset, session
    revocation) and self-service auth activity (register, login,
    logout) -- so a superadmin can see the full account history in one
    place, not just what other superusers have done.

    Written from two apps: project_superuser (its own admin actions,
    via log_activity()) and accounts (register/login/logout, via a
    lazy import of the same helper -- accounts stays the foundational
    app other apps depend on, so it reaches into project_superuser at
    call time rather than project_superuser reaching down into it).

    actor/target are kept as nullable FKs (SET_NULL) plus a
    denormalized email/role snapshot, so entries stay readable even
    after the account in question is later deleted, and so a failed
    login against an email with no matching account can still be
    logged (target_user null, target_email/target_role blank).
    """

    class Action(models.TextChoices):
        # Admin-initiated: actor is the acting superuser, target is
        # whoever they acted on.
        TOTP_REMOVED = "totp_removed", "TOTP removed"
        PASSWORD_RESET = "password_reset", "Password reset"
        SESSIONS_REVOKED = "sessions_revoked", "Sessions revoked"
        # Self-service auth activity: actor is the account itself
        # (null for a failed login, since nothing is proven yet).
        REGISTERED = "registered", "Registered"
        LOGIN_SUCCEEDED = "login_succeeded", "Login succeeded"
        LOGIN_FAILED = "login_failed", "Login failed"
        LOGGED_OUT = "logged_out", "Logged out"
        PASSWORD_RESET_REQUESTED = "password_reset_requested", "Password reset requested"

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="activity_log_actions"
    )
    actor_email = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=32, choices=Action.choices)
    target_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="activity_log_entries"
    )
    target_email = models.CharField(max_length=255, blank=True, default="")
    # Snapshot of Account.role/student_id/university_id at write time, so
    # the dashboard can filter/group auth activity (e.g. "show me this
    # university's login history") without joining back to Account --
    # blank if the target had no Account (e.g. unknown-email login attempt).
    # Only one of target_student_id/target_university_id is ever set,
    # matching Account's own student_id/university_id split.
    target_role = models.CharField(max_length=20, blank=True, default="")
    target_student_id = models.CharField(max_length=255, blank=True, default="")
    target_university_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ActivityLog({self.action}, actor={self.actor_email or '-'}, target={self.target_email or '-'})"
