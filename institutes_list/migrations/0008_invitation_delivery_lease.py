from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("institutes_list", "0007_listedstudent_invite_delivery")]
    operations = [migrations.AddField(model_name="listedstudent", name="invite_delivery_started_at",
                                     field=models.DateTimeField(null=True, blank=True))]
