from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("institutes", "0003_require_institute_country"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="institute",
            constraint=models.CheckConstraint(
                condition=~models.Q(country="US"),
                name="institute_country_not_us",
            ),
        ),
    ]
