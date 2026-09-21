from django.db import migrations, models


def normalize_and_audit_university_countries(apps, schema_editor):
    University = apps.get_model("universities", "University")
    flagged = []
    for university in University.objects.all().iterator():
        original = (university.country or "").strip()
        country = original.upper()
        if country != "US":
            flagged.append((university.pk, university.name, original))
            country = "US"
        if country != university.country:
            University.objects.filter(pk=university.pk).update(country=country)

    if flagged:
        print(
            "WARNING: University country was not US for these rows; "
            "backfilled to US and requires business review:",
            flagged,
        )


class Migration(migrations.Migration):
    dependencies = [("universities", "0002_university_country")]

    operations = [
        migrations.RunPython(
            normalize_and_audit_university_countries,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="university",
            name="country",
            field=models.CharField(max_length=2),
        ),
    ]
