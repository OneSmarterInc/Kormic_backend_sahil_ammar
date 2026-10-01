import uuid
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('accounts', '0003_githuboauthstate')]
    operations = [
        migrations.AddField(model_name='githuboauthstate', name='telemetry_id', field=models.UUIDField(null=True, editable=False)),
        migrations.AlterField(model_name='githuboauthstate', name='telemetry_id', field=models.UUIDField(default=uuid.uuid4, null=True, editable=False)),
    ]
