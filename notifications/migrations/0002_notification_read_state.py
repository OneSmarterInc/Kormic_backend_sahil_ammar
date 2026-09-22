from django.db import migrations, models
from django.db.models import F


def mark_existing_notifications_read(apps, schema_editor):
    NotificationLog = apps.get_model("notifications", "NotificationLog")
    NotificationLog.objects.filter(read_at__isnull=True).update(read_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationlog",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("agent_reply", "Agent Reply"),
                    ("pending_query_resolved", "Pending Query Resolved"),
                    ("agent_initiated", "Agent Initiated"),
                    ("proactive_checkin", "Proactive Check-in"),
                    ("university_query", "University Query"),
                    ("student_claimed", "Student Claimed"),
                    ("job_completed", "Job Completed"),
                    ("job_failed", "Job Failed"),
                    ("system_alert", "System Alert"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="read_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(mark_existing_notifications_read, migrations.RunPython.noop),
    ]
