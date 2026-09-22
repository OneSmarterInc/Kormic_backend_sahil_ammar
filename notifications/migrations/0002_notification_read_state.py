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
        migrations.AddField(
            model_name="notificationlog",
            name="read_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(mark_existing_notifications_read, migrations.RunPython.noop),
    ]
