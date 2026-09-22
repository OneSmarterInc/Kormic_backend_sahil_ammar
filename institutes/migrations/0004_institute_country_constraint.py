from django.db import migrations, models


def validate_existing_institutes(apps, schema_editor):
    Institute = apps.get_model("institutes", "Institute")
    invalid = list(Institute.objects.filter(country="US").values_list("id", flat=True)[:50])
    if invalid:
        raise RuntimeError(
            "Cannot enforce institute_country_not_us: existing US institute rows require manual review. "
            f"Example IDs: {invalid}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("institutes", "0003_require_institute_country"),
    ]

    operations = [
        migrations.RunPython(validate_existing_institutes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="institute",
            constraint=models.CheckConstraint(
                condition=~models.Q(country="US"),
                name="institute_country_not_us",
            ),
        ),
    ]
