from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("django_api", "0013_document_evidence_status_length"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="fitassessment",
            index=models.Index(
                fields=["student", "university_id", "-created_at", "-id"],
                name="fit_assessment_latest_idx",
            ),
        ),
    ]
