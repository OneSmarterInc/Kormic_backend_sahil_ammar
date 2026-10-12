from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_studentfacecapture")]
    operations = [migrations.AddField(model_name="account", name="session_version", field=models.PositiveIntegerField(default=0))]
