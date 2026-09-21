from django.db import migrations, models


def backfill_university_country(apps, schema_editor):
    University = apps.get_model("universities", "University")
    University.objects.filter(country="").update(country="US")


class Migration(migrations.Migration):
    dependencies = [("universities", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="university",
            name="country",
            field=models.CharField(default="US", max_length=2),
        ),
        migrations.RunPython(backfill_university_country, migrations.RunPython.noop),
    ]
