from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.CreateModel(
        name="CSPReport",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("day", models.DateField()),
            ("directive", models.CharField(max_length=40)),
            ("blocked_origin", models.CharField(max_length=255)),
            ("count", models.PositiveIntegerField(default=1)),
        ],
        options={"constraints": [models.UniqueConstraint(
            fields=("day", "directive", "blocked_origin"), name="unique_daily_csp_report"
        )]},
    )]
