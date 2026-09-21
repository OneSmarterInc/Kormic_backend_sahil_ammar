from django.db import migrations, models


def backfill_institute_country(apps, schema_editor):
    Institute = apps.get_model("institutes", "Institute")
    unresolved = []
    for institute in Institute.objects.all().iterator():
        country = (getattr(institute, "country", "") or "").strip().upper()
        if not country:
            country = "IN"
        if len(country) != 2 or not country.isalpha() or country == "US":
            unresolved.append((institute.pk, institute.name, country))
            country = "IN"
        Institute.objects.filter(pk=institute.pk).update(country=country)
    if unresolved:
        print("WARNING: institute countries required fallback to IN:", unresolved)


class Migration(migrations.Migration):
    dependencies = [("institutes", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="institute",
            name="country",
            field=models.CharField(default="IN", max_length=2),
        ),
        migrations.RunPython(backfill_institute_country, migrations.RunPython.noop),
    ]
