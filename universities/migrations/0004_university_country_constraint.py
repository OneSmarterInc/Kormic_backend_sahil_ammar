from django.db import migrations, models


def validate_existing_universities(apps, schema_editor):
    University = apps.get_model("universities", "University")
    invalid = list(University.objects.exclude(country="US").values_list("id", flat=True)[:50])
    if invalid:
        raise RuntimeError(
            "Cannot enforce university_country_us: existing non-US university rows require manual review. "
            f"Example IDs: {invalid}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("universities", "0003_require_university_country"),
    ]

    operations = [
        migrations.RunPython(validate_existing_universities, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="university",
            constraint=models.CheckConstraint(
                condition=models.Q(country="US"),
                name="university_country_us",
            ),
        ),
    ]
