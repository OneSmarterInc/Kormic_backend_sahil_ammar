from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("universities", "0004_university_country_constraint"),
    ]

    operations = [
        migrations.AlterField(
            model_name="university",
            name="country",
            field=models.CharField(default="US", max_length=2),
        ),
    ]
