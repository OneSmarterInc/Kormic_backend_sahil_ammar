from django.db import migrations, models


def backfill_roster_geography(apps, schema_editor):
    ListedStudent = apps.get_model("institutes_list", "ListedStudent")
    for row in ListedStudent.objects.select_related("source_list__institute").all().iterator():
        region = (row.region or row.state or "").strip()
        country = (row.country or row.source_list.institute.country or "").strip().upper()
        ListedStudent.objects.filter(pk=row.pk).update(region=region, country=country)


class Migration(migrations.Migration):
    dependencies = [
        ("institutes", "0002_institute_country"),
        ("institutes_list", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="listedstudent",
            name="country",
            field=models.CharField(blank=True, default="", max_length=2),
        ),
        migrations.AddField(
            model_name="listedstudent",
            name="region",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(backfill_roster_geography, migrations.RunPython.noop),
    ]
