from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("universities", "0003_require_university_country"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="university",
            constraint=models.CheckConstraint(
                condition=models.Q(country="US"),
                name="university_country_us",
            ),
        ),
    ]
