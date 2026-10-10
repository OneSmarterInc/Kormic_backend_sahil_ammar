from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('url_discovery', '0001_initial')]
    operations = [
        migrations.AddField(model_name='discoveredurl', name='html_snapshot', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='discoveredurl', name='rendered', field=models.BooleanField(default=False)),
    ]
