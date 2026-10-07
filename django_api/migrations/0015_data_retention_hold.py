from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("django_api", "0014_fit_assessment_latest_index")]

    operations = [
        migrations.AddField(
            model_name="agentjob", name="retention_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="agentjob", name="retention_compacted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="agentjob",
            index=models.Index(fields=["status", "completed_at"], name="agent_job_retention"),
        ),
        migrations.AddIndex(
            model_name="githubsyncrun",
            index=models.Index(fields=["status", "updated_at"], name="github_run_retention"),
        ),
        migrations.CreateModel(
            name="DataRetentionHold",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("subject_key", models.CharField(max_length=300)),
                ("reason", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="dataretentionhold",
            constraint=models.UniqueConstraint(
                fields=("subject_key",), condition=Q(released_at__isnull=True),
                name="one_active_retention_hold_per_subject",
            ),
        ),
    ]
