from django.db import models

from accounts.models import Account


class PushToken(models.Model):
    """
    An Expo push token for one installed app instance. Tokens are keyed
    globally (not per-account) since re-registering an existing token just
    reassigns it -- this correctly handles a shared/reset device where a
    different student later logs in and registers the same token.
    """

    class Platform(models.TextChoices):
        IOS = "ios", "iOS"
        ANDROID = "android", "Android"
        UNKNOWN = "unknown", "Unknown"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="push_tokens")
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=20, choices=Platform.choices, default=Platform.UNKNOWN)
    is_active = models.BooleanField(default=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"PushToken(account={self.account_id}, platform={self.platform}, active={self.is_active})"


class NotificationLog(models.Model):
    """
    Durable record of every push notification queued for delivery, so
    delivery can be audited/retried/tested independently of whatever
    triggered it. Created synchronously by notifications.services, delivered
    asynchronously by notifications.tasks.
    """

    class EventType(models.TextChoices):
        AGENT_REPLY = "agent_reply", "Agent Reply"
        PENDING_QUERY_RESOLVED = "pending_query_resolved", "Pending Query Resolved"
        AGENT_INITIATED = "agent_initiated", "Agent Initiated"
        PROACTIVE_CHECKIN = "proactive_checkin", "Proactive Check-in"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED_NO_TOKEN = "skipped_no_token", "Skipped (no device token)"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="notification_logs")
    event_type = models.CharField(max_length=30, choices=EventType.choices, default=EventType.OTHER)
    title = models.CharField(max_length=255)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"NotificationLog(account={self.account_id}, {self.event_type}, {self.status})"
