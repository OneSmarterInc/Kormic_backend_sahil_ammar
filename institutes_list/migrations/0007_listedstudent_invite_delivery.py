from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("institutes_list", "0003_rename_universitystudentlist_institutestudentlist"),
    ]

    operations = [
        migrations.AddField(
            model_name="listedstudent",
            name="invite_delivery_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("queued", "Queued"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="listedstudent",
            name="invite_delivery_error",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="listedstudent",
            name="invite_delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
